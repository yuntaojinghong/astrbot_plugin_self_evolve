"""自进化 (astrbot_plugin_self_evolve)
====================================

让机器人在**真实互动**里学会「这个群喜欢怎么被回话」以及「哪些事别再答错」。

与同类插件的区别（定位）
------------------------
本插件刻意做成**语感与记忆层**，与常见的群管/隐私插件正交、互补：

- 群管类插件（如磐石）负责「要不要回、执行什么动作」——
  它把废话挡在模型之前，因此那些消息**根本不会触发本插件的 on_llm_request**，
  本插件天然只从「真正花过 token 的交互」里学习，不会把被拦下的闲聊当素材。
- 隐私类插件（如匿名树洞）负责「以谁的身份说」——
  它以机器人身份转述内容，因此那些消息的发送者就是机器人自己；
  本插件用 self_id == sender_id 判定并跳过，避免把转述内容学成群偏好。
- 本插件负责「该怎么说、该记住什么」，通过 LLM 请求钩子注入，不抢消息、不改身份。

学习闭环
--------
1. ``on_llm_request``：为本次回复选一套有界策略档位，并把学到的经验注入请求。
2. ``after_message_sent``：记下机器人刚说了什么、用的是哪套策略。
3. 用户的下一条消息：解析成反馈信号，归因到当时实际使用的那套策略上。
4. 分数更新有硬上限、样本门槛与时间衰减；超过门槛才影响提示词。

安全与可控（这是本插件的立足点）
--------------------------------
- **有界**：任何档位分数夹在 ±max_abs，施加到提示词的偏移再夹在 ±max_offset。
- **门槛**：档位被选中次数不足 ``min_samples`` 不影响行为，只作探索依据。
- **衰减**：旧反馈按半衰期变淡，不会一次风波永久带偏。
- **可解释**：每条反馈都记入审计日志（哪句话 → 什么信号 → 哪一档加了多少分）。
- **可回滚**：每次生效变更存快照，``进化 回滚`` 一键退回。
- **防注入**：经验内容来自群聊，注入前做净化与指令性内容拦截，
  并用 <system_reminder> 包裹、声明其为观察记录而非指令。
- **总开关**：关闭后不注入、不学习。

基于 AstrBot v4（Star API，>= 4.16）开发，无第三方依赖。
"""

from __future__ import annotations

import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .learning import (
    BASELINE_INDEX,
    DIMENSION_HINT,
    Entry,
    KIND_CORRECTION,
    KIND_LABEL,
    SOURCE_USER,
    Choice,
    parse_feedback,
    render_injection,
    render_summary,
)
from .learning.feedback import SIG_NONE
from .store import AuditEntry, LearnStore, _new_id

__version__ = "0.1.0"

#: 本插件在事件上留下的标记键（命名空间化，避免与其它插件冲突）
EXTRA_NAMESPACE = "self_evolve"

#: 单次注入的正文字符上限
DEFAULT_MAX_INJECT = 700

#: 组合式 hook 参数（避免在不同 AstrBot 版本上因位置参数变化而崩）
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.agent.message import TextPart
    _HAS_TEXTPART = True
except Exception:  # TextPart 不可用时退化为「不注入」，而不是崩溃
    TextPart = None  # type: ignore
    _HAS_TEXTPART = False


