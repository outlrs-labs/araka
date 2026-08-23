"""FollowUp Bot — Tier-0 deterministic router (the LLM-bypass fast path).

Model-cascade tier 0: messages that match a strict, anchored pattern are
handled directly — no LLM call at all. That makes the most common turns
(relative reminders, listing reminders) ~instant AND removes the model's
chance to hallucinate on them entirely.

Design rules:
  - ANCHORED full-string regexes only. Anything fuzzy falls through to the
    agent (return None). A miss here costs nothing; a false positive would
    do the wrong action — so patterns are deliberately narrow.
  - Replies are written HERE in the bot's voice (lowercase, no IDs). Tool
    return strings are LLM-directed and never shown to the user raw.
  - Every handled turn is saved to ChatMemory so the agent keeps full
    context if the user follows up ("actually cancel that").
"""

import logging
import re
from datetime import timedelta

from sqlalchemy import select

from bot.database import async_session, ChatMemory, Reminder, User
from bot.utils.time import now_local, to_local_aware, utcnow_naive

logger = logging.getLogger(__name__)

_UNITS = r"(?:min|mins|minute|minutes|hr|hrs|hour|hours)"

# "remind me in 20 min to call mom"
_RE_A = re.compile(
    rf"^remind me in (\d{{1,3}}) ?({_UNITS}) to (.+)$", re.IGNORECASE)
# "remind me to call mom in 20 mins"
_RE_B = re.compile(
    rf"^remind me to (.+) in (\d{{1,3}}) ?({_UNITS})$", re.IGNORECASE)
# "in 20 min remind me to call mom"
_RE_C = re.compile(
    rf"^in (\d{{1,3}}) ?({_UNITS})[, ]+remind me to (.+)$", re.IGNORECASE)

# "reminders" / "my reminders" / "list my reminders" / "show reminders"
_RE_LIST = re.compile(
    r"^(?:list |show |what are )?(?:my )?reminders\??$", re.IGNORECASE)

# Compound-intent guard: "…call mom AND ALSO BOOK a meet with akshay" must
# not be swallowed as one reminder — multi-action turns go to the agent.
_RE_MULTI_INTENT = re.compile(
    r"\b(?:and|then|also|&)\s+(?:also\s+|then\s+)?"
    r"(?:book|schedule|set|email|remind|cancel|delete|meet)\b",
    re.IGNORECASE)


def _to_minutes(n: str, unit: str) -> int:
    mins = int(n)
    if unit.lower().startswith(("h",)):
        mins *= 60
    return mins


def _parse_relative_reminder(text: str):
    """Return (minutes, task) or None. Strict full-match only."""
    t = text.strip().rstrip(".!").strip()
    m = _RE_A.match(t)
    if m:
        return _to_minutes(m.group(1), m.group(2)), m.group(3).strip()
    m = _RE_B.match(t)
    if m:
        return _to_minutes(m.group(2), m.group(3)), m.group(1).strip()
    m = _RE_C.match(t)
    if m:
        return _to_minutes(m.group(1), m.group(2)), m.group(3).strip()
    return None


async def _remember(wa_id: str, user_text: str, reply: str) -> None:
    """Record the fast-path turn so the agent keeps conversational context."""
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            session.add(ChatMemory(user_id=u.id, role="user", text=user_text))
            session.add(ChatMemory(user_id=u.id, role="model", text=reply))
            await session.commit()


async def try_fast_path(wa_id: str, text: str):
    """Handle a message deterministically. Returns reply text, or None to
    fall through to the LLM agent. Never raises — any error falls through."""
    try:
        return await _try_fast_path(wa_id, text)
    except Exception as e:
        logger.warning(f"Fast path error (falling through to agent): {e}")
        return None


async def _try_fast_path(wa_id: str, text: str):
    # ── 1. Relative reminder create ─────────────────────────
    parsed = _parse_relative_reminder(text)
    if parsed:
        minutes, task = parsed
        # Sanity bounds; anything odd goes to the agent instead.
        if not (1 <= minutes <= 1440) or not (1 <= len(task) <= 200):
            return None
        if _RE_MULTI_INTENT.search(task):
            return None  # compound request — the agent must split it
        from bot.tools import set_reminder  # local import: avoids cycle
        result = await set_reminder(wa_id, message=task,
                                    relative_minutes=minutes)
        if not str(result).startswith("Reminder #"):
            return None  # tool refused — let the agent handle/explain it
        at = (now_local() + timedelta(minutes=minutes)).strftime("%I:%M %p")
        reply = f"done — i'll remind you at {at.lstrip('0').lower()}: {task}"
        await _remember(wa_id, text, reply)
        logger.info(f"Fast path: reminder in {minutes}m for {wa_id} (no LLM)")
        return reply

    # ── 2. List reminders ───────────────────────────────────
    if _RE_LIST.match(text.strip()):
        async with async_session() as session:
            rows = (await session.execute(
                select(Reminder).where(
                    Reminder.wa_id == wa_id, Reminder.is_sent == False,
                ).order_by(Reminder.remind_at).limit(20)
            )).scalars().all()
        if not rows:
            reply = "no active reminders."
        else:
            lines = ["your reminders:"]
            for r in rows:
                dt = to_local_aware(r.remind_at)
                when = (f"daily at {dt.strftime('%I:%M %p').lstrip('0').lower()}"
                        if r.is_recurring else
                        dt.strftime("%b %d, %I:%M %p").replace(" 0", " ").lower())
                lines.append(f"- {when} — {r.message}")
            reply = "\n".join(lines)
        await _remember(wa_id, text, reply)
        logger.info(f"Fast path: list reminders for {wa_id} (no LLM)")
        return reply

    return None
