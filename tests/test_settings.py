"""验证「面板内改设置」这条链路：读取 → 校验 → 写入 → 生效 → 持久化。

这是用户提出的核心诉求（不要在面板和原生配置页之间来回跳），
所以必须端到端验证，而不是只看接口能返回。
"""

import asyncio
import os
import sys
import types

PKG = "astrbot_plugin_self_evolve"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 桩
def _module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Logger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass

    def exception(self, *a, **k): pass


class _Filter:
    EventMessageType = types.SimpleNamespace(ALL="all", GROUP_MESSAGE="group", PRIVATE_MESSAGE="private")

    def __getattr__(self, name):
        def deco(*a, **k):
            return lambda fn: fn
        return deco


class AstrMessageEvent:
    pass


class Star:
    plugin_id = "self_evolve"

    def __init__(self, context=None, config=None):
        self.context = context
        if config is not None:
            self.config = config

    async def get_kv_data(self, key, default=None): return default
    async def put_kv_data(self, key, value): return None
    async def delete_kv_data(self, key): return None


class TextPart:
    def __init__(self, text=""):
        self.text = text
        self.type = "text"


class _WebReq:
    def __init__(self):
        self.query = {}
        self._json = {}

    def get(self, key, default=None):
        return self.query.get(key, default)

    async def json(self, default=None):
        return self._json or (default or {})


WEB_REQ = _WebReq()

_module("astrbot")
_module("astrbot.api", logger=_Logger())
_module("astrbot.api.event", filter=_Filter(), AstrMessageEvent=AstrMessageEvent,
        MessageChain=object, EventMessageType=_Filter.EventMessageType)
_module("astrbot.api.star", Context=object, Star=Star, register=lambda *a, **k: (lambda c: c))
_module("astrbot.api.message_components")
_module("astrbot.core")
_module("astrbot.core.agent")
_module("astrbot.core.agent.message", TextPart=TextPart)
_module("astrbot.core.utils")
_module("astrbot.core.utils.astrbot_path",
        get_astrbot_plugin_data_path=lambda: os.path.join(os.environ.get("TEMP", "."), "se_cfg_test"))
_module("astrbot.api.web",
        json_response=lambda p: p,
        error_response=lambda msg, status_code=400: {"error": msg, "code": status_code},
        request=WEB_REQ)

sys.path.insert(0, os.path.dirname(REPO))
main_mod = __import__(f"{PKG}.main", fromlist=["x"])
cfg_mod = __import__(f"{PKG}.config_service", fromlist=["x"])
pages_mod = __import__(f"{PKG}.pages_api", fromlist=["x"])



