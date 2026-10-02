"""反馈归因：把「用户的下一条消息」翻译成对上一条回复的评分。

为什么要单独一个模块
--------------------
自学习系统最容易出错的地方不是算法，而是**学错了对象**：
用户说「不对」时，到底是在否定机器人的上一条回复，还是在否定别人？
用户说「谢谢」时，是真的满意，还是只是礼貌性收尾？

本模块只做一件事，且做到可解释：
把一条用户消息解析成结构化的 :class:`Feedback`，附带
「是什么信号、正负、可信度（权重）」三要素。所有判断都是本地正则/词表，
零 token、零延迟，并且**可以被单测穷举**。

设计取舍
--------
- **否定优先**：先判负面再判正面。「不对，这样不行」不能被「行」这类正向词救回来。
- **礼貌词低权重**：「谢谢」「好的」在中文里常常只是收尾语，权重必须低于明确纠错。
- **隐式信号最弱**：追问、续聊这类行为噪声很大，权重给最低，且不单独触发学习。
- **不猜**：判定不了就返回 ``NONE``，宁可不学也不学错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
#  信号类型
# --------------------------------------------------------------------------- #

SIG_NONE = "none"
SIG_PRAISE = "praise"            # 「对了」「这个好」——明确满意
SIG_THANKS = "thanks"            # 「谢谢」「好的」——礼貌收尾，弱正向
SIG_CRITICISM = "criticism"      # 「不对」「错了」「你搞错了」——明确否定
SIG_CORRECTION = "correction"    # 「不是X，是Y」——给出更正内容，最强信号
SIG_REASK = "reask"              # 换个说法再问一遍——隐式：上次没解决问题
SIG_CONTINUE = "continue"        # 顺着聊下去——隐式：可接受
SIG_STOP = "stop"                # 「算了」「不用了」——隐式：放弃/不满

#: 每种信号的默认权重（正负之外的「可信度」）。
#: 数值不是拍脑袋：明确的纠错/否定必须显著高于礼貌词，否则
#: 「谢谢」会淹没「你答错了」，学习立刻退化成讨好。
SIGNAL_WEIGHT = {
    SIG_CORRECTION: 1.00,
    SIG_CRITICISM: 0.80,
    SIG_PRAISE: 0.70,
    SIG_REASK: 0.35,
    SIG_STOP: 0.30,
    SIG_THANKS: 0.25,
    SIG_CONTINUE: 0.15,
    SIG_NONE: 0.0,
}

#: 信号极性：给策略打分时用它决定 +/-。
SIGNAL_POLARITY = {
    SIG_CORRECTION: -1,
    SIG_CRITICISM: -1,
    SIG_STOP: -1,
    SIG_REASK: -1,
    SIG_PRAISE: +1,
    SIG_THANKS: +1,
    SIG_CONTINUE: +1,
    SIG_NONE: 0,
}

#: 只有达到这个权重的信号才允许单独驱动一次策略更新。
#: 隐式信号（reask/stop/continue）权重低于它，必须累计到样本门槛才生效。
STRONG_SIGNAL_WEIGHT = 0.50

# --------------------------------------------------------------------------- #
#  词表
# --------------------------------------------------------------------------- #

_PRAISE = [
    "对了", "没错", "就是这个", "答对了", "正确", "可以了", "这样就行",
    "有道理", "厉害", "牛", "棒", "赞", "靠谱", "懂了", "明白了",
    "完美", "到位", "准", "不错",
]
_THANKS = ["谢谢", "多谢", "感谢", "谢了", "3q", "thanks", "thank you", "thx", "好的", "好滴", "ok", "OK", "收到"]
_CRITICISM = [
    "不对", "错了", "答错", "搞错", "弄错", "不是这个", "不太对", "不正确",
    "你错", "胡说", "乱说", "瞎说", "扯淡", "离谱", "别瞎", "别乱",
    "没用", "不行", "重来", "再想想", "看清楚", "答非所问", "废话",
    "wrong", "incorrect", "nonsense",
]
_REASK = [
    "还是没", "没回答", "没答", "没懂", "没说清", "再说一遍", "重说",
    "我问的是", "你理解错", "我问你", "再答一次", "重新回答",
]
_STOP = ["算了", "不用了", "不必了", "就这样吧", "不聊了", "当我没问", "拉倒", "罢了"]

#: 「不是 A，是 B」这类纠正句式——能直接抽出正确内容，是最有价值的学习材料。
_CORRECTION_PATTERNS = [
    re.compile(r"不是\s*(?P<wrong>[^，,。；;\n]{1,30}?)\s*[，,]\s*是\s*(?P<right>[^。；;\n]{1,60})"),
    re.compile(r"不是\s*(?P<wrong>[^，,。；;\n]{1,30}?)\s*[，,]\s*(?:应该)?(?:而)?是\s*(?P<right>[^。；;\n]{1,60})"),
    re.compile(r"应该是\s*(?P<right>[^。；;\n]{1,60})"),
    re.compile(r"其实(?:是|叫)\s*(?P<right>[^。；;\n]{1,60})"),
    re.compile(r"我们(?:一般)?(?:叫|说|称)\s*(?P<right>[^。；;\n]{1,60})"),
]


def _compile(words) -> re.Pattern:
    # 长词优先，避免「不错」被「错」抢先匹配成负面
    ordered = sorted({w for w in words if w}, key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in ordered), re.IGNORECASE)


_PRAISE_RE = _compile(_PRAISE)
_THANKS_RE = _compile(_THANKS)
_CRITICISM_RE = _compile(_CRITICISM)
_REASK_RE = _compile(_REASK)
_STOP_RE = _compile(_STOP)


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Feedback:
    """一条用户消息里解析出的反馈。"""

    signal: str = SIG_NONE
    polarity: int = 0
    weight: float = 0.0
    #: 纠正类信号抽出的「正确内容」，可直接沉淀成经验条目
    correction: str = ""
    #: 命中的原文片段，用于向管理员解释「为什么这么判」
    evidence: str = ""
    #: 原始消息（截断保存，供面板展示）
    raw: str = ""
    #: 额外说明（例如「礼貌词，权重已降低」）
    notes: list = field(default_factory=list)

    @property
    def is_signal(self) -> bool:
        return self.signal != SIG_NONE and self.polarity != 0

    @property
    def is_strong(self) -> bool:
        """是否强到可以单独驱动一次策略更新。"""
        return self.is_signal and self.weight >= STRONG_SIGNAL_WEIGHT

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "polarity": self.polarity,
            "weight": self.weight,
            "correction": self.correction,
            "evidence": self.evidence,
            "note": "；".join(self.notes),
        }


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #

def _find(patterns: re.Pattern, text: str) -> str:
    m = patterns.search(text)
    return m.group(0) if m else ""


def extract_correction(text: str) -> str:
    """抽出纠正句式里的「正确内容」；抽不到返回空串。"""
    if not text:
        return ""
    for pat in _CORRECTION_PATTERNS:
        m = pat.search(text)
        if m:
            right = (m.groupdict().get("right") or "").strip()
            right = right.strip("「」\"'‘’“”（）() ")
            if right:
                return right[:200]
    return ""


def looks_like_question(text: str) -> bool:
    """粗判一句话是不是在提问（用于隐式「追问」判定）。"""
    if not text:
        return False
    t = text.strip()
    if t.endswith(("?", "？")):
        return True
    return bool(re.search(r"(什么|怎么|为什么|哪|谁|多少|是不是|能不能|有没有|如何|咋)", t))


def _token_overlap(a: str, b: str) -> float:
    """字符级 Jaccard 近似，用于判断「是不是在问同一件事」。

    刻意用字符而不是分词：插件不引入第三方依赖，也不该假设有分词器。
    对中文短句，字符二元组重叠已经足够区分「同一问题的另一种问法」和「换了个话题」。
    """
    a = re.sub(r"[\s，,。！!？?；;：:\"'「」（）()]+", "", a or "")
    b = re.sub(r"[\s，,。！!？!?；;：:\"'「」（）()]+", "", b or "")
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / float(len(ga | gb))


def parse(
    text: str,
    *,
    prev_user_text: str = "",
    is_reply_to_bot: bool = False,
) -> Feedback:
    """解析一条用户消息，判断它对机器人上一条回复是什么态度。

    Args:
        text: 用户这条消息的内容。
        prev_user_text: 该用户**上一条**消息内容（用于判断是不是在追问同一件事）。
        is_reply_to_bot: 这条消息是否明确是对机器人回复的引用/回应。

    Returns:
        :class:`Feedback`；判定不了时 ``signal == SIG_NONE``。
    """
    raw = (text or "").strip()
    fb = Feedback(raw=raw[:200])
    if not raw:
        return fb

    # ---- 0. 礼貌回应「谢谢/好的」：引用机器人的话时才算数，否则权重再降 ----
    correction = extract_correction(raw)
    criticism = _find(_CRITICISM_RE, raw)
    praise = _find(_PRAISE_RE, raw)
    thanks = _find(_THANKS_RE, raw)
    reask = _find(_REASK_RE, raw)
    stop = _find(_STOP_RE, raw)

    # ---- 1. 最强：明确否定（含纠正句式）----
    if criticism or (correction and not praise):
        fb.signal = SIG_CRITICISM
        fb.polarity = SIGNAL_POLARITY[SIG_CRITICISM]
        fb.weight = SIGNAL_WEIGHT[SIG_CRITICISM]
        fb.evidence = criticism or correction
        if correction:
            fb.signal = SIG_CORRECTION
            fb.weight = SIGNAL_WEIGHT[SIG_CORRECTION]
            fb.correction = correction
            fb.notes.append("识别为纠正句式，已抽取正确内容")
        return fb

    # ---- 2. 纠正句式（未带否定词，如「应该是 X」）----
    if correction:
        fb.signal = SIG_CORRECTION
        fb.polarity = SIGNAL_POLARITY[SIG_CORRECTION]
        fb.weight = SIGNAL_WEIGHT[SIG_CORRECTION]
        fb.correction = correction
        fb.evidence = correction
        fb.notes.append("识别为补充/更正内容")
        return fb

    # ---- 3. 追问：说明上一次没能解决问题（隐式，负向）----
    if reask:
        fb.signal = SIG_REASK
        fb.polarity = SIGNAL_POLARITY[SIG_REASK]
        fb.weight = SIGNAL_WEIGHT[SIG_REASK]
        fb.evidence = reask
        fb.notes.append("隐式信号：明确表示上次没答到点上")
        return fb

    # ---- 4. 放弃对话（隐式，负向）----
    if stop:
        fb.signal = SIG_STOP
        fb.polarity = SIGNAL_POLARITY[SIG_STOP]
        fb.weight = SIGNAL_WEIGHT[SIG_STOP]
        fb.evidence = stop
        fb.notes.append("隐式信号：用户放弃继续追问")
        return fb

    # ---- 5. 明确肯定 ----
    if praise:
        fb.signal = SIG_PRAISE
        fb.polarity = SIGNAL_POLARITY[SIG_PRAISE]
        fb.weight = SIGNAL_WEIGHT[SIG_PRAISE]
        fb.evidence = praise
        return fb

    # ---- 6. 礼貌收尾：权重低，且只有「在回应机器人」时才采信 ----
    if thanks:
        fb.signal = SIG_THANKS
        fb.polarity = SIGNAL_POLARITY[SIG_THANKS]
        fb.weight = SIGNAL_WEIGHT[SIG_THANKS]
        fb.evidence = thanks
        if not is_reply_to_bot:
            fb.notes.append("礼貌词且未明确回应机器人，仅作弱正向参考")
        else:
            fb.weight = min(0.4, fb.weight + 0.15)
            fb.notes.append("礼貌词且明确回应机器人")
        return fb

    # ---- 7. 换种说法重问同一件事 → 隐式「没解决」----
    if prev_user_text and looks_like_question(raw):
        overlap = _token_overlap(raw, prev_user_text)
        if overlap >= 0.34:
            fb.signal = SIG_REASK
            fb.polarity = SIGNAL_POLARITY[SIG_REASK]
            fb.weight = SIGNAL_WEIGHT[SIG_REASK]
            fb.evidence = f"与上一条问题相似度 {overlap:.2f}"
            fb.notes.append("隐式信号：疑似换个说法重问同一问题")
            return fb

    # ---- 8. 顺着聊下去 → 极弱正向 ----
    fb.signal = SIG_CONTINUE
    fb.polarity = SIGNAL_POLARITY[SIG_CONTINUE]
    fb.weight = SIGNAL_WEIGHT[SIG_CONTINUE]
    fb.notes.append("隐式信号：对话继续，未表达不满")
    return fb


def similarity(a: str, b: str) -> float:
    """对外暴露的相似度工具（面板/反思去重也用得上）。"""
    return _token_overlap(a, b)
