"""学习包：与 AstrBot 解耦的纯逻辑层。

这一层刻意不 import 任何 astrbot 模块，因此可以用桩环境离线跑回归测试，
也可以在面板、命令、反思等不同入口复用同一套规则。
"""

from .bandit import (
    ArmStat,
    ArmUpdate,
    BASELINE_INDEX,
    Choice,
    DIMENSIONS,
    DIMENSION_HINT,
    DIMENSION_PROMPT,
    N_LEVELS,
    StrategyBandit,
    clamp,
)
from .feedback import (
    Feedback,
    SIG_CONTINUE,
    SIG_CORRECTION,
    SIG_CRITICISM,
    SIG_NONE,
    SIG_PRAISE,
    SIG_REASK,
    SIG_STOP,
    SIG_THANKS,
    SIGNAL_POLARITY,
    SIGNAL_WEIGHT,
    STRONG_SIGNAL_WEIGHT,
    extract_correction,
    looks_like_question,
    parse as parse_feedback,
    similarity as feedback_similarity,
)
from .memory import (
    AddResult,
    Entry,
    KIND_CORRECTION,
    KIND_FACT,
    KIND_LABEL,
    KIND_PREFERENCE,
    KIND_TERM,
    KINDS,
    MAX_CONTENT_LEN,
    MIN_CONFIDENCE,
    MemoryStore,
    SOURCE_ADMIN,
    SOURCE_LABEL,
    SOURCE_REFLECT,
    SOURCE_USER,
    similarity as memory_similarity,
)
from .profile import (
    CLOSE_TAG,
    OPEN_TAG,
    RenderResult,
    is_injectable,
    render_injection,
    render_style,
    render_summary,
)

__all__ = [
    # bandit
    "ArmStat", "ArmUpdate", "BASELINE_INDEX", "Choice", "DIMENSIONS",
    "DIMENSION_HINT", "DIMENSION_PROMPT", "N_LEVELS", "StrategyBandit", "clamp",
    # feedback
    "Feedback", "SIG_CONTINUE", "SIG_CORRECTION", "SIG_CRITICISM", "SIG_NONE",
    "SIG_PRAISE", "SIG_REASK", "SIG_STOP", "SIG_THANKS", "SIGNAL_POLARITY",
    "SIGNAL_WEIGHT", "STRONG_SIGNAL_WEIGHT", "extract_correction",
    "looks_like_question", "parse_feedback", "feedback_similarity",
    # memory
    "AddResult", "Entry", "KIND_CORRECTION", "KIND_FACT", "KIND_LABEL",
    "KIND_PREFERENCE", "KIND_TERM", "KINDS", "MAX_CONTENT_LEN",
    "MIN_CONFIDENCE", "MemoryStore", "SOURCE_ADMIN", "SOURCE_LABEL",
    "SOURCE_REFLECT", "SOURCE_USER", "memory_similarity",
    # profile
    "CLOSE_TAG", "OPEN_TAG", "RenderResult", "is_injectable",
    "render_injection", "render_style", "render_summary",
]
