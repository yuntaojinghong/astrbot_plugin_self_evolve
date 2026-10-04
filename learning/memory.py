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
    """一条经验。

    ``subject_id`` / ``subject_name`` 用来表达「这条是关于谁的」：

    - 两者都为空 → 群级条目，对整个群生效（例如「本群不喜欢长篇大论」）
    - 有值       → 人员条目，只在该成员发言时优先注入（例如「@小明 喜欢被叫猫猫」）

    为什么需要它：此前所有条目都是群级的，模型整理时被明确要求「不要出现他/那个
    这类指代」，于是「谁喜欢什么」根本无法沉淀——用户感受到的就是「机器人记不住人」。
    """

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
    #: 这条经验关于谁（QQ 号）。空 = 群级条目
    subject_id: str = ""
    #: 该成员的显示名，仅供展示与提示词使用
    subject_name: str = ""

    def __post_init__(self):
        self.content = _norm(self.content)[:MAX_CONTENT_LEN]
        if self.kind not in KINDS:
            self.kind = KIND_FACT
        self.subject_id = str(self.subject_id or "").strip()
        self.subject_name = str(self.subject_name or "").strip()[:40]
        # 只有 id 没有名字时，用 id 兜底显示，避免面板出现空白主体
        if self.subject_id and not self.subject_name:
            self.subject_name = self.subject_id
        # 只有名字没有 id：无法做检索，退化成群级条目
        if not self.subject_id:
            self.subject_name = ""
        now = time.time()
        if not self.created:
            self.created = now
        if not self.last_seen:
            self.last_seen = self.created
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.evidence = max(1, int(self.evidence))
        if not self.eid:
            self.eid = self.make_eid()

    def make_eid(self) -> str:
        """生成稳定 id。

        主体必须参与：否则「小明喜欢猫」和「小红喜欢猫」会算出同一个 eid，
        互相覆盖。eid 同时是去重键，所以这是正确性问题，不只是好看。
        """
        scope = self.subject_id or "group"
        return f"{self.group_id}:{scope}:{self.kind}:{_dedup_key(self.content)[:40]}"

    @property
    def is_person(self) -> bool:
        """是否是关于某个具体成员的条目。"""
        return bool(self.subject_id)

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
            # 老数据里没有这两个键，from_dict 会补成空串 → 自动当作群级条目
            "subject_id": self.subject_id,
            "subject_name": self.subject_name,
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
            subject_id=str(d.get("subject_id", "") or ""),
            subject_name=str(d.get("subject_name", "") or ""),
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
                     threshold: float = 0.72, subject_id: str = "") -> Entry | None:
        """找语义上重复的条目。

        ``subject_id`` 参与判定：不同人的同类事实不能合并。否则
        「小明喜欢猫」和「小红喜欢猫」内容高度相似，会被并成一条，
        两个人的偏好就只剩一个人的了。
        """
        want = str(subject_id or "").strip()
        best, best_sim = None, 0.0
        for e in self.groups.get(str(group_id or ""), []):
            if kind and e.kind != kind:
                continue
            if e.subject_id != want:
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

        dup = self.find_similar(gid, entry.content, entry.kind, merge_threshold,
                                entry.subject_id)
        if dup is not None:
            dup.evidence += 1
            dup.last_seen = now
            # 重复印证提升置信度，但收敛到 1.0，避免"说得多就一定对"
            dup.confidence = min(1.0, dup.confidence + (1.0 - dup.confidence) * 0.25)
            # 后到的显示名补上（可能比先前更准确）
            if entry.subject_name and not dup.subject_name:
                dup.subject_name = entry.subject_name
            if entry.source == SOURCE_ADMIN:
                dup.pinned = True
                dup.confidence = max(dup.confidence, 0.9)
            return AddResult(entry=dup, created=False, merged=True,
                             reason=f"与已有条目重复（相似度 {similarity(dup.content, entry.content):.2f}），已合并")

        entry.group_id = gid
        entry.created = entry.created or now
        entry.last_seen = now
        # 无条件按最终归属重算 eid。
        #
        # 不能只判断 `if not entry.eid`：调用方常常先构造一条模板条目，
        # 那个临时 eid 是按空 group_id 算的。更关键的是，若有人复制已有条目
        # 改主体，沿用旧 eid 会让两条不同主体的记录共用同一个 id。
        entry.eid = entry.make_eid()
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
                 now: float | None = None, min_score: float = 0.18,
                 subject_id: str = "") -> list[Entry]:
        """取最有用的若干条：相关度 × 有效置信度 排序。

        ``subject_id`` 是**当前发言人**。传入时：

        - 关于他的个人条目优先，且**豁免关键词相关度过滤**——「记住某人」的意义
          就在于他说什么都能想起来，而不是非要提到关键字才想得起来。
        - 其他人的个人条目不注入（避免把 A 的偏好套到 B 身上）。
        - 群级条目照旧按相关度参与。

        不传 ``subject_id`` 时行为与从前一致（只取群级条目之外的全部），
        以便旧的调用方不受影响。
        """
        now = time.time() if now is None else now
        want = str(subject_id or "").strip()
        person: list[tuple[float, Entry]] = []
        group: list[tuple[float, Entry]] = []

        for e in self.groups.get(str(group_id or ""), []):
            conf = e.effective_confidence(self.half_life_days, now)
            if conf < MIN_CONFIDENCE:
                continue
            if e.subject_id:
                if e.subject_id != want:
                    continue        # 别人的个人条目，不注入
                # 本人的条目：不设相关度门槛，按置信度排序
                person.append((conf, e))
                continue
            score = e.relevance(query) * conf
            if query and score < min_score:
                continue
            group.append((score, e))

        person.sort(key=lambda t: t[0], reverse=True)
        group.sort(key=lambda t: t[0], reverse=True)

        total = max(0, int(limit))
        # 个人条目最多占一半额度，剩下的留给群级上下文，
        # 否则一个人攒的条目会把整块注入额度吃光。
        person_quota = total if not want else max(1, total // 2 + total % 2)
        picked = [e for _, e in person[:person_quota]]
        room = total - len(picked)
        if room > 0:
            picked += [e for _, e in group[:room]]
        # 仍有余量（群级条目不够）就把落选的个人条目补回来
        room = total - len(picked)
        if room > 0:
            picked += [e for _, e in person[person_quota: person_quota + room]]
        return picked[:total]

    def person_entries(self, group_id: str, subject_id: str,
                       *, now: float | None = None) -> list[Entry]:
        """某人在本群的全部个人条目（含已衰减到门槛以下的，供面板展示）。"""
        want = str(subject_id or "").strip()
        if not want:
            return []
        return [e for e in self.groups.get(str(group_id or ""), [])
                if e.subject_id == want]

    def known_people(self, group_id: str) -> dict[str, str]:
        """本群已记住的成员：{subject_id: subject_name}。"""
        out: dict[str, str] = {}
        for e in self.groups.get(str(group_id or ""), []):
            if e.subject_id:
                out.setdefault(e.subject_id, e.subject_name or e.subject_id)
        return out

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
