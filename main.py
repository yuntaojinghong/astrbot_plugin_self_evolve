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

import asyncio
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
    SOURCE_ADMIN,
    SOURCE_REFLECT,
    SOURCE_USER,
    Candidate,
    Choice,
    build_prompt,
    build_transcript,
    parse_candidates,
    parse_feedback,
    render_injection,
    render_summary,
    summarize,
    verify_all,
)
from .learning.feedback import SIG_NONE
from .config_service import ConfigService
from .store import AuditEntry, LearnStore, PendingItem, _new_id

__version__ = "0.5.1"

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
        # 保留 AstrBot 传入的**原始配置对象**，不要替换成新 dict：
        # 面板要就地改它并调 save_config() 才能持久化（替换掉就没有那个方法了）。
        # 缺省值由 _cfg() 在读取时兜底，因此这里不需要预先合并。
        self._raw_config = config
        self.config = config if isinstance(config, dict) else self._merge_config(config)
        self.config_service = ConfigService(self)
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
        # {group_id: [{"who","text"}]} 最近互动片段，供反思使用（有上限）
        self._history = {}
        # 自动反思后台任务
        self._reflect_task: asyncio.Task | None = None
        # 配置面板（WebUI Pages）
        self.web = None
        self._register_pages(context)

    def _register_pages(self, context: Context) -> None:
        """注册 WebUI 配置面板。

        低版本 AstrBot 不支持插件 Pages，此时静默降级，
        不影响学习与注入功能（阶段一、二完全可用）。
        """
        try:
            from .pages_api import SelfEvolveWeb

            self.web = SelfEvolveWeb(context, self)
            self.web.register_routes()
        except Exception as e:
            logger.warning("[自进化] 配置面板注册失败（不影响学习功能）: %s", e)
            self.web = None

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
        "history_limit": 60,
        "reflect_max_candidates": 5,
        "reflect_provider_id": "",
        "auto_reflect": False,
        "auto_reflect_minutes": 720,
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

    # ------------------------------------------------------------------ #
    #  配置读取 / 重载（面板内改配置用）
    # ------------------------------------------------------------------ #

    def plugin_dir(self) -> str:
        """插件所在目录（_conf_schema.json 在这里）。"""
        return os.path.dirname(os.path.abspath(__file__))

    def reload_runtime_config(self) -> None:
        """按当前配置更新运行时组件参数。

        学习率、硬上限、半衰期、容量这些是在组件**构造时**读进实例的，
        所以面板改完配置必须刷新，否则要等重载插件才生效。

        这里**不吞异常**：刷新失败必须让面板看到，否则用户会以为
        "保存成功、已生效"，实际还在用旧参数跑。曾因为吞掉异常
        （用了个不存在的序列化方法）导致配置改了却静默无效。
        """
        self.db.rebuild(
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

    def preview_injection(self, gid: str, probe: str = "") -> dict:
        """预览：给这个群 + 这句话，实际会注入什么。

        让用户直接看到"学习结果长什么样"，而不是靠猜参数含义。
        """
        mem = self.db.state.memory
        choice = self.db.state.bandit.choose(gid)
        entries = mem.retrieve(gid, probe, limit=self._cfg_int("inject_max_entries", 5))
        result = render_injection(
            entries=entries,
            choice=choice,
            max_chars=self._cfg_int("inject_max_chars", DEFAULT_MAX_INJECT),
        )
        return {
            "group_id": gid,
            "probe": probe,
            "active": self._cfg_bool("enabled") and self._cfg_bool("inject_enabled"),
            "choice": choice.to_dict(),
            "choice_desc": choice.describe(),
            "fallback": choice.fallback,
            "entries": [
                {"eid": e.eid, "content": e.content, "kind": e.kind,
                 "confidence": round(e.effective_confidence(mem.half_life_days), 3)}
                for e in entries
            ],
            "blocked": result.blocked,
            "style_notes": result.style_notes,
            "text": result.text,
        }

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
        if self._cfg_bool("auto_reflect"):
            await self.start_auto_reflect()

    async def terminate(self):
        await self.stop_auto_reflect()
        try:
            await self.db.save()
        except Exception as e:
            logger.warning("[自进化] 退出保存失败: %s", e)

    # ------------------------------------------------------------------ #
    #  自动反思（阶段二）
    # ------------------------------------------------------------------ #

    async def start_auto_reflect(self) -> None:
        if self._reflect_task and not self._reflect_task.done():
            return
        self._reflect_task = asyncio.create_task(self._auto_reflect_loop())
        logger.info("[自进化] 自动反思已启动，间隔 %s 分钟",
                    self._cfg_int("auto_reflect_minutes", 720))

    async def stop_auto_reflect(self) -> None:
        task = self._reflect_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reflect_task = None

    async def _auto_reflect_loop(self) -> None:
        """按间隔为「最近有互动」的群产出待批候选。

        注意：这里只**产出候选**，不会自动生效——生效必须经管理员审批。
        反思会消耗 token，因此只在确有新互动时才调用。
        """
        while True:
            try:
                minutes = max(5, self._cfg_int("auto_reflect_minutes", 720) or 720)
                await asyncio.sleep(minutes * 60)
                if not self._cfg_bool("enabled") or not self._cfg_bool("auto_reflect"):
                    continue
                cutoff = time.time() - minutes * 60
                for gid, bucket in list(self._history.items()):
                    if not bucket:
                        continue
                    if float(bucket[-1].get("ts") or 0) < cutoff:
                        continue
                    if self.db.pending_items(gid, status="pending"):
                        continue    # 上一次的还没审，先别堆
                    result = await self.run_reflection(gid)
                    logger.info("[自进化] 自动反思 %s: %s", gid, result.splitlines()[0])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[自进化] 自动反思异常（已忽略）: %s", e)

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
        """消息是否已被**其它插件**消费（磐石/树洞等），因而不适合当学习素材。

        .. important::
            这里**不能**用 ``is_stopped()`` 来判断。

            AstrBot 对「不需要机器人回复的普通群消息」本来就会 ``stop_event()``
            —— 那是它的正常流程，含义是"这条不用走 LLM"，**不是**"别的插件
            处理过了"。而这类不 @ 机器人的跟进（「哈哈哈」「好」「+1」）
            恰恰是隐式反馈最主要的来源。

            实测：同一条「哈哈哈」，事件未 stop 时捕获到 feedback=1，
            被 stop 后捕获 0；连喂 6 条则 6 : 0。也就是说旧逻辑把最常见的
            学习素材全部丢掉了 —— 用户看到的就是「装了几天什么都没捕捉到」。

            现在只认**伙伴插件显式留下的标记**；读不到就返回 False
            （宁可不判，也不误判导致漏学）。
        """
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            for key in ("panshi.consumed", f"{EXTRA_NAMESPACE}.consumed"):
                try:
                    if get_extra(key, False):
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

        .. important::
            **「选策略并记为待归因」与「是否注入」必须解耦。**

            此前 ``inject_enabled=False`` 时本方法直接 return，连
            ``_remember_choice()`` 都不执行 —— 于是 ``pending`` 永远不置位，
            ``on_user_message`` 在 `not pending` 处直接返回，**学习彻底停止**。
            而面板对这个开关的说明是「关闭后仍然继续学习（可在面板观察），
            只是不注入」，与实际行为完全相反。用户看到的正是
            「装了半天捕捉不到任何东西」。

            现在：无论是否注入，都先记录本次选用的策略；
            只有「把内容塞进请求」这一步受 ``inject_enabled`` 控制。
        """
        try:
            await self._ensure_loaded()
            if self._is_self_message(event):
                return
            gid = self._group_id(event)
            if not self._group_enabled(gid):
                return
            if req is None:
                return

            # ---- 选策略（含样本门槛：未达标则回退基线）----
            choice = self.db.state.bandit.choose(gid)

            prompt = ""
            try:
                prompt = str(getattr(req, "prompt", "") or "")
            except Exception:
                prompt = ""

            # ---- 关键：先把待归因状态记下来，这一步**不受 inject_enabled 影响** ----
            self._remember_choice(event, gid, choice, prompt)

            # ---- 以下才是「注入」，可被开关关掉 ----
            if not self._cfg_bool("inject_enabled"):
                return
            if not _HAS_TEXTPART:
                return

            # ---- 检索相关经验 ----
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
                # 什么都没学到：不注入，但待归因状态已在上面记好，学习照常
                return

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                logger.debug("[自进化] 当前请求对象不支持 extra_user_content_parts，跳过注入")
                return
            try:
                parts.append(TextPart(text=rendered.text))
            except Exception as e:
                logger.warning("[自进化] 注入失败（不影响正常回复）: %s", e)
                return

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
            self._append_history(gid, "bot", text)
            self.db.bump_stat(gid, "replies")
        except Exception as e:
            logger.debug("[自进化] 发送后钩子异常（已忽略）: %s", e)

    def _append_history(self, gid: str, who: str, text: str) -> None:
        """记录最近互动片段（反思的素材），带条数与单条长度上限。"""
        if not gid or not text:
            return
        bucket = self._history.setdefault(gid, [])
        bucket.append({"who": who, "text": str(text)[:200], "ts": time.time()})
        limit = self._cfg_int("history_limit", 60) or 60
        if len(bucket) > limit:
            del bucket[: len(bucket) - limit]

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
            if not consumed:
                self._append_history(gid, "user", text)

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
    #  反思（阶段二）：让模型复盘，产出候选 → 待批区
    # ------------------------------------------------------------------ #

    async def _call_reflection(self, prompt: str) -> tuple[str, str]:
        """调用 AstrBot 已配置的模型做一次复盘。

        Returns:
            ``(模型输出, 错误说明)``。没有可用模型时输出为空并给出原因。
        """
        generate = getattr(self.context, "llm_generate", None)
        if not callable(generate):
            return "", "当前 AstrBot 版本不支持 llm_generate，无法反思（学习本身不受影响）"
        provider_id = str(self._cfg("reflect_provider_id") or "").strip()
        if not provider_id:
            try:
                providers = self.context.get_all_providers() or []
            except Exception:
                providers = []
            if not providers:
                return "", "没有可用的对话模型，无法反思（可在 AstrBot「服务提供商」里配置）"
            try:
                provider_id = getattr(providers[0], "provider_config", {}).get("id", "") or ""
            except Exception:
                provider_id = ""
            if not provider_id:
                try:
                    provider_id = str(getattr(providers[0], "meta", lambda: None)() or "")
                except Exception:
                    provider_id = ""
        try:
            resp = await generate(chat_provider_id=provider_id, prompt=prompt)
            return str(getattr(resp, "completion_text", "") or ""), ""
        except TypeError:
            # 老版本签名不接受关键字
            try:
                resp = await generate(provider_id, prompt)
                return str(getattr(resp, "completion_text", "") or ""), ""
            except Exception as e:
                return "", f"调用模型失败：{e}"
        except Exception as e:
            return "", f"调用模型失败：{e}"

    def _recent_corrections(self, gid: str) -> list[str]:
        out = []
        for a in self.db.recent_audit(gid, limit=40):
            if a.correction:
                out.append(a.correction)
        return out

    async def run_reflection(self, gid: str) -> str:
        """执行一次反思并把通过的候选放进待批区。返回给用户看的说明。"""
        transcript = build_transcript(self._history.get(gid, []))
        if len(transcript) < 20:
            return "🤔 本群可用的互动片段太少，暂时没什么可复盘的。\n（多聊几轮、或积累一些反馈后再试。）"

        known = [e.content for e in self.db.state.memory.entries(gid)]
        signals = {k.replace("signal_", ""): v for k, v in self.db.group_stats(gid).items()
                   if k.startswith("signal_")}
        prompt = build_prompt(
            transcript=transcript,
            corrections=self._recent_corrections(gid),
            signals=signals,
            known=known,
        )
        raw, err = await self._call_reflection(prompt)
        if err:
            return f"⚠️ {err}"
        cands, note = parse_candidates(raw, limit=self._cfg_int("reflect_max_candidates", 5) or 5)
        if not cands:
            return f"🤔 本次反思没有产出可用候选（{note}）。"

        cands = verify_all(cands, store=self.db.state.memory, group_id=gid)
        accepted = [c for c in cands if c.accepted]
        self.db.bump_stat(gid, "reflections")

        for c in accepted:
            self.db.add_pending(PendingItem(
                pid=_new_id("p"), group_id=gid, created=time.time(),
                kind="entry",
                payload={"content": c.content, "kind": c.kind, "confidence": c.confidence},
                reason=f"反思候选（置信 {c.confidence:.2f}）",
            ))
        await self.db.maybe_save()

        lines = ["🧪 反思完成（结果未生效，需审批）", "", summarize(cands), ""]
        if accepted:
            lines.append(f"共 {len(accepted)} 条进入待批区。「进化 审批」查看，「进化 通过 <编号>」采纳。")
        else:
            lines.append("本次没有候选通过校验（原因见上）。")
        return "\n".join(lines)

    async def _pending_text(self, gid: str) -> str:
        items = self.db.pending_items(gid, status="pending")
        if not items:
            return ("📭 待批区是空的。\n"
                    "用「进化 反思」让模型复盘最近的互动，产出值得记住的候选。")
        lines = ["📥 待批候选（批准后才会生效）"]
        for i, it in enumerate(items, 1):
            p = it.payload or {}
            if it.kind == "entry":
                label = KIND_LABEL.get(str(p.get("kind")), str(p.get("kind")))
                lines.append(f"{i}. [{label}] {p.get('content')}")
                lines.append(f"    置信 {p.get('confidence')} · {it.reason}")
            else:
                lines.append(f"{i}. [{it.kind}] {p} —— {it.reason}")
            lines.append(f"    编号 {it.pid}")
        lines.append("")
        lines.append("用法：「进化 通过 <编号>」采纳 /「进化 驳回 <编号>」丢弃 /「进化 通过 全部」")
        return "\n".join(lines)

    async def _approve_text(self, gid: str, rest: str) -> str:
        pending = self.db.pending_items(gid, status="pending")
        if not pending:
            return "📭 待批区是空的，没有可采纳的候选。"
        target = (rest or "").strip()
        if target in ("全部", "all"):
            chosen = pending
        else:
            match = self._pick_pending(pending, target)
            if match is None:
                return f"没找到候选「{target}」。用「进化 审批」查看编号。"
            chosen = [match]

        added, merged = 0, 0
        for it in chosen:
            p = it.payload or {}
            if it.kind == "entry" and p.get("content"):
                res = self.db.state.memory.add(Entry(
                    content=str(p["content"]),
                    kind=str(p.get("kind") or "fact"),
                    group_id=gid,
                    confidence=float(p.get("confidence", 0.5) or 0.5),
                    source=SOURCE_REFLECT,
                ))
                added += 1 if res.created else 0
                merged += 1 if res.merged else 0
            self.db.resolve_pending(gid, it.pid, "approved")

        if self._cfg_bool("auto_snapshot"):
            self.db.push_snapshot(gid, f"采纳 {len(chosen)} 条反思候选",
                                  {"memory": f"+{added}", "merged": merged})
        self.db.bump_stat(gid, "approved", len(chosen))
        await self.db.maybe_save()
        return (f"✅ 已采纳 {len(chosen)} 条候选：新增 {added} 条、与已有条目合并 {merged} 条。\n"
                "下次对话起生效，可用「进化 回滚」撤销。")

    async def _reject_text(self, gid: str, rest: str) -> str:
        pending = self.db.pending_items(gid, status="pending")
        if not pending:
            return "📭 待批区是空的。"
        target = (rest or "").strip()
        if target in ("全部", "all"):
            chosen = pending
        else:
            match = self._pick_pending(pending, target)
            if match is None:
                return f"没找到候选「{target}」。用「进化 审批」查看编号。"
            chosen = [match]
        for it in chosen:
            self.db.resolve_pending(gid, it.pid, "rejected")
        await self.db.maybe_save()
        return f"🗑 已驳回 {len(chosen)} 条候选。"

    @staticmethod
    def _pick_pending(pending: list, target: str):
        """按序号或编号前缀挑一个待批项。"""
        if not target:
            return None
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(pending):
                return pending[idx]
        for it in pending:
            if it.pid == target or it.pid.endswith(target):
                return it
        # 退化为内容片段匹配
        low = target.lower()
        for it in pending:
            content = str((it.payload or {}).get("content") or "").lower()
            if low and low in content:
                return it
        return None

    # ------------------------------------------------------------------ #
    #  面板数据（阶段三）
    # ------------------------------------------------------------------ #

    def _is_paused(self) -> bool:
        return float(self.db.get_flag("paused_until") or 0) > time.time()

    def _known_groups(self) -> list[str]:
        """所有有学习痕迹的群（策略表 / 经验库 / 审计 / 待批任一处有数据）。"""
        gids = set(self.db.state.bandit.table) | set(self.db.state.memory.groups)
        gids |= set(self.db.audit) | set(self.db.pending) | set(self.db.snapshots)
        gids |= set(self.db.stats)
        return sorted(g for g in gids if g)

    def panel_groups(self) -> list[dict]:
        """面板首页：每个群一行摘要。"""
        rows: list[dict] = []
        for gid in self._known_groups():
            stats = self.db.state.memory.stats(gid)
            gstats = self.db.group_stats(gid)
            rows.append({
                "group_id": gid,
                "entries_alive": stats.get("alive", 0),
                "entries_total": stats.get("total", 0),
                "pending": len(self.db.pending_items(gid, status="pending")),
                "feedback": int(gstats.get("feedback", 0) or 0),
                "injections": int(gstats.get("injections", 0) or 0),
                "replies": int(gstats.get("replies", 0) or 0),
                "snapshots": len(self.db.snapshots.get(gid, [])),
                "blocked": int(gstats.get("blocked_injections", 0) or 0),
                "last_audit": max((a.created for a in self.db.audit.get(gid, [])), default=0),
            })
        rows.sort(key=lambda r: r["last_audit"], reverse=True)
        return rows

    def panel_group_detail(self, gid: str) -> dict:
        """面板详情：某群学到了什么、为什么、待批什么、有哪些版本。"""
        mem = self.db.state.memory
        rows = self.db.state.bandit.explain(gid)
        entries = []
        for e in mem.retrieve(gid, "", limit=200, min_score=0.0):
            entries.append({
                **e.to_dict(),
                "effective_confidence": round(
                    e.effective_confidence(mem.half_life_days), 4),
                "alive": e.is_alive(mem.half_life_days),
                "kind_label": KIND_LABEL.get(e.kind, e.kind),
            })
        pending = []
        for it in self.db.pending_items(gid, status="pending"):
            p = it.payload or {}
            pending.append({
                "pid": it.pid,
                "kind": it.kind,
                "content": p.get("content", ""),
                "entry_kind": p.get("kind", ""),
                "kind_label": KIND_LABEL.get(str(p.get("kind")), str(p.get("kind"))),
                "confidence": p.get("confidence"),
                "reason": it.reason,
                "created": it.created,
            })
        audit = [{
            "aid": a.aid, "created": a.created, "signal": a.signal,
            "polarity": a.polarity, "weight": a.weight,
            "user_message": a.user_message, "bot_reply": a.bot_reply,
            "evidence": a.evidence, "correction": a.correction,
            "note": a.note, "choice": a.choice, "updates": a.updates,
        } for a in self.db.recent_audit(gid, limit=50)]
        history = [{
            "sid": s.sid, "reason": s.reason, "created": s.created,
            "summary": s.summary,
        } for s in self.db.history(gid)]
        return {
            "group_id": gid,
            "stats": {**mem.stats(gid), **self.db.group_stats(gid)},
            "dimensions": rows,
            "entries": entries,
            "pending": pending,
            "audit": audit,
            "history": history,
            "paused": self._is_paused(),
        }

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
        if sub in ("反思", "reflect"):
            yield event.plain_result(await self.run_reflection(gid))
            return
        if sub in ("审批", "待批", "pending"):
            yield event.plain_result(await self._pending_text(gid))
            return
        if sub in ("通过", "采纳", "approve"):
            yield event.plain_result(await self._approve_text(gid, rest))
            return
        if sub in ("驳回", "拒绝", "reject"):
            yield event.plain_result(await self._reject_text(gid, rest))
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
        lines.append("子命令：记忆 / 审计 / 反思 / 审批 / 遗忘 / 回滚 / 开关 / 导出 / 重置")
        pending = len(self.db.pending_items(gid, status="pending"))
        if pending:
            lines.append(f"📥 有 {pending} 条反思候选待审批（「进化 审批」查看）")
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
            "进化 反思          让模型复盘最近互动，产出待批候选\n"
            "进化 审批          查看待批候选\n"
            "进化 通过 <编号>   采纳候选（可写「全部」）\n"
            "进化 驳回 <编号>   丢弃候选（可写「全部」）\n"
            "进化 遗忘 <编号>   删除某条经验\n"
            "进化 回滚          退回上一个版本\n"
            "进化 开关 开|关    暂停/恢复学习与注入\n"
            "进化 导出          导出本群学习数据\n"
            "进化 重置 确认     清空本群学习数据\n"
            "━━━━━━━━━━━━━━\n"
            "说明：本插件只在**真正调用模型**的对话里学习，不抢答、不改身份；\n"
            "所有调整都有上限、有依据、可回滚；反思产出必须审批后才生效。"
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
