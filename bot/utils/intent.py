"""Deterministic intent / entity detectors.

Pure functions that run *before* the LLM is invoked. They answer:
- Did the user's message contain an explicit clock time?
- Did it contain an explicit date or weekday?
- Did it specify a meeting mode (Meet/in-person)?

These let us GATE the LLM:
* If the user did NOT state a clock time, but the LLM returns one anyway,
  we know the LLM is hallucinating — reject it.

Anti-hallucination guard — eliminates the bug where "today" or
"set a gmeet with X" gets silently completed with `now + 0` as the start
time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple


# ── Compiled once at import (CPython caches these) ─────────────
# Clock times: "6 PM", "6pm", "6:30 PM", "18:30", "noon", "midnight".
# Excludes bare numbers like "3" which could be anything.
_CLOCK_RX = re.compile(
    r"""(?ix)
    \b(
        # 12-hour with am/pm
        (?:0?[1-9]|1[0-2])           # hour 1-12
        (?::[0-5]\d)?                # optional :MM
        \s*
        (?:a\.?m\.?|p\.?m\.?|am|pm)  # am/pm marker
        |
        # 24-hour (HH:MM)
        (?:[01]?\d|2[0-3]):[0-5]\d
        |
        # word forms
        noon | midnight
    )\b
    """
)

# Date hints: weekdays, "tomorrow", "today", "next week", explicit dates.
_DATE_RX = re.compile(
    r"""(?ix)
    \b(
        today | tomorrow | tonight |
        yesterday | day\s+after\s+tomorrow |
        mon(?:day)? | tue(?:s(?:day)?)? | wed(?:nesday)? |
        thu(?:r(?:s(?:day)?)?)? | fri(?:day)? | sat(?:urday)? | sun(?:day)? |
        next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday) |
        this\s+(?:week|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday) |
        # numeric date 1-31 followed by month name
        (?:[0-3]?\d)\s+
            (?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|
              jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?) |
        # 2026-05-21 / 21-05-2026 / 21/05/2026 etc.
        \d{1,4}[-/]\d{1,2}[-/]\d{1,4}
    )\b
    """
)

# Mode hints — Meet/Online vs in-person/offline.
_ONLINE_RX = re.compile(
    r"(?i)\b(g[-\s]?meet|google\s*meet|meet\b|zoom|online|video\s*call|virtual)\b"
)
_OFFLINE_RX = re.compile(
    r"(?i)\b(in[-\s]?person|offline|at\s+(?:my\s+)?office|at\s+home|f2f|face[-\s]?to[-\s]?face)\b"
)

# Relative-minute phrasing — "in 5 min", "after 10 minutes".
_RELATIVE_MIN_RX = re.compile(
    r"(?i)\b(?:in|after)\s+(\d{1,3})\s*(?:min(?:ute)?s?|mins?)\b"
)


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def has_explicit_clock_time(text: str) -> bool:
    """True if the message contains a recognisable clock time.

    Used to gate the LLM: if False, any start_time_iso the LLM returns is
    treated as a hallucination and rejected.
    """
    return bool(_CLOCK_RX.search(text or ""))


def has_explicit_date(text: str) -> bool:
    """True if the message contains a date keyword or numeric date.

    Note: 'in 5 minutes' is treated as a relative time and considered to
    have an explicit time anchor — handled by has_relative_minutes().
    """
    return bool(_DATE_RX.search(text or ""))


def extract_mode_hint(text: str) -> str | None:
    """Return 'online' / 'offline' / None depending on detected mode hint."""
    t = text or ""
    if _ONLINE_RX.search(t):
        return "online"
    if _OFFLINE_RX.search(t):
        return "offline"
    return None


def has_relative_minutes(text: str) -> int | None:
    """Return N if the message says 'in N minutes' / 'after N min', else None.

    Server-side path that bypasses the LLM entirely for the most common
    reminder pattern.
    """
    m = _RELATIVE_MIN_RX.search(text or "")
    return int(m.group(1)) if m else None


def has_any_time_anchor(text: str) -> bool:
    """Cheap check: does the message contain ANY time signal at all?

    Returns True if there's a clock time, a date keyword, or a relative
    minutes phrase. Used to decide whether to ask the user 'when?'.
    """
    return (
        has_explicit_clock_time(text)
        or has_explicit_date(text)
        or has_relative_minutes(text) is not None
    )


# ═══════════════════════════════════════════════════════════════
# Deterministic clock+date extractor
# ═══════════════════════════════════════════════════════════════
# Used ONLY for parsing a user's *direct* reply to "when?" — never to
# invent a time. Small models fumble obvious phrases ("5pm today",
# "tomorrow 12 PM"); this resolves them in <1ms with zero network.

_RX_12H = re.compile(
    r"(?ix)\b(0?[1-9]|1[0-2])(?:[:\.]([0-5]\d))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)\b"
)
_RX_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RX_NOON = re.compile(r"(?i)\bnoon\b")
_RX_MIDNIGHT = re.compile(r"(?i)\bmidnight\b")

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def _extract_hour_minute(text: str) -> Optional[Tuple[int, int]]:
    if _RX_NOON.search(text):
        return 12, 0
    if _RX_MIDNIGHT.search(text):
        return 0, 0
    m = _RX_12H.search(text)
    if m:
        h = int(m.group(1)) % 12
        if re.sub(r"[.\s]", "", m.group(3)).lower() == "pm":
            h += 12
        return h, int(m.group(2) or 0)
    m = _RX_24H.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _apply_date_hint(now_local: datetime, text: str, hh: int, mm: int) -> datetime:
    base = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    low = text.lower()
    if "day after tomorrow" in low:
        return base + timedelta(days=2)
    if "tomorrow" in low or "tmrw" in low or "tmr" in low:
        return base + timedelta(days=1)
    if "tonight" in low or "this evening" in low or "today" in low:
        return base
    next_week = "next" in low
    for name, target_dow in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            delta = (target_dow - now_local.weekday()) % 7
            if delta == 0 and not (base > now_local and not next_week):
                delta = 7
            if next_week and delta < 7:
                delta += 7
            return base + timedelta(days=delta)
    return base if base > now_local else base + timedelta(days=1)


def extract_meeting_datetime(text: str, now_local: datetime) -> Optional[datetime]:
    """Return a concrete aware datetime from an explicit clock time, else None.

    Returns None when there's no clock time in the text — we never invent one.
    """
    if not text:
        return None
    hm = _extract_hour_minute(text)
    if hm is None:
        return None
    return _apply_date_hint(now_local, text, hm[0], hm[1])
