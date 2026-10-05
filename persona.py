"""按群人设：让每个群用不同的人格，或跟随 AstrBot 全局设置。

背景
----

AstrBot 的人设（persona）是**全局按会话**的，`on_llm_request` 触发时
人设文本已经由 `astr_main_agent._ensure_persona_and_skills` 写进
`req.system_prompt`。插件能拿到这个字段，所以可以按群覆盖。

语义
----

- ``follow``（默认）：一个字都不动，完全用 AstrBot 设置的人设。
- ``custom``：用本群自定义人设**替换** AstrBot 人设那一段。

为什么是"替换"而不是"追加"
--------------------------

追加的话，全局人设和群人设会同时在系统提示里，模型两边都看得到，
经常表现成"人设串味"（一会儿按全局、一会儿按群）。用户要的是
「这个群用这个人设」，那就应该只有一份人格定义。

替换的实现要**精确定位** AstrBot 人设那一段，不能整段 `system_prompt` 覆盖掉
——那里还有技能说明、工具说明、平台约束等框架内容，

做法是从 AstrBot 拿到当前人设原文，在 `system_prompt` 里做一次精确子串替换；
找不到就退化为"前置我方人设 + 明确声明以它为准"，并在返回值里标注
``replaced=False``，方便排查。

纯函数，无 IO。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 自定义人设的长度上限。人设是系统提示的一部分，太长会挤掉上下文预算。
MAX_PERSONA_CHARS = 4000

#: 无法精确替换时用的分隔声明
OVERRIDE_HEADER = (
    "# 本群专属人设（优先级最高，与下方任何全局人设冲突时以本节为准）"
)


@dataclass
class ApplyResult:
    """一次人设应用的结果，便于面板/日志解释。"""

    applied: bool
    mode: str
    #: True = 精确替换掉了 AstrBot 人设那段；False = 退化为前置覆盖
    replaced: bool = False
    reason: str = ""
    before_len: int = 0
    after_len: int = 0


def clamp_persona_text(text: str) -> str:
    """规整人设文本：去首尾空白、限长。"""
    t = str(text or "").strip()
    if len(t) > MAX_PERSONA_CHARS:
        t = t[:MAX_PERSONA_CHARS].rstrip()
    return t


def build_system_prompt(*, system_prompt: str, mode: str, persona_text: str,
                        global_persona: str = "") -> tuple[str, ApplyResult]:
    """按配置算出最终的系统提示。

    Args:
        system_prompt: AstrBot 已经组装好的系统提示（含人设）。
        mode: ``follow`` 或 ``custom``。
        persona_text: 本群自定义人设正文。
        global_persona: AstrBot 当前人设原文，用于精确定位待替换段落。
            为空时无法精确替换，会退化为前置覆盖。

    Returns:
        ``(最终系统提示, 结果说明)``。
    """
    base = str(system_prompt or "")
    m = str(mode or "follow").strip().lower()
    text = clamp_persona_text(persona_text)

    if m != "custom":
        return base, ApplyResult(applied=False, mode="follow",
                                 reason="跟随 AstrBot 全局人设",
                                 before_len=len(base), after_len=len(base))
    if not text:
        return base, ApplyResult(applied=False, mode="custom",
                                 reason="自定义人设为空，按跟随处理",
                                 before_len=len(base), after_len=len(base))

    # 1) 能拿到全局人设原文 → 精确替换，保留框架附加的其他内容
    gp = str(global_persona or "").strip()
    if gp and gp in base:
        out = base.replace(gp, text, 1)
        return out, ApplyResult(applied=True, mode="custom", replaced=True,
                                reason="已替换 AstrBot 人设段落",
                                before_len=len(base), after_len=len(out))

    # 2) 拿不到 / 定位不到 → 前置覆盖，并明确声明优先级
    block = f"{OVERRIDE_HEADER}\n\n{text}"
    out = f"{block}\n\n{base}" if base.strip() else block
    return out, ApplyResult(
        applied=True, mode="custom", replaced=False,
        reason=("未能定位 AstrBot 人设原文，已改为前置覆盖"
                "（会同时存在两份人设描述，建议检查 AstrBot 是否用了非标准人设）"),
        before_len=len(base), after_len=len(out),
    )
