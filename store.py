"""持久化层：KV 存储 + 版本快照 + 审计日志 + 待批区。

设计目标（对应"可解释 / 可回滚"这两条硬要求）
--------------------------------------------
- **单一事实来源**：策略表与经验库整体序列化成一个 dict，快照就是复制它。
  回滚 = 覆盖，简单到不会出错。
- **版本化**：每次生效的变更先入快照再落地，历史有上限，可回退到任一版本。
- **审计日志**：记录"哪条用户消息 → 解析出什么信号 → 给哪一档加了多少分"，
  面板据此回答"你为什么变"。
- **待批区**：阶段二的反思产出先放这里，管理员批准后才进经验库。
  阶段一先把结构留好，避免后续改数据格式。

存储后端有两套：
1. AstrBot 插件 KV（``get_kv_data`` / ``put_kv_data``）——正式运行用；
2. 本地 JSON 文件——离线测试与 KV 不可用时用。
两者共用同一套序列化格式。
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from .learning.bandit import ArmStat, DIMENSIONS, StrategyBandit
from .learning.memory import Entry, MemoryStore

SCHEMA_VERSION = 1

#: 每个群最多保留的版本快照数
MAX_SNAPSHOTS = 30
#: 每个群最多保留的审计条目数
MAX_AUDIT = 400
#: 每个群最多保留的待批条目数
MAX_PENDING = 100


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
#  记录结构
# --------------------------------------------------------------------------- #

@dataclass
class Snapshot:
    """一次已生效变更的版本记录。"""

    sid: str = ""
    group_id: str = ""
    reason: str = ""
    created: float = 0.0
    #: 变更后学习状态的完整副本
    state: dict = field(default_factory=dict)
    #: 变更摘要（面板展示用），如 {"记忆": "+2 -1"}
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sid": self.sid, "group_id": self.group_id, "reason": self.reason,
            "created": self.created, "state": self.state, "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot | None":
        if not isinstance(d, dict) or not isinstance(d.get("state"), dict):
            return None
        return cls(
            sid=str(d.get("sid", "") or _new_id("s")),
            group_id=str(d.get("group_id", "")),
            reason=str(d.get("reason", "")),
            created=float(d.get("created", 0) or 0),
            state=d.get("state") or {},
            summary=d.get("summary") or {},
        )


@dataclass
class AuditEntry:
    """一次反馈归因的完整解释。"""

    aid: str = ""
    group_id: str = ""
    created: float = 0.0
    user_message: str = ""
    bot_reply: str = ""
    signal: str = ""
    polarity: int = 0
    weight: float = 0.0
    evidence: str = ""
    correction: str = ""
    #: 当时使用的策略组合
    choice: dict = field(default_factory=dict)
    #: 各档位分数变化 [{"dimension","level","before","after"}]
    updates: list = field(default_factory=list)
    #: 是否因为样本不足/总开关等原因没有真正生效
    applied: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "aid": self.aid, "group_id": self.group_id, "created": self.created,
            "user_message": self.user_message, "bot_reply": self.bot_reply,
            "signal": self.signal, "polarity": self.polarity, "weight": self.weight,
            "evidence": self.evidence, "correction": self.correction,
            "choice": self.choice, "updates": self.updates,
            "applied": self.applied, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEntry | None":
        if not isinstance(d, dict):
            return None
        return cls(
            aid=str(d.get("aid", "") or _new_id("a")),
            group_id=str(d.get("group_id", "")),
            created=float(d.get("created", 0) or 0),
            user_message=str(d.get("user_message", ""))[:200],
            bot_reply=str(d.get("bot_reply", ""))[:200],
            signal=str(d.get("signal", "")),
            polarity=int(d.get("polarity", 0) or 0),
            weight=float(d.get("weight", 0) or 0),
            evidence=str(d.get("evidence", ""))[:120],
            correction=str(d.get("correction", ""))[:200],
            choice=d.get("choice") or {},
            updates=d.get("updates") or [],
            applied=bool(d.get("applied", True)),
            note=str(d.get("note", ""))[:200],
        )


@dataclass
class PendingItem:
    """待批区条目：反思产出，管理员批准后才会生效。"""

    pid: str = ""
    group_id: str = ""
    created: float = 0.0
    #: "entry"（新经验条目） | "strategy"（策略变更建议）
    kind: str = "entry"
    payload: dict = field(default_factory=dict)
    reason: str = ""
    status: str = "pending"   # pending | approved | rejected

    def to_dict(self) -> dict:
        return {
            "pid": self.pid, "group_id": self.group_id, "created": self.created,
            "kind": self.kind, "payload": self.payload, "reason": self.reason,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingItem | None":
        if not isinstance(d, dict):
            return None
        return cls(
            pid=str(d.get("pid", "") or _new_id("p")),
            group_id=str(d.get("group_id", "")),
            created=float(d.get("created", 0) or 0),
            kind=str(d.get("kind", "entry")),
            payload=d.get("payload") or {},
            reason=str(d.get("reason", "")),
            status=str(d.get("status", "pending")),
        )


# --------------------------------------------------------------------------- #
#  学习状态容器
# --------------------------------------------------------------------------- #

class LearningState:
    """策略表 + 经验库的组合体，并提供按群序列化。"""

    def __init__(self, *, bandit: StrategyBandit | None = None,
                 memory: MemoryStore | None = None,
                 bandit_kwargs: dict | None = None,
                 memory_kwargs: dict | None = None):
        self.bandit = bandit or StrategyBandit(**(bandit_kwargs or {}))
        self.memory = memory or MemoryStore(**(memory_kwargs or {}))

    # ---------- 序列化 ---------- #

    def group_state(self, group_id: str) -> dict:
        """导出某群的学习状态（快照用）。"""
        gid = str(group_id or "")
        return {
            "memory": {gid: [e.to_dict() for e in self.memory.groups.get(gid, [])]},
            "bandit": {"table": {gid: {
                dim: [st.to_dict() for st in stats]
                for dim, stats in (self.bandit.table.get(gid) or {}).items()
            }}},
        }

    def load_group_state(self, group_id: str, state: dict) -> bool:
        """用快照覆盖某群的学习状态。返回是否成功。"""
        gid = str(group_id or "")
        if not isinstance(state, dict):
            return False
        mem = (state.get("memory") or {}).get(gid)
        if isinstance(mem, list):
            entries = []
            for it in mem:
                e = Entry.from_dict(it) if isinstance(it, dict) else None
                if e is not None:
                    e.group_id = gid
                    entries.append(e)
            self.memory.groups[gid] = entries
        tbl = ((state.get("bandit") or {}).get("table") or {}).get(gid)
        if isinstance(tbl, dict):
            built = {}
            for dim, stats in tbl.items():
                if dim not in DIMENSIONS or not isinstance(stats, list):
                    continue
                want = len(DIMENSIONS[dim])
                lv = [ArmStat.from_dict(s) if isinstance(s, dict) else ArmStat() for s in stats]
                built[dim] = (lv + [ArmStat() for _ in range(want)])[:want]
            self.bandit.table[gid] = built
        return True


# --------------------------------------------------------------------------- #
#  存储
# --------------------------------------------------------------------------- #

class LearnStore:
    """学习数据的持久化门面。

    Args:
        kv: AstrBot 的插件 KV 门面（需提供 ``get_kv_data`` / ``put_kv_data``）。
            传 None 时退化为纯内存 + 可选 JSON 落盘（离线测试用）。
        path: JSON 落盘路径；None 则不落盘。
        autosave: 是否在每次变更后立即落盘。
    """

    KEY = "self_evolve_state"

    def __init__(self, *, kv=None, path: str | None = None, autosave: bool = True,
                 bandit_kwargs: dict | None = None, memory_kwargs: dict | None = None):
        self.kv = kv
        self.path = path
        self.autosave = bool(autosave)
        self.state = LearningState(
            bandit_kwargs=bandit_kwargs, memory_kwargs=memory_kwargs
        )
        # 非学习状态（快照/审计/待批/开关/统计）单独放，避免与快照互相递归
        self.snapshots: dict[str, list[Snapshot]] = {}
        self.audit: dict[str, list[AuditEntry]] = {}
        self.pending: dict[str, list[PendingItem]] = {}
        self.flags: dict = {}
        self.stats: dict = {}
        self._loaded = False

    # ---------- 读写 ---------- #

    async def load(self) -> None:
        """读取持久化数据；失败时保持空状态并记录原因（不抛异常）。"""
        raw = None
        if self.kv is not None:
            try:
                raw = await self.kv.get_kv_data(self.KEY, None)
            except Exception as exc:  # 读取失败不能影响插件加载
                self.flags.setdefault("load_errors", []).append(f"KV 读取失败: {exc}")
                raw = None
        if raw is None and self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as exc:
                self.flags.setdefault("load_errors", []).append(f"文件读取失败: {exc}")
                raw = None
        if isinstance(raw, dict):
            self._apply(raw)
        self._loaded = True

    def _apply(self, raw: dict) -> None:
        try:
            self.state.bandit.load_dict(raw.get("bandit") or {})
            self.state.memory.load_dict(raw.get("memory") or {})
        except Exception as exc:
            self.flags.setdefault("load_errors", []).append(f"学习状态解析失败: {exc}")
        self.snapshots = self._parse_map(raw.get("snapshots"), Snapshot.from_dict)
        self.audit = self._parse_map(raw.get("audit"), AuditEntry.from_dict)
        self.pending = self._parse_map(raw.get("pending"), PendingItem.from_dict)
        self.flags = raw.get("flags") or {}
        self.stats = raw.get("stats") or {}

    @staticmethod
    def _parse_map(raw, factory) -> dict:
        out: dict[str, list] = {}
        if not isinstance(raw, dict):
            return out
        for gid, items in raw.items():
            if not isinstance(items, list):
                continue
            bucket = []
            for it in items:
                obj = factory(it)
                if obj is not None:
                    bucket.append(obj)
            out[str(gid)] = bucket
        return out

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "bandit": self.state.bandit.to_dict(),
            "memory": self.state.memory.to_dict(),
            "snapshots": {g: [s.to_dict() for s in v] for g, v in self.snapshots.items()},
            "audit": {g: [a.to_dict() for a in v] for g, v in self.audit.items()},
            "pending": {g: [p.to_dict() for p in v] for g, v in self.pending.items()},
            "flags": self.flags,
            "stats": self.stats,
        }

    async def save(self) -> bool:
        """落盘。KV 与文件任一成功即视为成功。"""
        payload = self.to_dict()
        ok = False
        if self.kv is not None:
            try:
                await self.kv.put_kv_data(self.KEY, payload)
                ok = True
            except Exception as exc:
                self.flags.setdefault("save_errors", []).append(f"KV 写入失败: {exc}")
        if self.path:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                ok = True
            except Exception as exc:
                self.flags.setdefault("save_errors", []).append(f"文件写入失败: {exc}")
        return ok

    async def maybe_save(self) -> None:
        if self.autosave:
            await self.save()

    # ---------- 快照 / 回滚 ---------- #

    def push_snapshot(self, group_id: str, reason: str, summary: dict | None = None,
                      *, now: float | None = None) -> Snapshot:
        """把当前学习状态存为一个版本。

        注意：必须在**变更之后**调用，快照保存的是"变更后"的状态，
        这样回滚到某个版本就等于回到那个时间点。
        """
        now = time.time() if now is None else now
        gid = str(group_id or "")
        snap = Snapshot(
            sid=_new_id("s"), group_id=gid, reason=str(reason or ""),
            created=now, state=self.state.group_state(gid), summary=summary or {},
        )
        bucket = self.snapshots.setdefault(gid, [])
        bucket.append(snap)
        if len(bucket) > MAX_SNAPSHOTS:
            del bucket[: len(bucket) - MAX_SNAPSHOTS]
        return snap

    def history(self, group_id: str) -> list[Snapshot]:
        return list(reversed(self.snapshots.get(str(group_id or ""), [])))

    def rollback(self, group_id: str, sid: str | None = None,
                 *, now: float | None = None) -> tuple[bool, str]:
        """回滚到某个版本；``sid`` 为空则回退到上一个版本。

        Returns:
            ``(是否成功, 说明)``。
        """
        now = time.time() if now is None else now
        gid = str(group_id or "")
        bucket = self.snapshots.get(gid, [])
        if not bucket:
            return False, "还没有任何可回滚的版本记录（学习发生并生效后才会产生）。"
        if sid:
            idx = next((i for i, s in enumerate(bucket) if s.sid == sid), None)
        else:
            idx = len(bucket) - 2 if len(bucket) >= 2 else 0
        if idx is None:
            return False, f"没找到版本 {sid}。"
        snap = bucket[idx]
        if not self.state.load_group_state(gid, snap.state):
            return False, "版本数据损坏，回滚失败。"
        # 回滚本身也算一次变更：截断之后的版本，并把当前状态记为新版本
        del bucket[idx + 1:]
        self.audit.setdefault(gid, []).append(AuditEntry(
            aid=_new_id("a"), group_id=gid, created=now,
            signal="rollback", polarity=0, weight=0,
            note=f"回滚到版本 {snap.sid}（{snap.reason}）",
        ))
        self._trim(self.audit, gid, MAX_AUDIT)
        return True, f"已回滚到版本 {snap.sid}（{snap.reason}）。"

    # ---------- 审计 ---------- #

    def log_audit(self, entry: AuditEntry) -> None:
        gid = str(entry.group_id or "")
        self.audit.setdefault(gid, []).append(entry)
        self._trim(self.audit, gid, MAX_AUDIT)

    def recent_audit(self, group_id: str, limit: int = 20) -> list[AuditEntry]:
        return list(reversed(self.audit.get(str(group_id or ""), [])))[: max(0, limit)]

    # ---------- 待批区 ---------- #

    def add_pending(self, item: PendingItem) -> PendingItem:
        gid = str(item.group_id or "")
        if not item.pid:
            item.pid = _new_id("p")
        bucket = self.pending.setdefault(gid, [])
        bucket.append(item)
        self._trim(self.pending, gid, MAX_PENDING)
        return item

    def pending_items(self, group_id: str, *, status: str = "pending") -> list[PendingItem]:
        items = self.pending.get(str(group_id or ""), [])
        if status == "all":
            return list(items)
        return [p for p in items if p.status == status]

    def resolve_pending(self, group_id: str, pid: str, status: str) -> PendingItem | None:
        for p in self.pending.get(str(group_id or ""), []):
            if p.pid == pid:
                p.status = status
                return p
        return None

    # ---------- 其它 ---------- #

    @staticmethod
    def _trim(mapping: dict, key: str, limit: int) -> None:
        bucket = mapping.get(key)
        if bucket and len(bucket) > limit:
            del bucket[: len(bucket) - limit]

    def reset_group(self, group_id: str) -> dict:
        """彻底清空某群的学习数据（含历史与审计）。"""
        gid = str(group_id or "")
        n_mem = self.state.memory.reset_group(gid)
        self.state.bandit.reset_group(gid)
        n_snap = len(self.snapshots.pop(gid, []))
        n_audit = len(self.audit.pop(gid, []))
        n_pending = len(self.pending.pop(gid, []))
        return {"memory": n_mem, "snapshots": n_snap, "audit": n_audit, "pending": n_pending}

    def bump_stat(self, group_id: str, key: str, delta: int = 1) -> None:
        gid = str(group_id or "")
        bucket = self.stats.setdefault(gid, {})
        bucket[key] = int(bucket.get(key, 0) or 0) + delta

    def group_stats(self, group_id: str) -> dict:
        return dict(self.stats.get(str(group_id or ""), {}))

    def set_flag(self, key: str, value) -> None:
        self.flags[str(key)] = value

    def get_flag(self, key: str, default=None):
        return self.flags.get(str(key), default)

    def export_group(self, group_id: str) -> dict:
        """导出某群数据（面板/命令用），快照只带摘要避免体积爆炸。"""
        gid = str(group_id or "")
        return {
            "schema": SCHEMA_VERSION,
            "group_id": gid,
            "exported_at": time.time(),
            "learning": self.state.group_state(gid),
            "snapshots": [{k: v for k, v in s.to_dict().items() if k != "state"}
                          for s in self.snapshots.get(gid, [])],
            "audit": [a.to_dict() for a in self.audit.get(gid, [])],
            "pending": [p.to_dict() for p in self.pending.get(gid, [])],
            "stats": self.group_stats(gid),
        }
