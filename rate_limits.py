"""Helpers for translating provider rate-limit hints into user-facing waits."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


_DURATION_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milliseconds?|ms|minutes?|mins?|min|m|seconds?|secs?|sec|s|hours?|hrs?|h)",
    re.IGNORECASE,
)
_RETRY_HINT = re.compile(r"(?:try again in|retry after)\s+([^.;,]+)", re.IGNORECASE)


def _parse_duration(value: str) -> float | None:
    matches = list(_DURATION_TOKEN.finditer(value))
    if not matches:
        return None

    seconds = 0.0
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        if unit in {"ms", "millisecond", "milliseconds"}:
            seconds += amount / 1000
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            seconds += amount * 60
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            seconds += amount * 3600
        else:
            seconds += amount
    return seconds


def retry_after_seconds(headers, error_message: str = "") -> int | None:
    """Read retry/reset hints from common HTTP and Groq response headers."""
    values: list[float] = []

    retry_header = headers.get("Retry-After") or headers.get("retry-after")
    if retry_header:
        retry_text = str(retry_header).strip()
        try:
            values.append(float(retry_text))
        except ValueError:
            duration = _parse_duration(retry_text)
            if duration is not None:
                values.append(duration)
            else:
                try:
                    retry_at = parsedate_to_datetime(retry_text)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    values.append((retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass

    for header_name in (
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    ):
        value = headers.get(header_name)
        if value:
            duration = _parse_duration(str(value))
            if duration is not None:
                values.append(duration)

    hint = _RETRY_HINT.search(error_message)
    if hint:
        duration = _parse_duration(hint.group(1))
        if duration is not None:
            values.append(duration)

    if not values:
        return None
    return max(1, math.ceil(max(values)))


def rate_limit_message(seconds: int | None) -> str:
    if seconds is None:
        return "⏳ وصل البوت إلى حد الطلبات الحالي. حاول مجدداً بعد قليل."
    if seconds >= 60:
        minutes = math.ceil(seconds / 60)
        wait = f"حوالي {minutes} دقيقة"
    else:
        wait = f"{seconds} ثانية"
    return f"⏳ وصل البوت إلى حد الطلبات الحالي. حاول مجدداً بعد {wait}."