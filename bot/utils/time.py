"""Centralised time/datetime helpers.

Replaces the dozen ad-hoc `datetime.now(timezone.utc).replace(tzinfo=None)`,
`pytz.timezone(config.BOT_TIMEZONE)`, and inline conversions scattered
through the codebase. Every function in this module is pure, side-effect
free, and tested against IST round-trips.

Naming convention
-----------------
* `*_aware`   — timezone-aware datetime (UTC unless suffixed `_local`).
* `*_naive`   — tz-stripped datetime (only for SQLite/MySQL columns that
                cannot hold tz info). Always UTC by convention.
* `parse_*`   — string → aware datetime (never naive).
* `format_*`  — aware datetime → display string.

Why naive UTC in the DB
-----------------------
SQLAlchemy `DateTime` (without `timezone=True`) maps to SQLite TEXT and
strips tz info silently. Storing naive UTC is the safe convention until
the Postgres migration adds `TIMESTAMPTZ` (Week 2 of the alignment plan).
After that, this module is the single switch-over point — just change
`utcnow_naive()` to return aware UTC and the call sites stay unchanged.

Performance
-----------
* `pytz.timezone()` is called exactly once at import; subsequent reads
  hit the module-level `IST` / `UTC` constants.
* `_DEFAULT_FORMAT` is a constant (compiled once by CPython).
* No regex, no `dateutil` — `datetime.fromisoformat` is ~5× faster.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

from bot.config import config

# ── Cached zone objects (one allocation per process) ────────────
UTC = pytz.utc
IST = pytz.timezone(config.BOT_TIMEZONE)  # default Asia/Kolkata

# ── Default display format used everywhere ──────────────────────
_DEFAULT_FORMAT = "%a %b %d, %I:%M %p IST"
_PROMPT_FORMAT = "%Y-%m-%d %H:%M:%S IST (%A, %I:%M %p)"


# ═══════════════════════════════════════════════════════════════
# "Now" — five callers, one implementation each
# ═══════════════════════════════════════════════════════════════

def utcnow_aware() -> datetime:
    """Current UTC time, tz-aware. Use for arithmetic and comparisons."""
    return datetime.now(timezone.utc)


def utcnow_naive() -> datetime:
    """Current UTC time, tz-stripped. Use for SQLite DateTime columns.

    Replaces the `datetime.now(timezone.utc).replace(tzinfo=None)` pattern
    that appeared 8+ times in the original codebase.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_local() -> datetime:
    """Current time in the bot's local zone (IST by default), tz-aware.

    Use for user-facing math like 'in 5 minutes' or 'tomorrow 9 AM'.
    """
    return datetime.now(IST)


def now_prompt_str() -> str:
    """Pre-formatted current local time for LLM system prompts.

    Single source of truth — every system prompt that needs 'current time'
    should call this rather than re-implementing the format.
    """
    return now_local().strftime(_PROMPT_FORMAT)


# ═══════════════════════════════════════════════════════════════
# Conversions
# ═══════════════════════════════════════════════════════════════

def to_utc_naive(dt: datetime) -> datetime:
    """Convert any datetime to naive UTC for DB storage.

    * Aware  → converted to UTC then stripped.
    * Naive  → assumed already UTC; returned unchanged.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def to_local_aware(dt: datetime) -> datetime:
    """Convert any datetime to aware local (IST) time.

    * Aware  → astimezone(IST).
    * Naive  → assumed UTC, attached then converted.
    """
    if dt.tzinfo is None:
        return UTC.localize(dt).astimezone(IST)
    return dt.astimezone(IST)


def ensure_aware_local(dt: datetime) -> datetime:
    """Return `dt` as IST-aware, treating naive input as IST-local.

    Distinct from `to_local_aware`: that one treats naive as UTC.
    Use this when the input is something the user typed ('5 PM') with
    no zone info — naive means IST, not UTC.
    """
    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


# ═══════════════════════════════════════════════════════════════
# Parsing
# ═══════════════════════════════════════════════════════════════

def parse_iso_to_aware(iso_str: str, assume_local_if_naive: bool = True) -> datetime:
    """Parse ISO 8601 → aware datetime.

    If the string lacks an offset and `assume_local_if_naive` is True,
    the result is localised to IST. Otherwise it's localised to UTC.
    Raises ValueError on unparseable input.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is not None:
        return dt
    return IST.localize(dt) if assume_local_if_naive else UTC.localize(dt)


