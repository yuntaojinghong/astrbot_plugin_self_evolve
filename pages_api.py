"""面板后端 Web API。

通过 ``context.register_web_api()`` 注册路由，供 ``pages/settings/`` 下的前端页面
经 ``window.AstrBotPluginPage`` bridge 调用。

约定（与既有插件一致）：
- 注册的路由带插件名前缀 ``/astrbot_plugin_self_evolve/...``
- 前端 bridge 调用时**不带**前缀，例如 ``apiGet("groups")``
- 响应统一用 ``astrbot.api.web`` 的 ``json_response`` / ``error_response``

设计原则：面板只做**读 + 审批 + 回滚**这类显式动作，不提供"直接改策略分数"的入口——
分数是学习出来的，手改会破坏"可解释"这条底线。要人工干预就用经验条目与回滚。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger

from . import __version__ as PLUGIN_VERSION

PLUGIN_NAME = "astrbot_plugin_self_evolve"

try:
    from astrbot.api.web import error_response, json_response, request
except Exception as e:  # pragma: no cover - 取决于 AstrBot 版本
    _WEB_IMPORT_ERROR = str(e)
    json_response = None  # type: ignore
    error_response = None  # type: ignore
    request = None  # type: ignore


class SelfEvolveWeb:
    """注册并处理自进化面板的 Web API。"""

    def __init__(self, context: Any, plugin: Any):
        self.context = context
        self.plugin = plugin      # SelfEvolve 实例
        #: 路由是否注册成功。低版本 AstrBot 上为 False——
        #: 面板不可用，但学习/注入（阶段一、二）完全不受影响。
        self.registered = False

    # ==================================================================
    #  注册
    # ==================================================================
    def register_routes(self) -> bool:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[自进化] 当前 AstrBot 版本不支持插件 Pages（缺少 register_web_api），"
                "配置面板不可用；学习功能不受影响"
            )
            return False
        if request is None or json_response is None:
            logger.warning("[自进化] Web 请求模块不可用，跳过面板注册: %s",
                           globals().get("_WEB_IMPORT_ERROR", ""))
            return False

        routes: list[tuple[str, Callable, list[str], str]] = [
            ("/bootstrap", self.api_bootstrap, ["GET"], "面板初始化数据"),
            ("/overview", self.api_overview, ["GET"], "全部群学习概览"),
            ("/group", self.api_group, ["GET"], "单个群的学习详情"),
            ("/approve", self.api_approve, ["POST"], "采纳待批候选"),
            ("/reject", self.api_reject, ["POST"], "驳回待批候选"),
            ("/forget", self.api_forget, ["POST"], "删除经验条目"),
            ("/reset_group", self.api_reset_group, ["POST"], "清空并移除某个群的学习数据"),
            ("/rollback", self.api_rollback, ["POST"], "回滚到指定版本"),
            ("/switch", self.api_switch, ["POST"], "暂停/恢复学习"),
            ("/reflect", self.api_reflect, ["POST"], "立即执行一次反思"),
            ("/persona", self.api_persona_get, ["GET"], "读取某群的按群人设"),
            ("/persona", self.api_persona_set, ["POST"], "写入某群的按群人设"),
            ("/persona", self.api_persona_clear, ["DELETE"], "删除某群的按群人设（回到跟随全局）"),
            ("/export", self.api_export, ["GET"], "导出某群学习数据"),
            # 面板内改配置：省掉「面板 / AstrBot 原生配置页」来回跳
            ("/config", self.api_config_get, ["GET"], "读取可配置项与当前值"),
            ("/config", self.api_config_set, ["POST"], "写入配置改动"),
            ("/preview", self.api_preview, ["POST"], "预览实际会注入的内容"),
        ]

        ok = 0
        for path, handler, methods, desc in routes:
            try:
                register(f"/{PLUGIN_NAME}{path}", self._wrap(handler), methods, desc)
                ok += 1
            except Exception as e:
                logger.error("[自进化] 注册路由 %s 失败: %s", path, e)
        self._registered = ok > 0
        self.registered = self._registered
        if self._registered:
            logger.info("[自进化] 配置面板已注册 %s 个接口", ok)
        return self._registered

    def _wrap(self, handler: Callable) -> Callable:
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as e:
                return _err(str(e), 400)
            except Exception as e:  # pragma: no cover
                logger.exception("[自进化] 面板接口异常")
                return _err(f"服务器内部错误: {e}", 500)

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    # ==================================================================
    #  Handlers
    # ==================================================================
    async def api_bootstrap(self):
        plugin = self.plugin
        await plugin._ensure_loaded()
        return _ok({
            "version": PLUGIN_VERSION,
            "enabled": plugin._cfg_bool("enabled"),
            "inject_enabled": plugin._cfg_bool("inject_enabled"),
            "paused": plugin._is_paused(),
            "config": {
                "min_samples": plugin._cfg_int("min_samples", 5),
                "max_offset": plugin._cfg_float("max_offset", 0.20),
                "learning_rate": plugin._cfg_float("learning_rate", 0.25),
                "epsilon": plugin._cfg_float("epsilon", 0.12),
                "decay_half_life_days": plugin._cfg_float("decay_half_life_days", 7.0),
                "memory_half_life_days": plugin._cfg_float("memory_half_life_days", 60.0),
                "max_entries_per_group": plugin._cfg_int("max_entries_per_group", 200),
                "learn_from_implicit": plugin._cfg_bool("learn_from_implicit"),
                "auto_reflect": plugin._cfg_bool("auto_reflect"),
                "admin_only": plugin._cfg_bool("admin_only"),
            },
            "dimensions": _dimension_meta(),
        })

    async def api_overview(self):
        plugin = self.plugin
        await plugin._ensure_loaded()
        return _ok({"groups": plugin.panel_groups()})

    async def api_group(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.plugin.panel_group_detail(gid))

    async def api_approve(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        ids = payload.get("ids")
        if not gid:
            return _err("缺少参数 group_id", 400)
        if ids in (None, "all"):
            rest = "全部"
        elif isinstance(ids, list) and ids:
            rest = str(ids[0])
        else:
            return _err("缺少参数 ids", 400)
        return _ok({"message": await self.plugin._approve_text(gid, rest)})

    async def api_reject(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        ids = payload.get("ids")
        if not gid:
            return _err("缺少参数 group_id", 400)
        rest = "全部" if ids in (None, "all") else str(ids[0] if isinstance(ids, list) and ids else "")
        if not rest:
            return _err("缺少参数 ids", 400)
        return _ok({"message": await self.plugin._reject_text(gid, rest)})

    async def api_forget(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        eid = str(payload.get("eid") or "")
        if not gid or not eid:
            return _err("缺少参数 group_id 或 eid", 400)
        return _ok({"message": await self.plugin._forget_text(gid, eid)})

    async def api_reset_group(self):
        """清空并移除某个群的学习数据（面板上「删除这个会话」）。

        必须清 stats：``_known_groups()`` 会把 ``set(self.db.stats)`` 算进
        「有学习痕迹的群」，只清经验/策略的话，删完刷新它又回来了。
        """
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        if not gid:
            return _err("缺少参数 group_id", 400)
        removed = self.plugin.db.reset_group(gid)
        # 顺手丢掉内存里的待归因状态与互动片段，否则刚删完又立刻长回来
        try:
            self.plugin._last.pop(gid, None)
            self.plugin._history.pop(gid, None)
        except Exception:
            pass
        await self.plugin.db.save()
        still = gid in self.plugin._known_groups()
        total = sum(int(v or 0) for v in removed.values())
        return _ok({
            "message": (f"已清空该群学习数据（共 {total} 项）" if total
                        else "该群本来就没有学习数据"),
            "removed": removed,
            "still_listed": still,
        })

    async def api_rollback(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        sid = str(payload.get("sid") or "")
        if not gid:
            return _err("缺少参数 group_id", 400)
        ok, msg = self.plugin.db.rollback(gid, sid or None)
        if ok:
            await self.plugin.db.save()
        return _ok({"ok": ok, "message": msg})

    async def api_switch(self):
        payload = await _json_body()
        action = str(payload.get("action") or "").strip()
        if action not in ("pause", "resume"):
            return _err("action 必须是 pause 或 resume", 400)
        msg = await self.plugin._switch_text("", "关" if action == "pause" else "开")
        return _ok({"message": msg, "paused": self.plugin._is_paused()})

    async def api_reflect(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok({"message": await self.plugin.run_reflection(gid)})

    async def api_persona_get(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok({"group_id": gid, **self.plugin.db.get_persona(gid)})

    async def api_persona_set(self):
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        if not gid:
            return _err("缺少参数 group_id", 400)
        mode = str(payload.get("mode") or "follow")
        text = str(payload.get("text") or "")
        if mode == "custom" and not text.strip():
            return _err("选择「专属人设」时必须填写人设内容", 400)
        saved = self.plugin.db.set_persona(gid, mode=mode, text=text)
        await self.plugin.db.maybe_save()
        hint = ("已切换为跟随 AstrBot 全局人设"
                if saved["mode"] == "follow"
                else f"已保存本群专属人设（{len(saved['text'])} 字），下一条消息起生效")
        return _ok({"group_id": gid, **saved, "message": hint})

    async def api_persona_clear(self):
        # DELETE 请求体在部分框架/代理上会被丢掉，所以同时支持查询参数
        payload = await _json_body()
        gid = str(payload.get("group_id") or _query_str("group_id"))
        if not gid:
            return _err("缺少参数 group_id", 400)
        removed = self.plugin.db.clear_persona(gid)
        await self.plugin.db.maybe_save()
        return _ok({"group_id": gid, "removed": removed,
                    "message": "已删除本群专属人设，回到跟随全局"})

    async def api_export(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.plugin.db.export_group(gid))

    # ------------------------------------------------------------------
    #  配置：面板内直接改，不必再跳到 AstrBot 原生配置页
    # ------------------------------------------------------------------
    async def api_config_get(self):
        await self.plugin._ensure_loaded()
        return _ok(self.plugin.config_service.describe())

    async def api_config_set(self):
        payload = await _json_body()
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            return _err("缺少参数 values（对象）", 400)
        result = await self.plugin.config_service.apply_async(values)
        # 改完配置要让插件按新参数刷新运行时组件（学习率、硬上限、半衰期
        # 这些是构造时读进实例的）。刷新失败**必须**报出来，
        # 否则用户会以为"保存成功、已生效"，实际还在用旧参数跑。
        if result.get("changed"):
            try:
                self.plugin.reload_runtime_config()
                result["applied"] = True
            except Exception as e:
                logger.error("[自进化] 配置已保存但运行时刷新失败: %s", e)
                result["applied"] = False
                result["message"] = (
                    f"{result.get('message', '')}；但运行时刷新失败，"
                    f"请在插件管理里重载插件后重试（{e}）"
                )
        return _ok(result)

    async def api_preview(self):
        """预览：给定群与一句话，实际会被注入什么。

        这是面板相对「只看配置项」的价值——用户能直接看到学习结果最终以
        什么样子进入对话，而不是靠猜参数含义。
        """
        payload = await _json_body()
        gid = str(payload.get("group_id") or "")
        probe = str(payload.get("message") or "")
        if not gid:
            return _err("缺少参数 group_id", 400)
        try:
            return _ok(self.plugin.preview_injection(gid, probe))
        except Exception as e:
            logger.exception("[自进化] 生成注入预览失败")
            return _err(f"生成预览失败: {e}", 500)


# ======================================================================
#  辅助
# ======================================================================
def _dimension_meta() -> list[dict]:
    """把策略维度渲染成前端可直接展示的元信息。"""
    from .learning.bandit import (
        BASELINE_INDEX, DIMENSIONS, DIMENSION_HINT, DIMENSION_PROMPT,
    )
    out: list[dict] = []
    for dim, levels in DIMENSIONS.items():
        out.append({
            "key": dim,
            "label": DIMENSION_PROMPT.get(dim, "{level}").replace("{level}", "").strip(" ：:"),
            "levels": list(levels),
            "hints": list(DIMENSION_HINT.get(dim, [])),
            "baseline": BASELINE_INDEX,
        })
    return out


def _ok(data: Any):
    if json_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return json_response(data)


def _err(message: str, status_code: int = 400):
    if error_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return error_response(message, status_code=status_code)


def _query_str(key: str, default: str = "") -> str:
    try:
        val = request.query.get(key, default)
        return str(val) if val is not None else default
    except Exception:
        return default


async def _json_body() -> dict:
    try:
        payload = await request.json(default={})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
