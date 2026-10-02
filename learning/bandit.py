"""有界多臂老虎机：学习「这个群喜欢怎么被回话」。

为什么用老虎机而不是"让模型自己改提示词"
----------------------------------------
直接让 LLM 改写自己的系统提示词是不可审计、不可回滚、会自我强化的。
把「回复风格」离散成有限档位、用带硬上限的奖励更新来选档，能做到：

- **有界**：任何档位的分数被夹在 ``[-max_abs, +max_abs]``，且最终施加到提示词上的
  偏移再乘 ``max_offset`` 系数并夹在基线 ±X% 内。无论收到多少反馈，行为都不会跑飞。
- **有据**：每个档位都记录了被选中次数与累计奖励，面板可以逐条解释。
- **可退**：状态就是一张纯字典表，快照/回滚是复制与覆盖。
- **不张冠李戴**：只有"当时真正用的那一档"才会被记分。

算法
----
1. **选择**：以 ``epsilon`` 概率随机探索；否则在（样本达标的）档位里选
   「乐观均值」最高的——样本少时给予置信加成，避免某个档位靠一次好评就锁死。
2. **更新**：``score += learning_rate * reward * signal_weight``，其中奖励由
   :mod:`learning.feedback` 给出的极性与权重决定。
3. **衰减**：``decay`` 以半衰期方式让旧反馈随时间变淡，防止被一次风波永久带偏。
4. **门槛**：档位被选中次数少于 ``min_samples`` 时，分数**不施加**到提示词，
   只作为探索依据——这样"学习"不会因为一条消息就改行为。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
#  策略维度
# --------------------------------------------------------------------------- #

#: 每个维度的可选档位，从低到高。索引即档位编号。
#: 基线统一取中间档（见 :data:`BASELINE_INDEX`）——这样两个方向都有调整空间。
DIMENSIONS: dict[str, list[str]] = {
    "length": ["极简", "简短", "适中", "详细", "详尽"],
    "formality": ["很口语", "偏口语", "适中", "偏正式", "很正式"],
    "emoji": ["完全不用", "极少", "偶尔", "较多", "大量"],
    "warmth": ["公事公办", "中性", "稍暖", "热情", "很热情"],
    "directness": ["很委婉", "偏委婉", "适中", "偏直接", "很直接"],
}

#: 维度 -> 渲染进提示词时的自然语言模板。``{level}`` 会被替换成该档位的描述。
DIMENSION_PROMPT: dict[str, str] = {
    "length": "回复长度：{level}",
    "formality": "语气正式程度：{level}",
    "emoji": "表情符号使用：{level}",
    "warmth": "情感温度：{level}",
    "directness": "表达直接程度：{level}",
}

#: 维度 -> 该档位的可执行描述（给模型看的第二人称指令，比"偏口语"更好用）
DIMENSION_HINT: dict[str, list[str]] = {
    "length": [
        "只回一两句，能用一句就别用两句",
        "尽量简短，不要展开",
        "正常长度，把话说完即可",
        "可以多说一点，给出理由或例子",
        "详细解释，分点说明",
    ],
    "formality": [
        "用很随意的口语，可以带语气词",
        "偏口语，像朋友随口聊天",
        "自然口语，不必刻意正式",
        "偏正式，用词讲究一些",
        "正式书面语，避免网络用语",
    ],
    "emoji": [
        "不要使用任何表情符号",
        "几乎不用表情，最多偶尔一个",
        "可以偶尔用一个表情",
        "可以适当多用表情让语气活泼",
        "多用表情符号表达情绪",
    ],
    "warmth": [
        "就事论事，不需要寒暄",
        "保持中性，不加情绪色彩",
        "语气平和，稍微友好一点",
        "语气热情，表现出关心",
        "非常热情，多给情绪支持",
    ],
    "directness": [
        "非常委婉，先铺垫再给结论",
        "尽量委婉，避免直接否定",
        "直说但注意措辞",
        "直接给结论，少铺垫",
        "非常直接，有话直说不要绕",
    ],
}

BASELINE_INDEX = 2  # 每个维度共 5 档，索引 2 是中间档
N_LEVELS = 5


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else (hi if value > hi else value)


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ArmStat:
    """单个档位的学习状态。"""

    score: float = 0.0      # 累计（含衰减）奖励
    pulls: int = 0          # 被选中次数
    updated: float = 0.0    # 最近一次更新时间戳

    def to_dict(self) -> dict:
        return {"score": round(self.score, 6), "pulls": self.pulls, "updated": self.updated}

    @classmethod
    def from_dict(cls, d: dict) -> "ArmStat":
        return cls(
            score=float(d.get("score", 0.0) or 0.0),
            pulls=int(d.get("pulls", 0) or 0),
            updated=float(d.get("updated", 0.0) or 0.0),
        )


@dataclass
class Choice:
    """一次策略选择的结果：每个维度选了哪一档。

    ``picks`` 是**实际会施加到行为上**的档位；``explored_picks`` 记录探索时
    真正抽到的档位（用于把反馈归因给被探索的那一档）。
    两者在"探索但样本不足"时会不同：行为仍走基线，但分数记给探索档。
    """

    picks: dict[str, int] = field(default_factory=dict)
    explored: bool = False          # 本次是否含随机探索
    fallback: bool = False          # 是否所有维度都因证据不足而回退基线
    explored_picks: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "picks": dict(self.picks),
            "explored": self.explored,
            "fallback": self.fallback,
            "explored_picks": dict(self.explored_picks or {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Choice":
        picks = {k: int(v) for k, v in (d.get("picks") or {}).items()}
        exp = {k: int(v) for k, v in (d.get("explored_picks") or {}).items()}
        return cls(picks=picks, explored=bool(d.get("explored")),
                   fallback=bool(d.get("fallback")), explored_picks=exp)

    def update_picks(self) -> dict[str, int]:
        """反馈应当记给哪些档位。

        以 ``picks``（实际施加的档位）为基准，再把**被探索过的**维度覆盖成
        探索档位——探索时那些维度的行为虽走基线，但变量来自被探索的档，
        反馈要记给它们，否则探索永远攒不到证据，样本门槛就成了死锁。

        注意不能直接用 ``explored_picks`` 整体替换：它只包含被探索的维度，
        替换会让未探索的维度丢失归因目标（曾因此把分数记到基线档上）。
        """
        out = dict(self.picks)
        for k, v in (self.explored_picks or {}).items():
            out[k] = v
        return out

    def key(self) -> str:
        """稳定字符串标识，用于持久化与展示。"""
        return ",".join(f"{k}={self.picks.get(k, BASELINE_INDEX)}" for k in sorted(DIMENSIONS))

    def describe(self) -> str:
        parts = []
        for dim in DIMENSIONS:
            idx = self.picks.get(dim, BASELINE_INDEX)
            levels = DIMENSIONS[dim]
            level = levels[idx] if 0 <= idx < len(levels) else "?"
            parts.append(f"{dim}={level}")
        return " · ".join(parts)


@dataclass
class ArmUpdate:
    """对某个档位施加一次奖励后的结果（用于解释与审计）。"""

    dimension: str
    level: int
    reward: float
    score_before: float
    score_after: float
    pulls: int


# --------------------------------------------------------------------------- #
#  学习器
# --------------------------------------------------------------------------- #

class StrategyBandit:
    """按群隔离的有界策略学习器。

    Args:
        max_abs: 单个档位分数的绝对值上限（硬上限的第一层）。
        max_offset: 分数换算成提示词偏移时的系数，偏移再被夹在 ±max_offset。
        learning_rate: 每次反馈的学习率。
        epsilon: 探索概率。
        min_samples: 档位生效所需的最小被选中次数（样本门槛）。
        decay_half_life: 分数衰减半衰期（秒）；0 表示不衰减。
        explore_bonus: 乐观初始化的置信加成系数。
    """

    def __init__(
        self,
        *,
        max_abs: float = 3.0,
        max_offset: float = 0.20,
        learning_rate: float = 0.25,
        epsilon: float = 0.12,
        min_samples: int = 5,
        decay_half_life: float = 7 * 86400,
        explore_bonus: float = 0.6,
    ):
        self.max_abs = float(max_abs)
        self.max_offset = float(max_offset)
        self.learning_rate = float(learning_rate)
        self.epsilon = clamp(float(epsilon), 0.0, 1.0)
        self.min_samples = int(min_samples)
        self.decay_half_life = float(decay_half_life)
        self.explore_bonus = float(explore_bonus)
        # {group_id: {dimension: [ArmStat x N_LEVELS]}}
        self.table: dict[str, dict[str, list[ArmStat]]] = {}

    # ---------- 基础 ---------- #

    def group_table(self, group_id: str) -> dict[str, list[ArmStat]]:
        """取（或惰性创建）某群的档位表。"""
        gid = str(group_id or "")
        tbl = self.table.get(gid)
        if tbl is None:
            tbl = {dim: [ArmStat() for _ in range(len(levels))] for dim, levels in DIMENSIONS.items()}
            self.table[gid] = tbl
        else:
            # 兼容维度新增/减少的情况
            for dim, levels in DIMENSIONS.items():
                if dim not in tbl or len(tbl[dim]) != len(levels):
                    tbl[dim] = [ArmStat() for _ in range(len(levels))]
        return tbl

    def _decayed(self, stat: ArmStat, now: float) -> float:
        """按半衰期把分数衰减到 now 时刻。"""
        if self.decay_half_life <= 0 or stat.updated <= 0:
            return stat.score
        elapsed = max(0.0, now - stat.updated)
        factor = 0.5 ** (elapsed / self.decay_half_life)
        return stat.score * factor

    def effective_score(self, group_id: str, dimension: str, level: int, *, now: float | None = None) -> float:
        """该档位当前的有效分数（含衰减）。"""
        now = time.time() if now is None else now
        tbl = self.group_table(group_id)
        stats = tbl.get(dimension)
        if not stats or not (0 <= level < len(stats)):
            return 0.0
        return self._decayed(stats[level], now)

    # ---------- 选择 ---------- #

    def choose(self, group_id: str, *, rng: random.Random | None = None,
               now: float | None = None) -> Choice:
        """为某群选一套策略档位。

        **关键保证**：任何**证据不足**的档位都不会被施加到行为上。
        探索时抽到的档位只用于"试探并积累证据"，实际行为仍走基线；
        被探索的档位记录在 ``explored_picks`` 里，供反馈归因使用。
        这样"学习"在攒够样本前完全不会改变机器人的说话方式。
        """
        now = time.time() if now is None else now
        rnd = rng or random
        tbl = self.group_table(group_id)
        picks: dict[str, int] = {}
        explored_picks: dict[str, int] = {}
        explored = False
        all_fallback = True

        for dim, levels in DIMENSIONS.items():
            stats = tbl[dim]

            # ---- 挑选目标档位 ----
            if rnd.random() < self.epsilon:
                # 探索：随机一档，但若证据不足则只记录、不施加
                trial = rnd.randrange(len(levels))
                explored = True
                explored_picks[dim] = trial
                if stats[trial].pulls >= self.min_samples:
                    picks[dim] = trial
                    all_fallback = False
                else:
                    picks[dim] = BASELINE_INDEX
                continue

            # ---- 利用：样本达标的档位里取乐观均值最高者 ----
            best_idx, best_val = None, None
            for idx, st in enumerate(stats):
                if st.pulls < self.min_samples:
                    continue
                val = self._decayed(st, now) + self.explore_bonus / (1.0 + st.pulls) ** 0.5
                if best_val is None or val > best_val:
                    best_val, best_idx = val, idx
            if best_idx is None:
                picks[dim] = BASELINE_INDEX
            else:
                picks[dim] = best_idx
                all_fallback = False

        return Choice(picks=picks, explored=explored, fallback=all_fallback,
                      explored_picks=explored_picks)

    # ---------- 更新 ---------- #

    def update(self, group_id: str, choice: Choice, polarity: int,
               weight: float, *, now: float | None = None) -> list[ArmUpdate]:
        """按反馈给「当时实际用到的档位」记分。

        Args:
            group_id: 群号。
            choice: 产生这条回复时使用的策略。
            polarity: +1 / -1。
            weight: 信号可信度（0~1）。

        Returns:
            每个维度的更新明细，供面板解释「为什么变」。
        """
        now = time.time() if now is None else now
        if polarity == 0 or weight <= 0:
            return []
        reward = float(polarity) * clamp(float(weight), 0.0, 1.0)
        tbl = self.group_table(group_id)
        out: list[ArmUpdate] = []
        # 探索时行为走的是基线，但变量来自被探索的那一档——反馈要记给它，
        # 否则探索永远积累不到证据，样本门槛就成了死锁。
        credited = choice.update_picks()

        for dim, levels in DIMENSIONS.items():
            idx = credited.get(dim, BASELINE_INDEX)
            if not (0 <= idx < len(levels)):
                idx = BASELINE_INDEX
            stats = tbl[dim]
            st = stats[idx]
            before = self._decayed(st, now)
            delta = self.learning_rate * reward
            after = clamp(before + delta, -self.max_abs, self.max_abs)
            st.score = after
            st.updated = now
            st.pulls += 1
            out.append(ArmUpdate(
                dimension=dim, level=idx, reward=reward,
                score_before=before, score_after=after, pulls=st.pulls,
            ))
        return out

    # ---------- 提示词偏移 ---------- #

    def offset_for(self, group_id: str, dimension: str, *, now: float | None = None) -> float:
        """把该维度当前档位的分数换算成相对基线的偏移（已夹在 ±max_offset）。"""
        now = time.time() if now is None else now
        tbl = self.group_table(group_id)
        stats = tbl.get(dimension)
        if not stats:
            return 0.0
        best_idx, best_val = BASELINE_INDEX, None
        for idx, st in enumerate(stats):
            if st.pulls < self.min_samples:
                continue
            val = self._decayed(st, now)
            if best_val is None or val > best_val:
                best_val, best_idx = val, idx
        if best_val is None or best_val <= 0:
            return 0.0
        raw = (best_idx - BASELINE_INDEX) / float(N_LEVELS - 1)   # -0.5 ~ +0.5
        return clamp(raw * (best_val / max(self.max_abs, 1e-9)), -self.max_offset, self.max_offset)

    def explain(self, group_id: str, *, now: float | None = None) -> list[dict]:
        """逐维度解释当前学到的倾向（面板用）。"""
        now = time.time() if now is None else now
        tbl = self.group_table(group_id)
        rows: list[dict] = []
        for dim, levels in DIMENSIONS.items():
            stats = tbl[dim]
            cells = []
            for idx, st in enumerate(stats):
                cells.append({
                    "level": idx,
                    "label": levels[idx],
                    "score": round(self._decayed(st, now), 4),
                    "pulls": st.pulls,
                    "eligible": st.pulls >= self.min_samples,
                })
            eligible = [c for c in cells if c["eligible"]]
            chosen = None
            if eligible:
                chosen = max(eligible, key=lambda c: c["score"])["level"]
            rows.append({
                "dimension": dim,
                "baseline": BASELINE_INDEX,
                "chosen": chosen,
                "fallback": chosen is None,
                "offset": round(self.offset_for(group_id, dim, now=now), 4),
                "cells": cells,
            })
        return rows

    # ---------- 序列化 ---------- #

    def to_dict(self) -> dict:
        return {
            "table": {
                gid: {
                    dim: [st.to_dict() for st in stats]
                    for dim, stats in tbl.items()
                }
                for gid, tbl in self.table.items()
            }
        }

    def load_dict(self, data: dict) -> None:
        tbl_all = (data or {}).get("table") or {}
        table: dict[str, dict[str, list[ArmStat]]] = {}
        for gid, tbl in tbl_all.items():
            if not isinstance(tbl, dict):
                continue
            dims: dict[str, list[ArmStat]] = {}
            for dim, stats in tbl.items():
                if dim not in DIMENSIONS or not isinstance(stats, list):
                    continue
                lv = [ArmStat.from_dict(s) if isinstance(s, dict) else ArmStat()
                      for s in stats]
                # 长度对齐
                want = len(DIMENSIONS[dim])
                lv = (lv + [ArmStat() for _ in range(want)])[:want]
                dims[dim] = lv
            table[str(gid)] = dims
        self.table = table

    def reset_group(self, group_id: str) -> None:
        self.table.pop(str(group_id or ""), None)
