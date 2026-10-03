"""配置读写服务：让面板直接改配置，不必再跳到 AstrBot 原生配置页。

设计要点
--------
1. **schema 是白名单**：只接受 ``_conf_schema.json`` 里声明过的键。
   否则面板就成了「任意写配置」的后门。
2. **按 schema 声明类型强制转换**：布尔/整数/浮点/字符串，转换失败就用默认值，
   绝不让面板把 "abc" 写进一个 int 字段，然后让插件在运行时炸掉。
3. **范围夹取**：schema 里带 ``slider``（min/max）的按区间夹住。
4. **改动立即生效**：直接改 ``self.config``，插件的 ``_cfg*`` 读的就是它；
   同时尽力持久化，失败也只是「重启后回退」，不影响本次运行。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any

#: 按面板分组展示（键必须在 schema 里存在，否则会被忽略）
GROUPS: list[dict[str, Any]] = [
    {
        "key": "basics",
        "label": "基础",
        "desc": "总开关与注入范围",
        "keys": ["enabled", "inject_enabled", "learn_in_private",
                 "inject_max_entries", "inject_max_chars"],
    },
    {
        "key": "learning",
        "label": "学习力度",
        "desc": "越保守越稳：学得慢，但不容易被几条消息带偏",
        "keys": ["min_samples", "max_offset", "max_abs", "learning_rate",
                 "epsilon", "decay_half_life_days", "memory_half_life_days",
                 "max_entries_per_group"],
    },
    {
        "key": "signals",
        "label": "反馈信号",
        "desc": "采集哪些信号、以及有效时间窗",
        "keys": ["learn_from_implicit", "min_signal_weight", "feedback_window_sec"],
    },
    {
        "key": "reflect",
        "label": "反思",
        "desc": "让模型复盘并产出待批候选（消耗 token，产出必须审批）",
        "keys": ["reflect_max_candidates", "reflect_provider_id",
                 "auto_reflect", "auto_reflect_minutes", "history_limit"],
    },
    {
        "key": "admin",
        "label": "管理",
        "desc": "命令权限与可回滚性",
        "keys": ["auto_snapshot", "enable_commands", "admin_only", "list_limit"],
    },
]

#: 每个配置项的风险级别：safe / caution
#: 面板会据此给提示——学习类插件的参数并不是"越大越好"。
RISK: dict[str, str] = {
    "max_offset": "caution",
    "max_abs": "caution",
    "learning_rate": "caution",
    "epsilon": "caution",
    "min_samples": "caution",
    "min_signal_weight": "caution",
    "decay_half_life_days": "caution",
    "auto_reflect": "caution",
    "inject_max_chars": "caution",
    "enabled": "caution",
    "inject_enabled": "caution",
    "admin_only": "caution",
}

#: 面板对个别参数给一句"人话"建议（覆盖 schema hint 里偏技术的说法）
ADVICE: dict[str, str] = {
    "max_offset": "风格最多偏离基线多少。调大会让语气变化更明显，也更容易翻车。不建议超过 0.35。",
    "min_samples": "某个档位要攒够多少次反馈才开始影响回复。调到 3 以下会变得很敏感。",
    "epsilon": "探索概率。调高会试更多新风格，代价是语气更不稳定。",
    "learning_rate": "每次反馈改变多少。调高学得快，也更容易被少数几条消息带偏。",
    "min_signal_weight": "低于该权重的反馈直接丢弃。调到 0.3 左右可以只信明确信号。",
    "auto_reflect": "开启后会定期调用模型复盘，**会额外消耗 token**；产出仍需你审批才生效。",
    "inject_enabled": "只停注入、继续学习。想先观察它学得对不对，就关掉这项。",
    "enabled": "关掉后不注入也不采集，行为与未安装一致。",
}

#: 一键预设：名字 → {配置键: 值}
PRESETS: list[dict[str, Any]] = [
    {
        "key": "observe",
        "label": "先观察（推荐上手）",
        "desc": "照常学习但不影响回复，用面板看它学得对不对",
        "values": {"enabled": True, "inject_enabled": False,
                   "learn_from_implicit": True},
    },
    {
        "key": "balanced",
        "label": "平衡（默认）",
        "desc": "默认参数：有一定学习速度，同时不易被带偏",
        "values": {"enabled": True, "inject_enabled": True, "min_samples": 5,
                   "max_offset": 0.20, "learning_rate": 0.25, "epsilon": 0.12,
                   "min_signal_weight": 0.0, "learn_from_implicit": True},
    },
    {
        "key": "strict",
        "label": "保守",
        "desc": "只信明确信号、偏移更小、门槛更高：几乎不会乱改语气",
        "values": {"enabled": True, "inject_enabled": True, "min_samples": 10,
                   "max_offset": 0.10, "learning_rate": 0.15, "epsilon": 0.05,
                   "min_signal_weight": 0.30, "learn_from_implicit": False},
    },
    {
        "key": "off",
        "label": "全部关闭",
        "desc": "完全不学习也不注入",
        "values": {"enabled": False, "inject_enabled": False,
                   "auto_reflect": False},
    },
]


#: 硬性安全上限：**不受 schema 影响**的最后一道闸门。
#: schema 的 slider 只是面板/原生配置页的输入范围，而 _conf.json 是可以手改的，
#: 手改能绕开 slider。这几个值直接决定"机器人说话会偏离基线多远"，
#: 因此无论谁怎么写配置，都必须在代码里再夹一次。
#: 超过上限不是报错，而是夹到上限并**明确告知**，避免用户以为设置生效了。
HARD_CEILING: dict[str, tuple[float, float]] = {
    "max_offset": (0.0, 0.60),        # 风格最大偏移 60%，再多就不像同一个机器人了
    "max_abs": (0.1, 10.0),           # 单档分数上限
    "learning_rate": (0.01, 1.0),
    "epsilon": (0.0, 1.0),
    "inject_max_chars": (50, 8000),
    "min_samples": (1, 200),
}


def load_schema(plugin_dir: str) -> dict:
    """读取 _conf_schema.json；读不到就返回空 dict（面板会退化为只读）。"""
    path = os.path.join(plugin_dir, "_conf_schema.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class ConfigService:
    """面板侧的配置读写。"""

    def __init__(self, plugin: Any, schema: dict | None = None):
        self.plugin = plugin
        self.schema = schema if schema is not None else load_schema(plugin.plugin_dir())

    # ==================================================================
    #  读
    # ==================================================================
    def current(self) -> dict:
        """当前生效的配置（已与应用内默认值合并）。"""
        merged = dict(getattr(self.plugin, "DEFAULTS", {}) or {})
        raw = getattr(self.plugin, "config", None)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if k in merged or not self.schema})
        return merged

    def defaults(self) -> dict:
        out = dict(getattr(self.plugin, "DEFAULTS", {}) or {})
        for key, spec in self.schema.items():
            if isinstance(spec, dict) and "default" in spec:
                out.setdefault(key, spec["default"])
        return out

    def field_meta(self, key: str) -> dict:
        spec = self.schema.get(key) or {}
        meta: dict[str, Any] = {
            "key": key,
            "type": spec.get("type", "string"),
            "label": spec.get("description") or key,
            "hint": ADVICE.get(key) or spec.get("hint") or "",
            "risk": RISK.get(key, "safe"),
        }
        slider = spec.get("slider")
        if isinstance(slider, dict):
            meta["min"] = slider.get("min")
            meta["max"] = slider.get("max")
            meta["step"] = slider.get("step")
        return meta

    def describe(self) -> dict:
        """给面板的完整描述：分组 + 每项元信息 + 当前值 + 预设。"""
        current, defaults = self.current(), self.defaults()
        known = set(self.schema) or set(current)
        groups = []
        used: set[str] = set()
        for group in GROUPS:
            keys = [k for k in group["keys"] if k in known]
            if not keys:
                continue
            used.update(keys)
            groups.append({
                "key": group["key"],
                "label": group["label"],
                "desc": group["desc"],
                "fields": [
                    {**self.field_meta(k), "value": current.get(k, defaults.get(k)),
                     "default": defaults.get(k)}
                    for k in keys
                ],
            })
        # schema 里有、但没归入任何分组的键，兜到一个"其它"组，避免漏展示
        rest = sorted(known - used)
        if rest:
            groups.append({
                "key": "others", "label": "其它", "desc": "未归类的配置项",
                "fields": [
                    {**self.field_meta(k), "value": current.get(k, defaults.get(k)),
                     "default": defaults.get(k)}
                    for k in rest
                ],
            })
        return {
            "groups": groups,
            "presets": PRESETS,
            "current": current,
            "defaults": defaults,
            "writable": bool(self.schema),
        }

    # ==================================================================
    #  校验
    # ==================================================================
    def _coerce(self, key: str, value: Any) -> tuple[bool, Any, str]:
        """按 schema 把值转成正确类型，并按范围夹取。

        夹取分两层：

        1. schema 的 ``slider``——面板与原生配置页的输入范围；
        2. :data:`HARD_CEILING`——代码里的安全上限。``_conf.json`` 可以手改，
           手改能绕过 slider，因此凡直接影响"说话偏离基线多远"的参数，
           必须在代码里再夹一次。

        被夹取时返回一条提示而不是默默改掉，让用户知道自己的值没被完整采纳。

        Returns:
            ``(是否接受, 规范化后的值, 提示/拒绝原因)``
        """
        spec = self.schema.get(key)
        if spec is None:
            return False, None, f"未知配置项 {key}（不在 _conf_schema.json 里）"
        kind = spec.get("type", "string")

        if kind == "bool":
            if isinstance(value, bool):
                return True, value, ""
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "on", "是", "开"):
                    return True, True, ""
                if low in ("false", "0", "no", "off", "否", "关"):
                    return True, False, ""
            if isinstance(value, (int, float)):
                return True, bool(value), ""
            return False, None, f"{key} 需要布尔值"

        if kind in ("int", "float"):
            if isinstance(value, bool):
                return False, None, f"{key} 需要数值，不是布尔"
            try:
                num = float(value)
            except (TypeError, ValueError):
                return False, None, f"{key} 需要数值"
            if num != num or num in (float("inf"), float("-inf")):
                return False, None, f"{key} 不是有效数值"

            note = ""
            slider = spec.get("slider") or {}
            lo, hi = slider.get("min"), slider.get("max")
            if isinstance(lo, (int, float)) and num < lo:
                num, note = float(lo), f"{key} 已按最小值 {lo} 夹取"
            if isinstance(hi, (int, float)) and num > hi:
                num, note = float(hi), f"{key} 已按最大值 {hi} 夹取"

            ceiling = HARD_CEILING.get(key)
            if ceiling:
                clo, chi = ceiling
                if num < clo:
                    num, note = clo, f"{key} 低于安全下限 {clo}，已夹取"
                elif num > chi:
                    num, note = chi, f"{key} 超出安全上限 {chi}，已夹取"

            if kind == "int":
                return True, int(round(num)), note
            return True, float(num), note

        # string / text 等
        if value is None:
            return True, "", ""
        return True, str(value), ""

    def build_patch(self, payload: dict) -> tuple[dict, list[str], list[str]]:
        """把面板提交的改动整理成可写入的补丁。

        Returns:
            ``(patch, 被拒绝的说明, 实际有变化的键)``
        """
        if not isinstance(payload, dict):
            return {}, ["提交内容不是对象"], []
        current = self.current()
        patch: dict[str, Any] = {}
        rejected: list[str] = []
        for key, value in payload.items():
            ok, norm, why = self._coerce(str(key), value)
            if not ok:
                rejected.append(why)
                continue
            if current.get(key) != norm:
                patch[key] = norm
        return patch, rejected, sorted(patch)

    # ==================================================================
    #  写
    # ==================================================================
    def apply(self, payload: dict) -> dict:
        """应用改动：更新内存配置并尽力持久化。"""
        patch, rejected, changed = self.build_patch(payload)
        if not patch:
            return {"changed": [], "rejected": rejected,
                    "persisted": False,
                    "message": "没有需要改动的项" + (f"；{len(rejected)} 项被拒绝" if rejected else "")}

        raw = getattr(self.plugin, "config", None)
        if not isinstance(raw, dict):
            return {"changed": [], "rejected": rejected, "persisted": False,
                    "message": "当前配置对象不可写"}

        raw.update(patch)
        persisted, note = self._persist(patch)
        return {
            "changed": changed,
            "rejected": rejected,
            "persisted": persisted,
            "message": f"已更新 {len(changed)} 项" + ("" if persisted else f"（{note}）"),
        }

    def _persist(self, patch: dict) -> tuple[bool, str]:
        """尽力把配置写盘。

        写盘失败不影响**本次运行**（内存已更新），只是重启后回退。
        """
        raw = self.plugin.config
        save = getattr(raw, "save_config", None)
        if not callable(save):
            return False, "配置对象不支持保存，重启后会回退"

        # 优先用 replace_config 参数做一次合并保存；
        # 老版本签名不同就直接就地保存（值已经在 raw 里了）。
        try:
            params = inspect.signature(save).parameters
        except (TypeError, ValueError):
            params = {}
        try:
            if "replace_config" in params:
                save(replace_config=dict(patch))
            else:
                save()
            return True, ""
        except TypeError:
            try:
                save()
                return True, ""
            except Exception as exc:
                return False, f"保存失败：{exc}"
        except Exception as exc:
            return False, f"保存失败：{exc}"

    async def apply_async(self, payload: dict) -> dict:
        """save_config 是同步阻塞的，放到线程里跑，别堵事件循环。"""
        return await asyncio.to_thread(self.apply, payload)