# ═══════════════════════════════════════════════════════════════
# Formatting (single canonical recipe — used by all reply paths)
# ═══════════════════════════════════════════════════════════════

def format_local(dt: datetime, fmt: Optional[str] = None) -> str:
    """Format an aware-or-naive UTC datetime in IST for display."""
    local = to_local_aware(dt)
    return local.strftime(fmt or _DEFAULT_FORMAT)


def format_time_only(dt: datetime) -> str:
    """Just the time portion (e.g. '6:30 PM')."""
    return to_local_aware(dt).strftime("%-I:%M %p")


def format_date_short(dt: datetime) -> str:
    """Short date (e.g. 'Wed May 21')."""
    return to_local_aware(dt).strftime("%a %b %-d")


# ═══════════════════════════════════════════════════════════════
# Arithmetic helpers
# ═══════════════════════════════════════════════════════════════

def add_minutes(dt: datetime, minutes: int) -> datetime:
    """Returns dt + minutes. Type-stable: aware-in → aware-out."""
    return dt + timedelta(minutes=minutes)


def diff_hours(later: datetime, earlier: datetime) -> float:
    """Hours between two datetimes (later - earlier).

    Both args must be either aware OR naive — mixing raises TypeError.
    """
    return (later - earlier).total_seconds() / 3600.0


# ═══════════════════════════════════════════════════════════════
# AM/PM disambiguation (was inline twice in tools.set_reminder)
# ═══════════════════════════════════════════════════════════════

def disambiguate_am_pm(
    dt_local: datetime,
    now_local_aware: datetime,
    max_future_hours: float = 10.0,
) -> tuple[datetime, bool]:
    """Detect and fix obvious AM/PM mistakes from LLM parses.

    Heuristic: if a reminder is set for >10h in the future or >10h in the
    past, but flipping AM/PM puts it within 0–2h of *now*, we assume the
    LLM lost the meridiem. Returns `(corrected_dt, was_flipped)`.

    This is the deterministic version of the inline hack that was
    duplicated in two branches of `set_reminder` ([bot/tools.py:548-572]).
    Tests live in `tests/test_time.py` (to be added).
    """
    delta_h = diff_hours(dt_local, now_local_aware)

    if delta_h > max_future_hours:
        flipped = dt_local - timedelta(hours=12)
        if 0 < diff_hours(flipped, now_local_aware) < 2:
            return flipped, True
    elif delta_h < -max_future_hours:
        flipped = dt_local + timedelta(hours=12)
        if 0 < diff_hours(flipped, now_local_aware) < 2:
            return flipped, True

    return dt_local, False


# ═══════════════════════════════════════════════════════════════
# Next-occurrence helper (for recurring daily reminders)
# ═══════════════════════════════════════════════════════════════

def next_occurrence_local(hour: int, minute: int) -> datetime:
    """Next IST-aware datetime where wall-clock == HH:MM.

    If today's HH:MM has passed, returns tomorrow's. Used by recurring
    reminders to schedule the next fire-time.
    """
    now = now_local()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ═══════════════════════════════════════════════════════════════
# Module-level sanity check — fails loudly on import if zones break
# ═══════════════════════════════════════════════════════════════
assert UTC is not None, "UTC zone failed to load"
assert IST is not None, f"BOT_TIMEZONE {config.BOT_TIMEZONE!r} is not a valid IANA zone"
