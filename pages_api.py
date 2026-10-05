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

        # 注册一条通配路由，真正是哪个接口靠 URL 后缀判断。
        #
        # 为什么不按固定路径注册：地址前面有几层前缀不由插件决定。
        # 实测过的真实地址里插件名出现过两次 ——
        #   /api/v1/plugins/extensions/astrbot_plugin_self_evolve/
        #                              astrbot_plugin_self_evolve/preview
        # 路由声明是 /plugins/extensions/{plugin_path:path}，plugin_path 取整条剩余
        # 路径，再拿它去 fullmatch 注册路径。所以「带前缀」和「不带前缀」两种
        # 固定写法都会在另一种情况下全部失配，而页面本身照旧能打开
        # （HTML 是静态文件），表现为「界面出来了、点什么都提示未找到该路由」。
        #
        # 用 /<path:rest> 匹配任何深度，再用后缀定位端点，就与前缀层数无关了。
        # 接口清单见 _endpoint_table()。
        wildcard = [("/<path:rest>", self.api_dispatch,
                     ["GET", "POST", "DELETE"], "面板接口（按后缀分派）")]
        ok = 0
        for path, handler, methods, desc in wildcard:
            candidate = path if path.startswith("/") else f"/{path}"
            try:
                register(candidate, self._wrap(handler), methods, desc)
                ok += 1
            except Exception as e:
                logger.error("[自进化] 注册路由 %s 失败: %s", candidate, e)
        self._registered = ok > 0
        self.registered = self._registered
        if self._registered:
            logger.info(
                "[自进化] 配置面板已注册通配接口，共 %s 个端点",
                len(self._endpoint_table()),
            )
        return self._registered

    def _endpoint_table(self) -> dict[str, tuple[str, set[str]]]:
        """端点表：URL 后缀 -> (处理函数名, 允许的方法)。

        后缀与前端 ``apiGet`` / ``apiPost`` 传的字符串**必须一致**。
        读和写共用同一个后缀，靠请求方法区分——这与前端原有调用一致：

        * ``config``   GET = 读配置，POST = 写配置
        * ``persona``  GET = 读人设，POST = 写人设，DELETE = 清除

        同后缀挂多方法在这里很自然：分派时先按后缀查表，再校验方法。
        """
        return {
            "bootstrap": ("api_bootstrap", {"GET"}),
            "overview": ("api_overview", {"GET"}),
            "group": ("api_group", {"GET"}),
            "approve": ("api_approve", {"POST"}),
            "reject": ("api_reject", {"POST"}),
            "forget": ("api_forget", {"POST"}),
            "reset_group": ("api_reset_group", {"POST"}),
            "rollback": ("api_rollback", {"POST"}),
            "switch": ("api_switch", {"POST"}),
            "reflect": ("api_reflect", {"POST"}),
            "export": ("api_export", {"GET"}),
            "preview": ("api_preview", {"POST"}),
            "config": ("api_config_get", {"GET"}),
            "config/save": ("api_config_set", {"POST"}),
            "persona": ("api_persona_get", {"GET"}),
            "persona/save": ("api_persona_set", {"POST"}),
            "persona/clear": ("api_persona_clear", {"DELETE"}),
        }

    def _tail_of(self, request_) -> str:
        """从请求里取出端点后缀，形如 ``"preview"``。

        优先用框架解析出的通配参数；取不到再从完整路径里按**已知端点后缀**
        定位——这样与地址前头有多少层前缀无关。
        """
        params = getattr(request_, "path_params", None)
        if isinstance(params, dict) and params.get("rest"):
            return str(params["rest"]).strip("/")

        raw_path = ""
        for src in (request_, getattr(request_, "_request", None)):
            p = getattr(src, "path", None)
            if isinstance(p, str) and p:
                raw_path = p
                break
        if not raw_path:
            return ""
        raw_path = raw_path.split("?", 1)[0].strip("/")
        return self._normalize_endpoint(raw_path)

    async def api_dispatch(self, rest: str = "", **kwargs):
        """通配入口：按 URL 后缀把请求分派到具体处理函数。

        ``rest`` 是框架从 ``/<path:rest>`` 里解出来的通配内容，会作为
        **关键字参数**传进来，所以签名里必须收它——否则直接
        ``TypeError: got an unexpected keyword argument 'rest'``。
        （磐石那边线上就是这么炸的，同一个写法。）
        多收一个 ``**kwargs`` 兜住其它版本可能多传的参数。

        方法不符返回 405，端点不认识返回 404 并记日志。
        """
        endpoint = str(rest or "").strip("/")
        endpoint = self._normalize_endpoint(endpoint) if endpoint \
            else self._tail_of(request)

        req_obj = getattr(request, "_request", None) or request
        method = str(getattr(req_obj, "method", "GET") or "GET").upper()

        hit = self._endpoint_table().get(endpoint)
        if hit is None:
            logger.warning("[自进化] 面板请求了未知端点: %r", endpoint)
            return _err(f"未知接口 {endpoint!r}，请更新插件", 404)

        handler_name, allowed = hit
        if method not in allowed:
            return _err(
                f"{endpoint} 不接受 {method}（允许 {'/'.join(sorted(allowed))}）", 405)
        return await getattr(self, handler_name)()

    def _normalize_endpoint(self, tail: str) -> str:
        """把一段路径映射成端点名。

        真实 URL 里插件名可能出现 0~2 次，所以不能假定 ``tail`` 就是端点：
        按**已知端点的最长后缀**匹配来剥离多余前缀。
        认不出来时原样返回，交给调用方报 404。
        """
        tail = str(tail or "").strip("/")
        if not tail:
            return ""
        table = self._endpoint_table()
        if tail in table:
            return tail
        best = ""
        for ep in table:
            if tail.endswith("/" + ep) and len(ep) > len(best):
                best = ep
        return best or tail

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
