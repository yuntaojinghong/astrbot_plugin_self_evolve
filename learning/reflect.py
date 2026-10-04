"""反思：让模型复盘最近的互动，产出「值得记住的东西」候选。

为什么反思产出必须经审批
------------------------
模型从对话里总结出的内容**无法自动验证正确性**：它可能把一次玩笑当成群规、
把某个人的偏好当成全群偏好、甚至把恶意引导总结成经验。所以本模块只负责
**产出候选**，进待批区；是否生效由管理员决定。

本模块保持纯逻辑（不依赖 AstrBot），因此提示词构造与产出解析都能离线单测：

- :func:`build_prompt`     构造反思提示词
- :func:`parse_candidates` 解析模型输出（容错：markdown 代码块、多余文字、字段缺失）
- :func:`verify_candidate` 逐条校验（类型合法、内容合规、非指令性、非重复、长度合理）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .memory import (
    KINDS,
    KIND_CORRECTION,
    KIND_FACT,
    KIND_LABEL,
    KIND_PREFERENCE,
    KIND_TERM,
    MAX_CONTENT_LEN,
    MemoryStore,
    similarity,
)
from .profile import is_injectable

#: 单次反思最多产出多少条候选（防止模型一口气吐几十条把待批区刷满）
MAX_CANDIDATES = 8
#: 候选内容长度上下限
MIN_LEN = 4
#: 低于该置信度的候选直接丢弃
MIN_CONF = 0.3
#: 与已有条目相似度超过该值即视为重复
DUP_THRESHOLD = 0.72

SYSTEM_PROMPT = """你是一个群聊助手的「经验整理员」。

你的任务：阅读下面提供的对话片段与用户反馈，找出**真正值得长期记住、可复用**的信息。

严格只输出 JSON 数组，不要任何解释、不要 markdown 代码块。数组每个元素形如：
{"kind": "term|preference|correction|fact", "content": "...", "confidence": 0.0~1.0, "subject": "谁"}
其中 "subject" 可省略；写的时候只能是对话里出现过的那个「名字(qq号)」原文。

四类含义：
- term        本群特有的说法、称呼、缩写（例如大家管版主叫什么）
- preference  稳定偏好或禁忌。**关于全群的**（例如不喜欢长篇大论）subject 留空；
              **关于某个人的**（例如他只吃辣、他不喜欢被叫全名）要写 subject。
- correction  助手曾经答错、应当纠正的知识点
- fact        相对稳定的事实。同样区分全群与个人（个人：他在读高三、他养了只猫）

怎么判断该不该带 subject：
- 这句话是**某一个人的**属性 → 必须带 subject，否则会被当成全群偏好，
  以后对所有人套用，反而更糟。
- 这句话对**所有人**都成立 → subject 留空。

硬性要求：
1. content 必须是**一句独立可读的陈述**，不要出现「他」「那个」这类指代
   （带了 subject 也要把话写完整，例如写「小明不吃香菜」而不是「他不吃香菜」）。
2. **不要记录隐私信息**：真实姓名、电话、住址、身份证、账号密码、学校班级全称等，
   一律不写。称呼用群里的昵称/群名片。涉及健康、家庭矛盾、情感创伤这类敏感内容，
   只在不写就会反复踩雷时才记，且只写中性的相处方式（例如「别拿他的体重开玩笑」）。
