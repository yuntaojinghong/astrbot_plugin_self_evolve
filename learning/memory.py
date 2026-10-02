"""经验记忆：把反复出现的信息沉淀成可检索、可衰减的条目。

与"把聊天记录塞进上下文"的区别
------------------------------
塞聊天记录会把上下文越撑越满、噪声越来越大、还夹带隐私。
本模块只保留**原子化、有来源、有置信度**的短句，并且：

- **去重合并**：同一件事说三次不会变成三条，而是 ``evidence 3`` + 置信度提升。
- **衰减淘汰**：长期没被再次印证的条目置信度下降，低于阈值自动失效，
  不会被某次偶发对话永久污染。
- **可溯源**：每条都记得是谁贡献的（反思产出 / 管理员手写 / 用户纠正）。
- **群隔离**：检索永远只在当前群的条目里进行。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
#  条目类型
# --------------------------------------------------------------------------- #

KIND_TERM = "term"              # 本群黑话、称呼、缩写
KIND_PREFERENCE = "preference"  # 该群偏好（话题、口吻、禁忌）
KIND_CORRECTION = "correction"  # 机器人曾答错、应记住的正确说法
KIND_FACT = "fact"              # 群内稳定事实（谁是谁、什么活动）

KINDS = (KIND_TERM, KIND_PREFERENCE, KIND_CORRECTION, KIND_FACT)
KIND_LABEL = {
    KIND_TERM: "术语",
    KIND_PREFERENCE: "偏好",
    KIND_CORRECTION: "纠错",
    KIND_FACT: "事实",
}

SOURCE_REFLECT = "reflect"
SOURCE_ADMIN = "admin"
SOURCE_USER = "user"
SOURCE_LABEL = {SOURCE_REFLECT: "自动反思", SOURCE_ADMIN: "管理员", SOURCE_USER: "用户纠正"}

#: 低于该置信度的条目不参与注入。
MIN_CONFIDENCE = 0.25
#: 单条内容长度上限，避免把整段话塞进来。
MAX_CONTENT_LEN = 200

_CLEAN_RE = re.compile(r"[\r\n\t]+")


def _norm(text: str) -> str:
    """归一化：折叠空白、去首尾标点，用于去重比较。"""
    t = _CLEAN_RE.sub(" ", str(text or "")).strip()
    t = t.strip("「」\"'‘’“”（）()【】[]《》 ,，.。;；:：-—")
    return re.sub(r"\s+", " ", t)


def _dedup_key(text: str) -> str:
    """去重键：忽略大小写与标点后的紧凑形式。"""
    t = _norm(text).lower()
    return re.sub(r"[\s，,。.！!？?；;：:\"'「」（）()【】\[\]]+", "", t)


def _bigrams(text: str) -> set[str]:
    """字符二元组，用于中文近似匹配（零依赖，不需要分词器）。"""
    t = _dedup_key(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（零依赖，对中文短句够用）。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 1.0 if _dedup_key(a) == _dedup_key(b) else 0.0
    return len(ga & gb) / float(len(ga | gb))


def containment(needle: str, haystack: str) -> float:
    """needle 的二元组有多大比例出现在 haystack 里（0~1）。

    中文查询常是「接口文档在哪」这种问句，与存储的「本群的接口文档在置顶里」
    既非互相包含、Jaccard 也偏低，但「接口/口文/文档/档在」这些二元组大面积重合。
    用包含率比用 Jaccard 更能反映「问的就是这件事」。
    """
    ga, gb = _bigrams(needle), _bigrams(haystack)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga))


# --------------------------------------------------------------------------- #
#  条目
# --------------------------------------------------------------------------- #

def score_relevance(content: str, query: str, confidence: float = 1.0) -> float:
    """计算「某条内容」与「查询串」的相关度（0~1）。

    纯函数，便于单测穷举。分层判定，从强到弱：

    1. 空查询 → 直接用置信度（面板列表用）。
    2. 互相包含 → 最强信号。
    3. 分词片段命中：**内容里的词出现在查询里**。
    4. 二元组包含率：中文问句最可靠的近似匹配
       （实测「接口文档在哪」对「本群的接口文档在置顶里」= 0.80，
        而无关组合都 ≤ 0.33，因此 0.40 是能干净分开两者的阈值）。
    5. 二元组 Jaccard 兜底。

    .. note::
        本函数是**词面匹配**，不做语义改写。「版主怎么称呼」这类把关键实词
        换成同义表达的问句匹配不上，这是零依赖实现的已知边界——因此注入
        按"宁缺毋滥"处理：匹配不上就不注入，而不是放宽阈值引入误匹配。
    """
    if not query:
        return confidence
    q = _dedup_key(query)
    c = _dedup_key(content)
    if not q or not c:
        return 0.0
    if c in q:
        return 1.0
    if q in c:
        return 0.9

    # 3) 内容分词后，看词是否出现在查询里
    words = [w for w in re.split(r"[\s，,。.！!？?；;：:]+", _norm(content)) if len(w) >= 2]
    hits = sum(1 for w in words if w in query)
    if hits:
        return min(0.9, 0.5 + 0.4 * hits / len(words))

    # 3b) 查询里的词是否出现在内容里（与 3 互补：内容未分词时 3 会失手，
    #     例如「版主」夹在「大家管版主叫」这个长片段里）
    for w in [x for x in re.split(r"[\s，,。.！!？?；;：:]+", _norm(query)) if len(x) >= 2]:
        if w in content:
            return 0.8

    # 4) 二元组包含率（阈值按实测分离度确定）
    cont = containment(query, content)
    if cont >= 0.40:
        return min(0.9, 0.55 + 0.4 * cont)

    # 5) 兜底
    return max(similarity(content, query) * 0.6, cont * 0.5)


@dataclass
class Entry:
    """一条经验。"""

    content: str
    kind: str = KIND_FACT
    group_id: str = ""
    confidence: float = 0.5
    evidence: int = 1
    source: str = SOURCE_REFLECT
    created: float = 0.0
    last_seen: float = 0.0
    #: 管理员确认过的条目免疫衰减与自动删除
    pinned: bool = False
    eid: str = ""

    def __post_init__(self):
        self.content = _norm(self.content)[:MAX_CONTENT_LEN]
        if self.kind not in KINDS:
            self.kind = KIND_FACT
        now = time.time()
        if not self.created:
            self.created = now
        if not self.last_seen:
            self.last_seen = self.created
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.evidence = max(1, int(self.evidence))
        if not self.eid:
            self.eid = f"{self.group_id}:{self.kind}:{_dedup_key(self.content)[:40]}"

    # ---------- 派生属性 ---------- #

    def relevance(self, query: str) -> float:
        """与查询串的相关度（0~1）。无查询时按置信度计。"""
        conf = self.confidence if not query else 1.0
        return score_relevance(self.content, query, conf)

    def age_days(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - float(self.last_seen or self.created)) / 86400.0)

    def effective_confidence(self, half_life_days: float, now: float | None = None) -> float:
        """含时间衰减的有效置信度。``pinned`` 条目不衰减。"""
        if self.pinned or half_life_days <= 0:
            return self.confidence
        factor = 0.5 ** (self.age_days(now) / float(half_life_days))
        return max(0.0, min(1.0, self.confidence * factor))

    def is_alive(self, half_life_days: float, now: float | None = None) -> bool:
        if self.pinned:
            return True
        return self.effective_confidence(half_life_days, now) >= MIN_CONFIDENCE

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "kind": self.kind,
            "group_id": self.group_id,
            "confidence": round(self.confidence, 4),
            "evidence": self.evidence,
            "source": self.source,
            "created": self.created,
            "last_seen": self.last_seen,
            "pinned": self.pinned,
            "eid": self.eid,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Entry | None":
        if not isinstance(d, dict):
            return None
        content = d.get("content")
        if not content:
            return None
        return cls(
            content=str(content),
            kind=str(d.get("kind", KIND_FACT)),
            group_id=str(d.get("group_id", "")),
            confidence=float(d.get("confidence", 0.5) or 0.5),
            evidence=int(d.get("evidence", 1) or 1),
            source=str(d.get("source", SOURCE_REFLECT)),
            created=float(d.get("created", 0) or 0),
            last_seen=float(d.get("last_seen", 0) or 0),
            pinned=bool(d.get("pinned", False)),
            eid=str(d.get("eid", "")),
        )


# --------------------------------------------------------------------------- #
#  存储
# --------------------------------------------------------------------------- #

@dataclass
class AddResult:
    """写入结果，供调用方解释「是新增还是合并」。"""

    entry: Entry
    created: bool
    merged: bool
    reason: str = ""


class MemoryStore:
    """按群隔离的经验条目集合。

    Args:
        max_per_group: 每群条目上限，超出时淘汰置信度最低的（``pinned`` 除外）。
        half_life_days: 置信度衰减半衰期（天）。
    """

    def __init__(self, *, max_per_group: int = 200, half_life_days: float = 60.0):
        self.max_per_group = int(max_per_group)
        self.half_life_days = float(half_life_days)
        # {group_id: [Entry]}
        self.groups: dict[str, list[Entry]] = {}

    # ---------- 读 ---------- #

    def entries(self, group_id: str) -> list[Entry]:
        return list(self.groups.get(str(group_id or ""), []))

    def find_similar(self, group_id: str, content: str, kind: str | None = None,
                     threshold: float = 0.72) -> Entry | None:
        """找语义上重复的条目。"""
        best, best_sim = None, 0.0
        for e in self.groups.get(str(group_id or ""), []):
            if kind and e.kind != kind:
                continue
            s = similarity(e.content, content)
            if s >= threshold and s > best_sim:
                best, best_sim = e, s
        return best

    # ---------- 写 ---------- #

    def add(self, entry: Entry, *, now: float | None = None,
            merge_threshold: float = 0.72) -> AddResult:
        """加入一条经验；与已有条目重复时合并（evidence+1、置信度提升）。"""
        now = time.time() if now is None else now
        gid = str(entry.group_id or "")
        bucket = self.groups.setdefault(gid, [])

        dup = self.find_similar(gid, entry.content, entry.kind, merge_threshold)
        if dup is not None:
            dup.evidence += 1
            dup.last_seen = now
            # 重复印证提升置信度，但收敛到 1.0，避免"说得多就一定对"
            dup.confidence = min(1.0, dup.confidence + (1.0 - dup.confidence) * 0.25)
            if entry.source == SOURCE_ADMIN:
                dup.pinned = True
                dup.confidence = max(dup.confidence, 0.9)
            return AddResult(entry=dup, created=False, merged=True,
                             reason=f"与已有条目重复（相似度 {similarity(dup.content, entry.content):.2f}），已合并")

        entry.group_id = gid
        entry.created = entry.created or now
        entry.last_seen = now
        if not entry.eid:
            entry.eid = f"{gid}:{entry.kind}:{_dedup_key(entry.content)[:40]}"
        if entry.source == SOURCE_ADMIN:
            entry.pinned = True
            entry.confidence = max(entry.confidence, 0.9)
        bucket.append(entry)
        self._enforce_limit(gid, now)
        return AddResult(entry=entry, created=True, merged=False)

    def _enforce_limit(self, group_id: str, now: float) -> int:
        """超出上限时淘汰最弱的条目（pinned 免疫）。返回淘汰数量。"""
        bucket = self.groups.get(group_id, [])
        if len(bucket) <= self.max_per_group:
            return 0
        ranked = sorted(
            bucket,
            key=lambda e: (e.pinned, e.effective_confidence(self.half_life_days, now)),
        )
        drop = len(bucket) - self.max_per_group
        removed = 0
        keep: list[Entry] = []
        for e in ranked:
            if removed < drop and not e.pinned:
                removed += 1
                continue
            keep.append(e)
        self.groups[group_id] = keep
        return removed

    def forget(self, group_id: str, eid: str) -> bool:
        """按 eid、或按内容片段删除条目（命令里用户只会给片段）。

        匹配顺序：eid 精确 → 内容完全一致 → 内容包含该片段。
        用包含匹配是为了让「进化 遗忘 扫地僧」这类片段用法也能命中。
        """
        bucket = self.groups.get(str(group_id or ""), [])
        target_key = _dedup_key(eid)
        if not target_key:
            return False
        for i, e in enumerate(bucket):
            if e.eid == eid:
                bucket.pop(i)
                return True
        for i, e in enumerate(bucket):
            if _dedup_key(e.content) == target_key:
                bucket.pop(i)
                return True
        for i, e in enumerate(bucket):
            if target_key in _dedup_key(e.content):
                bucket.pop(i)
                return True
        return False

    def set_pinned(self, group_id: str, eid: str, pinned: bool) -> bool:
        for e in self.groups.get(str(group_id or ""), []):
            if e.eid == eid:
                e.pinned = bool(pinned)
                if pinned:
                    e.confidence = max(e.confidence, 0.9)
                return True
        return False

    def prune(self, *, now: float | None = None) -> int:
        """清理所有已失效条目（置信度衰减到阈值以下）。返回清理数量。"""
        now = time.time() if now is None else now
        removed = 0
        for gid, bucket in list(self.groups.items()):
            alive = [e for e in bucket if e.is_alive(self.half_life_days, now)]
            removed += len(bucket) - len(alive)
            self.groups[gid] = alive
        return removed

    # ---------- 检索 ---------- #

    def retrieve(self, group_id: str, query: str = "", *, limit: int = 5,
                 now: float | None = None, min_score: float = 0.18) -> list[Entry]:
        """取最有用的若干条：相关度 × 有效置信度 排序。"""
        now = time.time() if now is None else now
        scored: list[tuple[float, Entry]] = []
        for e in self.groups.get(str(group_id or ""), []):
            conf = e.effective_confidence(self.half_life_days, now)
            if conf < MIN_CONFIDENCE:
                continue
            score = e.relevance(query) * conf
            if query and score < min_score:
                continue
            scored.append((score, e))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[: max(0, int(limit))]]

    # ---------- 统计 / 序列化 ---------- #

    def stats(self, group_id: str, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        bucket = self.groups.get(str(group_id or ""), [])
        alive = [e for e in bucket if e.is_alive(self.half_life_days, now)]
        by_kind: dict[str, int] = {}
        for e in alive:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {
            "total": len(bucket),
            "alive": len(alive),
            "pending_decay": len(bucket) - len(alive),
            "by_kind": by_kind,
            "pinned": sum(1 for e in bucket if e.pinned),
        }

    def to_dict(self) -> dict:
        return {"groups": {gid: [e.to_dict() for e in bucket] for gid, bucket in self.groups.items()}}

    def load_dict(self, data: dict) -> None:
        raw = (data or {}).get("groups") or {}
        out: dict[str, list[Entry]] = {}
        for gid, items in raw.items():
            if not isinstance(items, list):
                continue
            bucket: list[Entry] = []
            for it in items:
                e = Entry.from_dict(it) if isinstance(it, dict) else None
                if e is not None:
                    e.group_id = str(gid)
                    if not e.eid:
                        e.eid = f"{gid}:{e.kind}:{_dedup_key(e.content)[:40]}"
                    bucket.append(e)
            out[str(gid)] = bucket
        self.groups = out

    def reset_group(self, group_id: str) -> int:
        return len(self.groups.pop(str(group_id or ""), []))
