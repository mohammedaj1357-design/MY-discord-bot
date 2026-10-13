"""Remove generic assistant filler and developer-only honorific greetings."""

from __future__ import annotations

import re


_CLOSER_PHRASES = (
    r"كيف\s+(?:يمكنني|أستطيع|أقدر)\s+(?:أن\s+)?(?:أساعدك|مساعدتك)(?:\s+(?:الآن|اليوم))?",
    r"كيف\s+أساعدك(?:\s+اليوم)?",
    r"هل\s+(?:تحتاج(?:\s+إلى)?|تود)\s+(?:أي\s+)?مساعدة(?:\s+(?:أخرى|إضافية))?",
    r"هل\s+(?:هناك|يوجد)\s+(?:أي\s+)?شيء\s+آخر",
    r"هل\s+(?:عندك|لديك)\s+(?:أي\s+)?(?:سؤال|استفسار)(?:\s+آخر)?",
    r"إذا\s+كان\s+لديك\s+(?:أي\s+)?(?:سؤال|استفسار)(?:\s+آخر)?(?:\s*[،,]?\s*(?:أخبرني|قل\s+لي))?",
    r"أنا\s+هنا\s+ل(?:مساعدتك|لمساعدة)",
    r"لا\s+تتردد\s+في\s+(?:السؤال|طلب\s+المساعدة)",
    r"أتمنى\s+أن\s+(?:أكون|أني|أنني)\s+قد\s+ساعدتك",
    r"أتمنى\s+أن\s+يكون\s+هذا\s+قد\s+ساعدك",
    r"(?:شكراً|شكرا|شكرًا)\s+لك(?:\s+على\s+سؤالك)?",
    r"thank\s+you(?:\s+for\s+your\s+question)?",
    r"thanks(?:\s+for\s+your\s+question)?",
    r"how\s+can\s+i\s+help(?:\s+you)?(?:\s+today)?",
    r"is\s+there\s+anything\s+else(?:\s+i\s+can\s+help\s+you\s+with)?",
    r"let\s+me\s+know\s+if\s+you\s+need\s+anything\s+else",
    r"feel\s+free\s+to\s+ask(?:\s+if\s+you\s+have\s+more\s+questions)?",
    r"i\s+hope\s+(?:that\s+)?helps",
)
_CLOSER_PATTERNS = tuple(
    re.compile(
        rf"(?:^|(?<=[.!?؟…])\s+)(?:{phrase})[.!?؟…]*\s*$",
        re.IGNORECASE,
    )
    for phrase in _CLOSER_PHRASES
)
_THANKS_PREFIX = re.compile(
    r"^(?:(?:شكراً|شكرا|شكرًا)\s+لك(?:\s+على\s+سؤالك)?|"
    r"thank\s+you(?:\s+for\s+your\s+question)?|"
    r"thanks(?:\s+for\s+your\s+question)?)[!！.،,;؛:؟?\s]+",
    re.IGNORECASE,
)
_DEVELOPER_HONORIFIC_GREETING = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<greeting>"
    r"مرحب(?:ا|اً|ًا)|أهلاً|أهلا|هلا"
    r")\s+يا\s+مولاي\b"
)


def _replace_developer_honorific(match: re.Match) -> str:
    return match.group("indent") + match.group("greeting")


def _is_only_canned_closer(text: str) -> bool:
    return any(
        (match := pattern.search(text.strip())) is not None and match.start() == 0
        for pattern in _CLOSER_PATTERNS
    )


def clean_ai_response(
    text: str,
    *,
    avoid_honorific_greeting: bool = False,
) -> str:
    """Remove generic filler and, for the developer, a servile greeting.

    Generic phrases are stripped only at the beginning or as a separate final
    sentence. Quoted examples and phrase-only responses are preserved.
    The honorific rule is enabled only for developer replies.
    """
    original = text
    cleaned = text.strip()
    if avoid_honorific_greeting:
        cleaned = _DEVELOPER_HONORIFIC_GREETING.sub(
            _replace_developer_honorific,
            cleaned,
        )

    for _ in range(4):
        for pattern in _CLOSER_PATTERNS:
            match = pattern.search(cleaned)
            if match is None or match.start() == 0:
                continue
            candidate = cleaned[: match.start()].rstrip(" \t\r\n,،;؛—–-")
            if candidate and not _is_only_canned_closer(candidate):
                cleaned = candidate
                break
        else:
            break

    thanks_match = _THANKS_PREFIX.match(cleaned)
    if thanks_match is not None:
        candidate = cleaned[thanks_match.end() :].lstrip()
        if candidate and not _is_only_canned_closer(candidate):
            cleaned = candidate

    return cleaned or original