# =====================================================================
#  执行体
#
#  全部断言都放在 main() 里，不用模块顶层——模块顶层会在 pytest collect
#  阶段就执行，那既让失败信息难看（只会说 collecting 出错），
#  也容易和别的测试文件互相污染（见 tests/README 的说明）。
# =====================================================================
def main() -> None:
    # ------------------------------------------------- 一个"像真的"配置对象
    class FakePluginConfig(dict):
        """模拟 AstrBotConfig：dict + save_config(replace_config=None)。"""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.saved = 0
            self.last_patch = None

        def save_config(self, replace_config=None, indent=None):
            self.saved += 1
            if replace_config:
                self.last_patch = dict(replace_config)
                super().update(replace_config)
            return True


    def make_plugin(config=None):
        raw = FakePluginConfig(config or {})
        plugin = main_mod.SelfEvolve(object(), raw)
        plugin.db.path = os.path.join(os.environ.get("TEMP", "."), f"se_cfg_{id(plugin)}.json")
        plugin.db.kv = None
        return plugin, raw


    # ======================================================================
    print("=" * 72)
    print("一、读取配置描述")
    print("=" * 72)
    plugin, raw = make_plugin({"min_samples": 7, "max_offset": 0.15})
    desc = plugin.config_service.describe()
    groups = {g["key"]: g for g in desc["groups"]}
    print("  分组:", list(groups))
    print("  预设:", [p["key"] for p in desc["presets"]])
    print("  可写:", desc["writable"])
    assert desc["writable"], "schema 读不到，面板会退化为只读"
    assert desc["current"]["min_samples"] == 7, desc["current"]
    assert desc["current"]["max_offset"] == 0.15
    # 未在 config 里出现的键应回落到默认值
    assert desc["current"]["epsilon"] == 0.12, desc["current"]["epsilon"]
    print("  OK 读到了用户配置并回落默认值")

    # 所有 schema 键都必须出现在某个分组里，否则面板漏展示
    shown = {f["key"] for g in desc["groups"] for f in g["fields"]}
    schema_keys = set(plugin.config_service.schema)
    assert shown == schema_keys, f"漏展示: {schema_keys - shown}"
    print(f"  OK {len(shown)} 个 schema 键全部有展示")

    # 风险项与建议
    cautions = [f["key"] for f in (f for g in desc["groups"] for f in g["fields"]) if f["risk"] == "caution"]
    print(f"  需谨慎项 {len(cautions)} 个: {cautions[:5]}...")
    assert "max_offset" in cautions

    print()
    print("=" * 72)
    print("二、类型校验与范围夹取（schema 是白名单）")
    print("=" * 72)
    svc = plugin.config_service

    cases = [
        ({"min_samples": "12"}, True, 12, "字符串数字应能转成 int"),
        ({"min_samples": 3.7}, True, 4, "浮点应四舍五入成 int"),
        ({"min_samples": "abc"}, False, None, "非数字必须拒绝"),
        ({"min_samples": True}, False, None, "布尔不能当数字"),
        ({"enabled": "false"}, True, False, "字符串布尔应识别"),
        ({"enabled": "开"}, True, None, "中文布尔应识别（当前已是 true，无变化属正常）"),
        ({"enabled": 0}, True, False, "0 转 bool"),
        ({"not_a_key": 1}, False, None, "schema 外的键必须拒绝（白名单）"),
    ]
    for values, should_ok, expect, label in cases:
        patch, rejected, changed = svc.build_patch(values)
        key = next(iter(values))
        if should_ok:
            if expect is None:
                ok = not rejected
            else:
                got = patch.get(key, svc.current().get(key))
                ok = (got == expect) and not rejected
        else:
            ok = bool(rejected) and key not in patch
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        print(f"        patch={patch} rejected={rejected}")
        assert ok, label

    print()
    print("  范围夹取（两层）：")
    # 第一层 schema slider 上限是 1.0，但第二层代码硬上限 0.60 更严，因此取 0.60。
    # 这正是"两层"的意义：光靠 slider 挡不住 0.6~1.0 这段危险区间。
    patch, _, _ = svc.build_patch({"max_offset": 99.0})
    print(f"    max_offset=99  -> {patch.get('max_offset')}  (硬上限 0.60 优于 slider 1.0)")
    assert patch.get("max_offset") == 0.60, patch

    # 把 slider 放开到 5.0，硬上限仍应生效
    svc2 = cfg_mod.ConfigService(plugin, dict(svc.schema))
    svc2.schema["max_offset"] = {**svc.schema["max_offset"],
                                 "slider": {"min": 0.0, "max": 5.0, "step": 0.05}}
    patch2, _, _ = svc2.build_patch({"max_offset": 5.0})
    print(f"    slider 放开到 5.0 -> {patch2.get('max_offset')}  (硬上限仍然生效)")
    assert patch2.get("max_offset") == 0.60, patch2

    # 未设硬上限的项只受 slider 约束
    patch3, _, _ = svc.build_patch({"max_entries_per_group": 999999})
    print(f"    max_entries_per_group=999999 -> {patch3.get('max_entries_per_group')}  (仅 slider 上限)")
    assert patch3.get("max_entries_per_group") == 5000, patch3

    # 下限夹取 + 提示文案
    patch4, _, _ = svc.build_patch({"min_samples": 0})
    print(f"    min_samples=0  -> {patch4.get('min_samples')}  (slider 下限 1)")
    assert patch4.get("min_samples") == 1, patch4

    accepted, norm, note = svc._coerce("max_offset", 99.0)
    print(f"    夹取时给出提示: {note!r}")
    assert note, "夹取了却没告诉用户"

    print()
    print("=" * 72)
    print("三、写入 + 持久化 + 立即生效")
    print("=" * 72)
    res = svc.apply({"min_samples": 20, "max_offset": 0.30, "enabled": False})
    print("  写入结果:", res)
    assert res["changed"] and res["persisted"], res
    assert raw["min_samples"] == 20 and raw["max_offset"] == 0.30
    assert raw["enabled"] is False
    assert raw.saved >= 1, "没有调用 save_config，改动不会持久化"
    print(f"  OK 已写入 FakeAstrBotConfig 且调用 save_config（{raw.saved} 次）")

    # 立即生效：_cfg_int 读的就是新值
    assert plugin._cfg_int("min_samples") == 20
    print("  OK 插件读取到新值（_cfg_int=20）")

    # 重建运行时组件后仍然生效
    plugin.reload_runtime_config()
    assert plugin.db.state.bandit.min_samples == 20, plugin.db.state.bandit.min_samples
    print(f"  OK 重建后策略表用上了新参数（min_samples={plugin.db.state.bandit.min_samples}）")

    # 无效值不应污染配置
    before = dict(raw)
    res = svc.apply({"min_samples": "坏值", "不存在的键": 1})
    print("  无效提交:", res)
    assert raw == before, "无效值被写进去了！"
    assert len(res["rejected"]) == 2, res
    print("  OK 无效值被拒绝且未写入")

    print()
    print("=" * 72)
    print("四、注入预览")
    print("=" * 72)
    gid = "1001"
    plugin.db.state.memory.add(
        __import__(f"{PKG}.learning.memory", fromlist=["x"]).Entry(
            content="本群把版主叫扫地僧", kind="term", group_id=gid)
    )
    prev = plugin.preview_injection(gid, "版主叫啥")
    print("  active:", prev["active"])
    print("  choice:", prev["choice_desc"])
    print("  entries:", [e["content"] for e in prev["entries"]])
    print("  text 前 120 字:", repr(prev["text"][:120]))
    assert prev["entries"], "相关经验没被检索到"
    assert prev["text"], "注入文本为空"

    print()
    print("=" * 72)
    print("五、HTTP 接口层")
    print("=" * 72)
    web = plugin.web
    assert web is not None, "面板没注册"

    boot = asyncio.run(web.api_config_get())
    assert "groups" in boot and "presets" in boot
    print("  OK GET /config")

    WEB_REQ._json = {"values": {"epsilon": 0.25}}
    res = asyncio.run(web.api_config_set())
    print("  POST /config ->", res)
    # 先确认拿到的是成功响应。少了这一句的话，接口返回 {"error": ...} 时
    # 下一行只会报 KeyError: 'changed'，看不出真正原因（这个测试之前就是这样，
    # 而且它没被 CI 执行，所以一直没人发现）。
    assert "error" not in res, f"保存配置被拒了: {res}"
    assert res["changed"] == ["epsilon"], res
    assert plugin.config["epsilon"] == 0.25
    print("  OK POST /config（epsilon -> 0.25）")

    # 顺带把这个接口的分工钉住，免得以后又搞混：
    #   HTTP 层收的是 {"values": {...}}（前端就是这么发的）
    #   api_config_set 负责拆出 values，再交给 config_service.apply_async
    #   所以 apply_async 收的是**拆开后**的扁平内容
    flat = asyncio.run(plugin.config_service.apply_async({"epsilon": 0.5}))
    assert "error" not in flat and flat["changed"] == ["epsilon"], flat
    assert plugin.config["epsilon"] == 0.5

    wrapped = asyncio.run(plugin.config_service.apply_async({"values": {"epsilon": 0.9}}))
    assert wrapped["changed"] == [], (
        "apply_async 不该接受带 values 外壳的整包——"
        f"那样会把 values 当成未知字段丢掉。实际: {wrapped}")
    print("  OK apply_async 收扁平内容，不接受 values 外壳")

    # 非法请求
    WEB_REQ._json = {}
    res = asyncio.run(web.api_config_set())
    assert "error" in res, res
    print("  OK 空提交被拒:", res["error"])

    WEB_REQ._json = {"group_id": gid, "message": "版主"}
    res = asyncio.run(web.api_preview())
    assert "text" in res, res
    print(f"  OK POST /preview（{len(res['text'])} 字）")

    WEB_REQ._json = {}
    res = asyncio.run(web.api_preview())
    assert "error" in res
    print("  OK 缺 group_id 被拒")

    WEB_REQ.query = {}
    WEB_REQ._json = {}

    print()
    print("=" * 72)
    print("全部通过：面板内改设置可读、可校验、可写、可持久化、立即生效")
    print("=" * 72)


def test_settings_end_to_end() -> None:
    """给 pytest 用的入口。

    本文件原本所有断言都在模块顶层，`pytest tests` 会在 collect 阶段
    就把它们跑掉；又因为它和 tests/test_logic.py 都往 sys.modules 里塞假
    astrbot 模块，先跑的那个会把后跑的那个的桩覆盖掉，于是接口读不到请求体，
    报出一个与真实原因无关的「缺少参数 values」。

    收进函数之后，pytest 只把它当成一个普通用例；CI 里仍然逐个文件
    独立进程执行，互不影响。
    """
    main()


if __name__ == "__main__":
    main()
