"""学习素材过滤：把不该学的文本挡在外面。

参考 astrbot_plugin_self_learning 的 sample_filter 思路（**只学设计，不抄代码**，
那个项目是 AGPL-3.0，直接抄会把本项目的 MIT 许可拖成 AGPL）。

为什么需要：群里的文本并不都是"群友说的话"。至少有四类会污染学习：

1. **命令**：``/reset``、``!help``、``.签到``，以及"使用 xx 命令"这类引导语
2. **系统/插件输出**：版本横幅、命令帮助文本、报错堆栈、超时提示
3. **通知类事件**：入群/退群/poke 的占位文本
4. **纯媒体占位**：``[图片]`` ``[Image]`` ``[Face]``

这些一旦被当成"群偏好"学进去，机器人就会开始模仿报错信息或复读命令。

纯函数，无 IO，便于单测。
"""

from __future__ import annotations

import re

#: 命令前缀。中文全角斜杠也常见（手机输入法）
_CMD_PREFIX = re.compile(r"^\s*[/／!#.。！]\s*\S")

#: 裸命令词：不带前缀，但整句就是命令名
_BARE_COMMANDS = (
    "help", "new", "reset", "provider", "history", "persona", "plugin",
    "model", "tools", "status", "menu",
    "帮助", "菜单", "重置", "清空", "状态", "签到", "抽签",
)

#: 「使用 /xxx 命令」这类引导语——是机器人在教用户，不是用户的表达习惯
_CMD_GUIDANCE = (
    re.compile(r"(?:使用|输入|发送|执行|运行|调用|通过|试试)[^。！？\n]{0,12}"
               r"(?:命令|指令|菜单|帮助|功能|插件)"),
    re.compile(r"[/／!#.][A-Za-z_][\w-]*[^。！？\n]{0,24}"
               r"(?:命令|指令|菜单|帮助|功能|插件)"),
)

#: 系统/框架输出
_SYSTEM_OUTPUT = (
    re.compile(r"^AstrBot\s+v?\d", re.IGNORECASE),
    re.compile(r"^(Traceback|Exception|Error|Warning|TimeoutError|"
               r"KeyError|ValueError|TypeError|AttributeError)\b", re.MULTILINE),
    re.compile(r"(调用超时|请求超时|发生错误|调用失败|未配置|不可用|已忽略|"
               r"权限不足|无法连接|连接失败|重试中)"),
    re.compile(r"\b\d+\.\d+\.\d+\b.*(?:插件|plugin)", re.IGNORECASE),
    re.compile(r"^\[[\w.]+\]\s*\S+:\d+", re.MULTILINE),          # [module:123]
    re.compile(r"^\s*at\s+\S+\s*\(", re.MULTILINE),               # 堆栈帧
    re.compile(r"(sqlite3|OperationalError|database is locked)", re.IGNORECASE),
)

#: 纯媒体/表情占位。整条消息只有这些时没有学习价值
_MEDIA_ONLY = re.compile(
    r"^\s*(?:\[(?:图片|视频|音频|语音|文件|表情|Image|Video|Record|File|Face|"
    r"At|Reply|ComponentType[^\]]*)\][\s，,。.]*)+$",
    re.IGNORECASE,
)

#: 纯符号/单字符（「？」「。」「+1」这类留着当反馈信号可以，但不该当"知识"）
_SYMBOL_ONLY = re.compile(r"^[\s\W_]+$")

#: 学习素材的最小长度。太短没法提炼出可用信息
MIN_LEARNABLE_LEN = 2


def should_learn(text: str) -> tuple[bool, str]:
    """判断一段文本是否适合作为学习素材。

    Returns:
        ``(是否可用, 原因)``。不可用时原因用于日志与面板排查。
    """
    raw = str(text or "")
    if not raw.strip():
        return False, "内容为空"

    t = raw.strip()
    if len(t) < MIN_LEARNABLE_LEN:
        return False, "内容过短"

    if _MEDIA_ONLY.match(t):
        return False, "只有媒体/表情占位，没有文字"

    if _SYMBOL_ONLY.match(t):
        return False, "只有符号，没有实际内容"

    if _CMD_PREFIX.match(t):
        return False, "命令"

    if t.lower().strip("。.!！?？") in _BARE_COMMANDS:
        return False, "裸命令词"

    for pat in _CMD_GUIDANCE:
        if pat.search(t):
            return False, "命令引导语"

    for pat in _SYSTEM_OUTPUT:
        if pat.search(t):
            return False, "系统/报错输出"

    return True, ""