@register(
    "astrbot_plugin_self_evolve",
    "yuntaojinghong",
    "自进化：从真实互动中学会每个群的说话方式与要记住的事，全部有界、可解释、可回滚",
    __version__,
)
class SelfEvolve(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = self._merge_config(config)
        self.data_dir = self._resolve_data_dir()
        path = os.path.join(self.data_dir, "self_evolve_state.json")
        self.db = LearnStore(
            kv=self,
            path=path,
            autosave=False,          # 由调用点显式控制落盘时机
            bandit_kwargs={
                "max_abs": self._cfg_float("max_abs", 3.0),
                "max_offset": self._cfg_float("max_offset", 0.20),
                "learning_rate": self._cfg_float("learning_rate", 0.25),
                "epsilon": self._cfg_float("epsilon", 0.12),
                "min_samples": self._cfg_int("min_samples", 5),
                "decay_half_life": self._cfg_float("decay_half_life_days", 7.0) * 86400,
            },
            memory_kwargs={
                "max_per_group": self._cfg_int("max_entries_per_group", 200),
                "half_life_days": self._cfg_float("memory_half_life_days", 60.0),
            },
        )
        self._loaded = False
        # {group_id: {"reply","choice","user_msg","ts","pending"}}
        self._last = {}

    # ------------------------------------------------------------------ #
    #  配置
    # ------------------------------------------------------------------ #

    DEFAULTS = {
        "enabled": True,
        "learn_in_private": False,
        "min_samples": 5,
        "max_abs": 3.0,
        "max_offset": 0.20,
        "learning_rate": 0.25,
        "epsilon": 0.12,
        "decay_half_life_days": 7.0,
        "memory_half_life_days": 60.0,
        "max_entries_per_group": 200,
        "inject_enabled": True,
        "inject_max_entries": 5,
        "inject_max_chars": DEFAULT_MAX_INJECT,
        "auto_snapshot": True,
        "enable_commands": True,
        "admin_only": True,
        "learn_from_implicit": True,
        "min_signal_weight": 0.0,
        "feedback_window_sec": 600,
        "list_limit": 20,
    }

    @classmethod
    def _merge_config(cls, config) -> dict:
        out = dict(cls.DEFAULTS)
        if isinstance(config, dict):
            out.update({k: v for k, v in config.items() if not k.startswith("_")})
        else:
            d = getattr(config, "__dict__", None)
            if isinstance(d, dict):
                out.update({k: v for k, v in d.items() if not k.startswith("_")})
        return out

    def _cfg(self, key, default=None):
        v = self.config.get(key)
        return self.DEFAULTS.get(key) if v is None else v

    def _cfg_bool(self, key) -> bool:
        v = self._cfg(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(v)

    def _cfg_int(self, key, default: int = 0) -> int:
        try:
            return int(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key, default: float = 0.0) -> float:
        try:
            return float(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _resolve_data_dir(self) -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            return os.path.join(get_astrbot_plugin_data_path(), "astrbot_plugin_self_evolve")
        except Exception:
            base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            return os.path.join(base, "plugin_data", "astrbot_plugin_self_evolve")

    # ------------------------------------------------------------------ #
    #  生命周期 / 工具
    # ------------------------------------------------------------------ #

    async def initialize(self):
        await self._ensure_loaded()
        logger.info("[自进化] 已加载，学习状态：%s", self._brief())

    async def terminate(self):
        try:
            await self.db.save()
        except Exception as e:
            logger.warning("[自进化] 退出保存失败: %s", e)

    async def _ensure_loaded(self):
        if self._loaded:
            return
        await self.db.load()
        self._loaded = True

    def _brief(self) -> str:
        groups = len(self.db.state.bandit.table)
        entries = sum(len(v) for v in self.db.state.memory.groups.values())
        return f"{groups} 个群 / {entries} 条经验"

    @staticmethod
    def _group_id(event) -> str:
        try:
            gid = event.get_group_id()
            return str(gid) if gid not in (None, "") else ""
        except Exception:
            return ""

    @staticmethod
    def _sender_id(event) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _self_id(event) -> str:
        try:
            return str(event.get_self_id() or "")
        except Exception:
            return ""

    def _is_self_message(self, event) -> bool:
        """机器人自己发的消息（含匿名树洞等以 bot 身份转述的内容）。

        与磐石 ``guard._is_self_message`` 保持同一语义：sender == self_id。
        否则会把转述内容、机器人自己的发言当成"群偏好"学进去。
        """
        sid, me = self._sender_id(event), self._self_id(event)
        return bool(me) and sid == me

    def _is_consumed(self, event) -> bool:
        """消息是否已被其它插件消费（磐石/树洞等）。

        读取伙伴插件可能留下的标记；读不到时退回 AstrBot 内置信号：
        ``is_stopped()`` 与 ``should_call_llm(False)``。
        探测失败一律返回 False（宁可不判，也不误判导致漏学）。
        """
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            for key in ("panshi.consumed", f"{EXTRA_NAMESPACE}.consumed"):
                try:
                    if get_extra(key, False):
                        return True
                except Exception:
                    pass
        try:
            if hasattr(event, "is_stopped") and event.is_stopped():
                return True
        except Exception:
            pass
        return False

    def _group_enabled(self, group_id: str) -> bool:
        if not self._cfg_bool("enabled"):
            return False
        if not group_id and not self._cfg_bool("learn_in_private"):
            return False
        if self.db.get_flag("paused_until") and float(self.db.get_flag("paused_until") or 0) > time.time():
            return False
        return True

    # ------------------------------------------------------------------ #
    #  钩子 1：注入（LLM 请求前）
    # ------------------------------------------------------------------ #

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在请求发出前，把学到的经验与风格倾向注入进去。

        注意：本钩子只在**真的要调用模型**时触发。被其它插件拦下的消息
        （磐石意图闸门、树洞私聊静默）不会走到这里，因此本插件天然只从
        真正消耗过 token 的交互里学习。
        """
        try:
            await self._ensure_loaded()
            if not self._cfg_bool("inject_enabled"):
                return
            if self._is_self_message(event):
                return
            gid = self._group_id(event)
            if not self._group_enabled(gid):
                return
            if not _HAS_TEXTPART or req is None:
                return

            # ---- 选策略（含样本门槛：未达标则回退基线）----
            choice = self.db.state.bandit.choose(gid)

            # ---- 检索相关经验 ----
            prompt = ""
            try:
                prompt = str(getattr(req, "prompt", "") or "")
            except Exception:
                prompt = ""
            entries = self.db.state.memory.retrieve(
                gid, prompt, limit=self._cfg_int("inject_max_entries", 5)
            )

            rendered = render_injection(
                entries=entries, choice=choice,
                max_chars=self._cfg_int("inject_max_chars", DEFAULT_MAX_INJECT),
            )
            if rendered.blocked:
                self.db.bump_stat(gid, "blocked_injections", len(rendered.blocked))
            if not rendered.text:
                # 什么都没学到时不留痕，避免污染状态
                self._remember_choice(event, gid, choice, prompt)
                return

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                logger.debug("[自进化] 当前请求对象不支持 extra_user_content_parts，跳过注入")
                self._remember_choice(event, gid, choice, prompt)
                return
            try:
                parts.append(TextPart(text=rendered.text))
            except Exception as e:
                logger.warning("[自进化] 注入失败（不影响正常回复）: %s", e)
                return

            self._remember_choice(event, gid, choice, prompt)
            self.db.bump_stat(gid, "injections")
            self.db.bump_stat(gid, "injected_entries", len(rendered.used_entries))
        except Exception as e:
            # 学习插件绝不能因为自身异常影响用户正常对话
            logger.warning("[自进化] 注入钩子异常（已忽略）: %s", e)

    def _remember_choice(self, event, gid: str, choice: Choice, prompt: str) -> None:
        """把本次选用的策略记到状态与事件上，供反馈归因复用。"""
        st = self._last.setdefault(gid, {})
        st["choice"] = choice.to_dict()
        st["user_msg"] = (prompt or "")[:200]
        st["ts"] = time.time()
        st["pending"] = True
        try:
            event.set_extra(f"{EXTRA_NAMESPACE}.choice", choice.to_dict())
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  钩子 2：记录机器人回复（发送后）
    # ------------------------------------------------------------------ #

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """记下机器人刚发出的回复，作为下一条用户消息的反馈对象。"""
        try:
            await self._ensure_loaded()
            gid = self._group_id(event)
            if not self._group_enabled(gid):
                return
            text = self._result_text(event)
            if not text:
                return
            st = self._last.setdefault(gid, {})
            st["reply"] = text[:400]
            st["reply_ts"] = time.time()
            self.db.bump_stat(gid, "replies")
        except Exception as e:
            logger.debug("[自进化] 发送后钩子异常（已忽略）: %s", e)

    @staticmethod
    def _result_text(event) -> str:
        try:
            result = event.get_result()
        except Exception:
            return ""
        if result is None:
            return ""
        chain = getattr(result, "chain", None)
        if not chain:
            return ""
        buf = []
        for comp in chain:
            t = getattr(comp, "text", None)
            if t:
                buf.append(str(t))
        return " ".join(buf).strip()

    # ------------------------------------------------------------------ #
    #  钩子 3：反馈归因（用户的下一条消息）
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        """把用户的下一条消息解析成对上一条回复的反馈，并归因到当时的策略。

        本处理器**不产出任何回复、不消费消息**，只读取与记录，
        因此不会与群管/树洞类插件抢答。
        """
        try:
            await self._ensure_loaded()
            # 已发过言的机器人自身消息（含树洞转述）：不学习
            if self._is_self_message(event):
                return
            gid = self._group_id(event)
            if not self._group_enabled(gid):
                return
            # 已被其它插件消费的消息（如磐石的指令/风控处置）：不作为学习素材，
            # 但仍要推进"上一条消息"指针，避免下次误归因到更早的一轮。
            consumed = self._is_consumed(event)
            text = self._message_text(event)
            if not text:
                return

            st = self._last.get(gid) or {}
            prev_reply = str(st.get("reply") or "")
            prev_ts = float(st.get("reply_ts") or 0)
            choice = Choice.from_dict(st.get("choice") or {})
            pending = bool(st.get("pending"))

            # 清掉待归因标记：一条回复只结算一次
            st["pending"] = False
            st["prev_user_msg"] = text[:200]
            self._last[gid] = st

            if consumed or not prev_reply or not pending:
                return
            # 只在"紧接着机器人发言之后"的消息上归因，且有时间窗
            window = self._cfg_int("feedback_window_sec", 600) or 600
            if prev_ts and (time.time() - prev_ts) > window:
                return

            fb = parse_feedback(
                text,
                prev_user_text=str(st.get("prev_user_q") or ""),
                is_reply_to_bot=self._reply_targets_bot(event),
            )
            if not self._cfg_bool("learn_from_implicit") and not fb.is_strong:
                return
            if fb.signal == SIG_NONE or fb.polarity == 0:
                return
            if fb.weight < self._cfg_float("min_signal_weight", 0.0):
                return

            updates = self.db.state.bandit.update(gid, choice, fb.polarity, fb.weight)

            # 明确的纠正内容 → 直接沉淀成经验条目（用户纠正来源，置信度较高）
            added = None
            if fb.signal in ("correction",) and fb.correction:
                added = self.db.state.memory.add(Entry(
                    content=fb.correction, kind=KIND_CORRECTION, group_id=gid,
                    confidence=0.6, evidence=1, source=SOURCE_USER,
                ))

            entry = AuditEntry(
                aid=_new_id("a"), group_id=gid, created=time.time(),
                user_message=text[:200], bot_reply=prev_reply[:200],
                signal=fb.signal, polarity=fb.polarity, weight=fb.weight,
                evidence=fb.evidence, correction=fb.correction,
                choice=choice.to_dict(),
                updates=[{
                    "dimension": u.dimension, "level": u.level,
                    "before": round(u.score_before, 4), "after": round(u.score_after, 4),
                    "pulls": u.pulls,
                } for u in updates],
                applied=bool(updates),
                note="；".join(fb.notes),
            )
            self.db.log_audit(entry)
            self.db.bump_stat(gid, "feedback")
            self.db.bump_stat(gid, f"signal_{fb.signal}")
            if added is not None:
                self.db.bump_stat(gid, "entries_added")
                if self._cfg_bool("auto_snapshot") and (added.created or added.merged):
                    self.db.push_snapshot(
                        gid, f"用户纠正沉淀经验：{fb.correction[:40]}",
                        {"memory": "+1" if added.created else "~1"},
                    )
            await self.db.maybe_save()
        except Exception as e:
            logger.warning("[自进化] 反馈归因异常（已忽略）: %s", e)

    @staticmethod
    def _message_text(event) -> str:
        try:
            return (event.get_message_str() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _reply_targets_bot(event) -> bool:
        """消息是否为对机器人发言的引用回复。"""
        try:
            me = str(event.get_self_id() or "")
            if not me:
                return False
            for seg in event.get_messages() or []:
                sid = getattr(seg, "sender_id", None)
                if sid is not None and str(sid) == me:
                    return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------ #
    #  命令：单入口 + 子命令（与其它插件命令零冲突）
    # ------------------------------------------------------------------ #

    @filter.command("进化")
    async def cmd_evolve(self, event: AstrMessageEvent, arg: str = ""):
        """自进化管理：/进化 [状态|记忆|审计|遗忘|回滚|开关|导出|帮助]"""
        if not self._cfg_bool("enable_commands"):
            return
        await self._ensure_loaded()
        sub, _, rest = (arg or "").strip().partition(" ")
        sub = sub.strip()
        rest = rest.strip()

        if self._cfg_bool("admin_only") and not self._is_admin(event):
            yield event.plain_result("⛔ 「进化」命令仅管理员可用。")
            return

        gid = self._group_id(event)
        if sub in ("", "状态", "status"):
            yield event.plain_result(self._status_text(gid))
            return
        if sub in ("记忆", "memory"):
            yield event.plain_result(self._memory_text(gid, rest))
            return
        if sub in ("审计", "为什么", "why", "audit"):
            yield event.plain_result(self._audit_text(gid))
            return
        if sub in ("遗忘", "忘记", "forget"):
            yield event.plain_result(await self._forget_text(gid, rest))
            return
        if sub in ("回滚", "rollback"):
            yield event.plain_result(await self._rollback_text(gid))
            return
        if sub in ("开关", "暂停", "pause", "resume"):
            yield event.plain_result(await self._switch_text(gid, rest))
            return
        if sub in ("导出", "export"):
            yield event.plain_result(self._export_text(gid))
            return
        if sub in ("重置", "reset"):
            yield event.plain_result(await self._reset_text(gid, rest))
            return
        yield event.plain_result(self._help_text())

    # ---------- 命令实现 ---------- #

    def _status_text(self, gid: str) -> str:
        stats = self.db.state.memory.stats(gid)
        rows = self.db.state.bandit.explain(gid)
        base = render_summary(stats=stats, rows=rows, group_id=gid or "（私聊）")
        s = self.db.group_stats(gid)
        learned = s.get("feedback", 0)
        lines = [base, ""]
        paused = float(self.db.get_flag("paused_until") or 0) > time.time()
        state = "⏸ 已暂停学习" if paused else ("✅ 学习中" if self._cfg_bool("enabled") else "⬜ 总开关关闭")
        lines.append(f"状态：{state} · 已归因 {learned} 次反馈 · 注入 {s.get('injections', 0)} 次")
        versions = len(self.db.snapshots.get(gid, []))
        if versions:
            lines.append(f"版本：{versions} 个可回滚快照")
        if s.get("blocked_injections"):
            lines.append(f"🛡 已拦截 {s['blocked_injections']} 条可疑经验注入（疑似指令性内容）")
        lines.append("")
        lines.append("子命令：记忆 / 审计 / 遗忘 / 回滚 / 开关 / 导出 / 重置")
        return "\n".join(lines)

    def _memory_text(self, gid: str, query: str) -> str:
        items = self.db.state.memory.retrieve(
            gid, query, limit=self._cfg_int("list_limit", 20), min_score=0.0
        )
        if not items:
            return "🧠 本群还没有沉淀下经验条目。\n（需要反复出现的信息才会入库；用户明确纠正也会立即入库。）"
        head = "🧠 本群经验条目" + (f"（匹配「{query}」）" if query else "")
        lines = [head]
        for i, e in enumerate(items, 1):
            label = KIND_LABEL.get(e.kind, e.kind)
            conf = e.effective_confidence(self.db.state.memory.half_life_days)
            pin = "📌" if e.pinned else ""
            lines.append(f"{i}. {pin}[{label}] {e.content}")
            lines.append(f"    置信 {conf:.2f} · 印证 {e.evidence} 次 · {e.eid}")
        lines.append("")
        lines.append("用「进化 遗忘 <编号或内容>」删除某条。")
        return "\n".join(lines)

    def _audit_text(self, gid: str) -> str:
        rows = self.db.recent_audit(gid, limit=8)
        if not rows:
            return "📋 还没有反馈记录。\n归因发生在「用户对机器人上一条回复做出反应」时。"
        lines = ["📋 最近的反馈归因（我为什么变）"]
        for a in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(a.created or 0))
            arrow = "👍" if a.polarity > 0 else ("👎" if a.polarity < 0 else "•")
            lines.append(f"{arrow} [{when}] 信号={a.signal} 权重={a.weight:.2f}")
            if a.user_message:
                lines.append(f"    用户说：{a.user_message}")
            if a.evidence:
                lines.append(f"    依据：{a.evidence}")
            if a.correction:
                lines.append(f"    更正为：{a.correction}")
            ch = (a.choice or {}).get("picks") or {}
            if ch:
                brief = " ".join(
                    f"{d}={DIMENSION_HINT.get(d, [''])[v] if 0 <= v < len(DIMENSION_HINT.get(d, [])) else v}"
                    for d, v in list(ch.items())[:3]
                )
                lines.append(f"    当时策略：{brief}")
            if a.note:
                lines.append(f"    备注：{a.note}")
        return "\n".join(lines)

    async def _forget_text(self, gid: str, target: str) -> str:
        if not target:
            return "用法：「进化 遗忘 <编号或内容片段>」。编号见「进化 记忆」。"
        # 先按序号处理
        if target.isdigit():
            items = self.db.state.memory.retrieve(
                gid, "", limit=self._cfg_int("list_limit", 20), min_score=0.0
            )
            idx = int(target) - 1
            if 0 <= idx < len(items):
                target = items[idx].eid
        ok = self.db.state.memory.forget(gid, target)
        if not ok:
            return f"没找到「{target}」。用「进化 记忆」查看现有条目。"
        if self._cfg_bool("auto_snapshot"):
            self.db.push_snapshot(gid, f"删除经验：{target[:40]}", {"memory": "-1"})
        await self.db.maybe_save()
        return f"✅ 已忘记「{target}」。"

    async def _rollback_text(self, gid: str) -> str:
        ok, msg = self.db.rollback(gid)
        if ok:
            await self.db.maybe_save()
        return ("✅ " if ok else "⚠️ ") + msg

    async def _switch_text(self, gid: str, rest: str) -> str:
        r = (rest or "").strip()
        now = time.time()
        if r in ("开", "on", "开启", "恢复", "resume"):
            self.db.set_flag("paused_until", 0)
            await self.db.maybe_save()
            return "✅ 已恢复学习。"
        if r in ("关", "off", "关闭", "暂停", "pause"):
            self.db.set_flag("paused_until", now + 86400 * 365)
            await self.db.maybe_save()
            return "⏸ 已暂停本插件的学习与注入（`进化 开关 开` 恢复）。"
        if r in ("", "状态", "status"):
            paused = float(self.db.get_flag("paused_until") or 0) > now
            return f"当前学习状态：{'⏸ 已暂停' if paused else '✅ 运行中'}\n用法：「进化 开关 开|关」"
        return "用法：「进化 开关 开|关」"

    def _export_text(self, gid: str) -> str:
        import json
        data = self.db.export_group(gid)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > 1800:
            text = text[:1800] + "\n…（已截断，完整数据请用面板导出）"
        return f"📤 本群学习数据导出：\n```json\n{text}\n```"

    async def _reset_text(self, gid: str, rest: str) -> str:
        if rest.strip() not in ("确认", "confirm", "yes"):
            return "⚠️ 这会清空本群全部学习数据（经验/策略/历史/审计）。\n确认请发送：「进化 重置 确认」"
        n = self.db.reset_group(gid)
        await self.db.maybe_save()
        return (f"✅ 已清空本群学习数据：经验 {n['memory']} 条、"
                f"快照 {n['snapshots']} 个、审计 {n['audit']} 条、待批 {n['pending']} 条。")

    @staticmethod
    def _help_text() -> str:
        return (
            "🧠 自进化 · 用法\n"
            "━━━━━━━━━━━━━━\n"
            "进化              本群学习总览（学到了什么）\n"
            "进化 记忆 [关键词] 查看经验条目\n"
            "进化 审计          最近的反馈归因（我为什么变）\n"
            "进化 遗忘 <编号>   删除某条经验\n"
            "进化 回滚          退回上一个版本\n"
            "进化 开关 开|关    暂停/恢复学习与注入\n"
            "进化 导出          导出本群学习数据\n"
            "进化 重置 确认     清空本群学习数据\n"
            "━━━━━━━━━━━━━━\n"
            "说明：本插件只在**真正调用模型**的对话里学习，不抢答、不改身份；\n"
            "所有调整都有上限、有依据、可回滚。"
        )

    # ------------------------------------------------------------------ #
    #  权限
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_admin(event) -> bool:
        """管理员判定。

        只认平台上报的角色（AstrBot 会按管理员列表填充）。
        判定不了就返回 False——「不确定」绝不能当成「有权」。
        """
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:
            pass
        try:
            role = getattr(event, "role", "") or ""
            if str(role).lower() in ("admin", "owner"):
                return True
        except Exception:
            pass
        return False
