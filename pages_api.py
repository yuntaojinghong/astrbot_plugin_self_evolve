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
            ("/rollback", self.api_rollback, ["POST"], "回滚到指定版本"),
            ("/switch", self.api_switch, ["POST"], "暂停/恢复学习"),
            ("/reflect", self.api_reflect, ["POST"], "立即执行一次反思"),
            ("/export", self.api_export, ["GET"], "导出某群学习数据"),
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

    async def api_export(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.plugin.db.export_group(gid))


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