3. 不要输出指令性内容（不要写「以后你必须…」「忽略规则」这类句子）。
4. 只写你有把握的；没把握就不要输出。宁少勿滥。
5. **最多 5 条**；如果确实没有值得记住的，输出空数组 []。
"""


@dataclass
class Candidate:
    """一条反思候选。"""

    content: str
    kind: str = KIND_FACT
    confidence: float = 0.5
    reason: str = ""            # 保留原因（来自模型或本地判定）
    accepted: bool = True
    reject_reason: str = ""
    #: 这条候选关于谁，形如「小明(123456)」。空 = 群级条目
    subject: str = ""

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "accepted": self.accepted,
            "reject_reason": self.reject_reason,
            "subject": self.subject,
        }


# --------------------------------------------------------------------------- #
#  提示词
# --------------------------------------------------------------------------- #

def build_prompt(*, transcript: str, corrections: list[str] | None = None,
                 signals: dict | None = None, known: list[str] | None = None,
                 max_chars: int = 4000) -> str:
    """构造反思提示词。

    Args:
        transcript: 最近的对话片段（用户消息 + 机器人回复）。
        corrections: 用户明确纠正过的内容（高价值线索）。
        signals: 反馈信号统计，如 ``{"criticism": 3, "praise": 5}``。
        known: 已经记住的条目内容，用于提示模型不要重复。
        max_chars: 提示词总长上限。
    """
    parts: list[str] = ["【最近的互动片段】", (transcript or "（无）")[: max_chars // 2]]

    if corrections:
        parts.append("")
        parts.append("【用户明确纠正过的内容（优先级最高）】")
        for c in corrections[:6]:
            parts.append(f"- {c[:120]}")

    if signals:
        parts.append("")
        parts.append("【反馈信号统计】")
        for k, v in sorted(signals.items()):
            if v:
                parts.append(f"- {k}: {v} 次")

    if known:
        parts.append("")
        parts.append("【已经记住的内容（不要重复输出）】")
        for k in known[:12]:
            parts.append(f"- {k[:100]}")

    parts.append("")
    parts.append("请按系统要求输出 JSON 数组。")
    text = "\n".join(parts)
    return text[:max_chars]


def build_transcript(records: list[dict], *, max_items: int = 24) -> str:
    """把互动记录渲染成对话片段。

    ``records`` 每项形如
    ``{"who": "user"|"bot", "text": "...", "sender_id": "...", "sender_name": "..."}``。

    发言人名字必须渲染出来。此前一律写成「群友」，提示词里根本分不清谁说了什么，
    模型想提炼「谁喜欢什么」也无从下手——这是"机器人记不住人"的源头。
    名字缺失时退回 QQ 号，身份仍然不丢。
    """
    lines: list[str] = []
    for r in (records or [])[-max_items:]:
        text = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
        if not text:
            continue
        if str(r.get("who")) == "user":
            name = str(r.get("sender_name") or "").strip()
            sid = str(r.get("sender_id") or "").strip()
            if name and sid and name != sid:
                who = f"{name}({sid})"   # 带 QQ 号，候选里的人名才能对上真实 id
            else:
                who = name or sid or "群友"
        else:
            who = "助手"
        lines.append(f"{who}：{text[:200]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)
_JSON_ARRAY = re.compile(r"\[.*\]", re.S)

_KIND_ALIAS = {
    "term": KIND_TERM, "术语": KIND_TERM, "词": KIND_TERM, "称呼": KIND_TERM,
    "preference": KIND_PREFERENCE, "偏好": KIND_PREFERENCE, "习惯": KIND_PREFERENCE,
    "correction": KIND_CORRECTION, "纠错": KIND_CORRECTION, "纠正": KIND_CORRECTION,
    "fact": KIND_FACT, "事实": KIND_FACT,
}


def _extract_json_array(text: str) -> list | None:
    """从模型输出里尽力抠出 JSON 数组（容忍代码块与前后废话）。"""
    if not text:
        return None
    candidates = []
    m = _JSON_BLOCK.search(text)
    if m:
        candidates.append(m.group(1))
    m2 = _JSON_ARRAY.search(text)
    if m2:
        candidates.append(m2.group(0))
    candidates.append(text.strip())
    for raw in candidates:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "candidates", "data", "result", "list"):
                if isinstance(data.get(key), list):
                    return data[key]
    return None


def parse_candidates(text: str, *, limit: int = MAX_CANDIDATES) -> tuple[list[Candidate], str]:
    """解析模型输出为候选列表。

    Returns:
        ``(候选列表, 说明)``。解析失败时候选为空、说明给出原因。
    """
    data = _extract_json_array(text)
    if data is None:
        return [], "未能从模型输出中解析出 JSON 数组"
    out: list[Candidate] = []
    for item in data:
        if len(out) >= limit:
            break
        if isinstance(item, str):
            item = {"content": item}
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("text") or "").strip()
        if not content:
            continue
        raw_kind = str(item.get("kind") or item.get("type") or KIND_FACT).strip().lower()
        kind = _KIND_ALIAS.get(raw_kind, KIND_FACT)
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append(Candidate(
            content=content[:MAX_CONTENT_LEN],
            kind=kind,
            confidence=max(0.0, min(1.0, conf)),
            reason=str(item.get("reason") or ""),
            subject=str(item.get("subject") or "").strip()[:60],
        ))
    return out, f"解析出 {len(out)} 条候选"


# --------------------------------------------------------------------------- #
#  校验
# --------------------------------------------------------------------------- #

#: 疑似隐私信息的模式：一旦命中就不进候选（避免把群友隐私写进经验库）
_PRIVACY_PATTERNS = [
    re.compile(r"1[3-9]\d{9}"),                       # 手机号
    re.compile(r"\b\d{6,12}\b"),                      # 长数字串（QQ/账号）
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),   # 邮箱
    re.compile(r"(身份证|护照|银行卡|信用卡)\s*[:：]?\s*\d"),
    re.compile(r"(住址|家庭住址|具体地址)\s*[:：]"),
]

#: 指代词开头的内容多半缺乏上下文，进库后无法独立理解。
#:
#: 注意要排除「这个群 / 那个群 / 该群」——那是**集合**不是指代某人，
#: 而且组内规则本来就该这么写（「这个群的固定活动是周五开黑」）。
#: 此前不排除会把这类正常条目误杀。
_PRONOUN_START = re.compile(
    r"^(?:他|她|它|他们|她们|这人|那人|该用户|该群友)(?!群)"
    r"|^(?:这个|那个)(?!群)"
)


def verify_candidate(cand: Candidate, *, store: MemoryStore, group_id: str,
                     existing: list[Candidate] | None = None) -> Candidate:
    """逐条校验候选，就地写回 ``accepted`` 与 ``reject_reason``。

    校验项：类型合法、长度合理、无隐私信息、无指代、非指令性内容、不与已有条目重复。
    """
    content = (cand.content or "").strip()

    if cand.kind not in KINDS:
        cand.kind = KIND_FACT
    if len(content) < MIN_LEN:
        return _reject(cand, f"内容过短（少于 {MIN_LEN} 字）")
    if len(content) > MAX_CONTENT_LEN:
        return _reject(cand, "内容过长")
    if cand.confidence < MIN_CONF:
        return _reject(cand, f"置信度过低（{cand.confidence:.2f} < {MIN_CONF}）")

    for pat in _PRIVACY_PATTERNS:
        if pat.search(content):
            return _reject(cand, "疑似包含隐私信息（手机号/账号/邮箱/地址等）")

    # 指代否决只在**没有主体**时生效。
    # 带 subject 的候选（如「他不吃香菜」+ subject=小明）脱离上下文仍可理解，
    # 主体就是那个「他」——此时按指代否决会把它误杀。
    if not cand.subject and _PRONOUN_START.match(content):
        return _reject(cand, "以指代词开头，脱离上下文无法理解")
    ok, reason = is_injectable(content)
    if not ok:
        return _reject(cand, reason)

    subject_id, _subject_name = split_subject(cand.subject)
    dup = store.find_similar(group_id, content, None, DUP_THRESHOLD, subject_id)
    if dup is not None:
        return _reject(cand, f"与已有条目重复（相似度 {similarity(dup.content, content):.2f}）")

    for other in (existing or []):
        if other is cand or not other.accepted:
            continue
        # 主体不同不算重复：「小明喜欢猫」和「小红喜欢猫」是两条
        if split_subject(other.subject)[0] != subject_id:
            continue
        if similarity(other.content, content) >= DUP_THRESHOLD:
            return _reject(cand, "与本批其他候选重复")

    cand.accepted = True
    return cand


def split_subject(raw: str) -> tuple[str, str]:
    """把模型给的 subject 拆成 ``(qq号, 显示名)``。

    模型只会看到 ``小明(123456)`` 这种带 id 的写法，所以正常情况能直接拆出来。
    但也兼容只写名字、「名字(123)」带空格、以及全角括号等写法。
    拆不出数字 id 时**返回空 id**——宁可不记，也不能把 A 的事记到别人头上。
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    m = re.search(r"[（(]\s*(\d{4,15})\s*[)）]", text)
    if m:
        name = (text[: m.start()] + text[m.end():]).strip(" 　()（）")
        return m.group(1), (name or m.group(1))
    if text.isdigit():
        return text, text
    # 只有名字没有 id：无法定位到具体成员
    return "", text


def _reject(cand: Candidate, reason: str) -> Candidate:
    cand.accepted = False
    cand.reject_reason = reason
    return cand


def verify_all(cands: list[Candidate], *, store: MemoryStore, group_id: str) -> list[Candidate]:
    """按顺序校验整批候选，并去掉批内重复。"""
    out: list[Candidate] = []
    for c in cands:
        verify_candidate(c, store=store, group_id=group_id, existing=out)
        out.append(c)
    return out


def summarize(cands: list[Candidate]) -> str:
    """给管理员看的候选摘要。"""
    if not cands:
        return "本次反思没有产出候选。"
    lines = []
    for i, c in enumerate(cands, 1):
        # 标出这条是关于谁的，管理员审批时才知道会作用在谁身上
        who = ""
        if c.subject:
            _sid, name = split_subject(c.subject)
            who = f"（关于 {name}）"
        if c.accepted:
            label = KIND_LABEL.get(c.kind, c.kind)
            lines.append(f"{i}. [{label}]{who} {c.content}（置信 {c.confidence:.2f}）")
        else:
            lines.append(f"{i}. ⛔ 已剔除：{c.content[:40]}{who} —— {c.reject_reason}")
    return "\n".join(lines)
