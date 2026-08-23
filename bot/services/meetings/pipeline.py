"""Post-meeting pipeline: find → fetch → summarise → deliver.

The one place that knows the ORDER of operations. Each step it calls is
provider-agnostic or provider-owned, so this file never learns whether a
transcript came from Meet or Zoom.

    due meetings          tasks that ended recently, no summary row yet
      -> resolve          meeting link -> provider conference id
      -> fetch            provider -> TranscriptContext (speakers + segments)
      -> summarise        TranscriptContext -> MeetingSummary (via the LLM)
      -> store            one row per meeting, so nothing is paid for twice
      -> deliver          condensed summary to WhatsApp

Idempotency is the design constraint. A row in `meeting_summaries` means
"already handled" — the poller can run every 10 minutes forever without
re-fetching, re-summarising or re-sending. `delivered` is tracked separately
so a failed WhatsApp send retries the SEND only, never the LLM call.

INERT until `config.MEET_TRANSCRIPTS_ENABLED` is true. See this package's
__init__ for the two prerequisites that must be real first.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import timedelta

from sqlalchemy import select

from bot.config import config
from bot.database import async_session, MeetingSummary, Task, User
from bot.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

# A meeting is only worth checking once it has actually finished.
_SETTLE_MINUTES = 5


async def _due_meetings(limit: int = 10) -> list:
    """Meetings that ended recently and have no summary row yet."""
    now = utcnow_naive()
    oldest = now - timedelta(hours=config.MEET_TRANSCRIPT_LOOKBACK_HOURS)
    async with async_session() as session:
        handled = set((await session.execute(
            select(MeetingSummary.task_id)
        )).scalars().all())
        rows = (await session.execute(
            select(Task).where(
                Task.status == "scheduled",
                Task.scheduled_at.isnot(None),
                Task.scheduled_at >= oldest,
                Task.meeting_link.isnot(None),
            ).order_by(Task.scheduled_at.desc())
        )).scalars().all()

    due = []
    for task in rows:
        if task.id in handled or not (task.meeting_link or "").strip():
            continue
        ends = task.scheduled_at + timedelta(
            minutes=(task.duration_minutes or 30) + _SETTLE_MINUTES
        )
        if now >= ends:
            due.append(task)
        if len(due) >= limit:
            break
    return due


async def _record(task, *, provider: str = "google_meet", conference_id: str = "",
                  summary=None, unavailable_reason: str = "") -> None:
    async with async_session() as session:
        session.add(MeetingSummary(
            wa_id=task.wa_id,
            task_id=task.id,
            provider=provider,
            conference_id=conference_id,
            summary_json=(
                json.dumps(dataclasses.asdict(summary)) if summary else None
            ),
            delivered=False,
            unavailable_reason=unavailable_reason or None,
        ))
        await session.commit()


async def _mark_delivered(task_id: int) -> None:
    async with async_session() as session:
        row = (await session.execute(
            select(MeetingSummary).where(MeetingSummary.task_id == task_id)
        )).scalars().first()
        if row:
            row.delivered = True
            await session.commit()


def provider_for(meeting_link: str) -> str:
    """Which provider hosts this meeting, judged by its link.

    Chosen per meeting rather than by a global setting, so a Meet link and a
    Zoom link can coexist for the same user on the same day.
    """
    link = (meeting_link or "").lower()
    if "zoom.us" in link:
        return "zoom"
    if "meet.google.com" in link:
        return "google_meet"
    return ""


async def process_one(task) -> bool:
    """Handle a single finished meeting. True if a summary was delivered."""
    from bot.services.meetings.meet_source import (
        MeetTranscriptSource, resolve_conference,
    )
    from bot.services.meetings.zoom_source import (
        ZoomTranscriptSource, meeting_id_from_link,
    )
    from bot.services.meetings.summariser import summarise
    from bot.services.meetings.transcript_source import (
        MeetingRef, TranscriptUnavailable,
    )
    from bot.services.whatsapp import send_message_async

    provider = provider_for(task.meeting_link)
    if not provider:
        # Neither Meet nor Zoom — a dial-in or a bare location. Record it so
        # the poller stops looking at this meeting every cycle.
        await _record(task, unavailable_reason="unrecognised meeting link")
        return False

    async with async_session() as session:
        db_user = (await session.execute(
            select(User).where(User.wa_id == task.wa_id)
        )).scalar_one_or_none()
    if not db_user:
        return False
    # Zoom auth is account-level, so it works without a linked Google account.
    if provider == "google_meet" and not db_user.google_token_json:
        return False

    try:
        if provider == "zoom":
            conference_id = meeting_id_from_link(task.meeting_link)
            source = ZoomTranscriptSource()
        else:
            conference_id = await resolve_conference(
                db_user, task.meeting_link, started_after=task.scheduled_at,
            )
            source = MeetTranscriptSource()
    except TranscriptUnavailable as e:
        # Permanent — record it so this meeting is never retried.
        await _record(task, provider=provider, unavailable_reason=str(e)[:250])
        return False
    except Exception as e:
        logger.warning("Conference lookup failed for task %s: %s", task.id, e)
        return False        # transient: no row written, retried next cycle

    if not conference_id:
        return False        # conference not published yet — try again later

    ref = MeetingRef(
        provider=provider,
        external_event_id=task.external_event_id or "",
        conference_id=conference_id,
        title=task.title or "",
        start=task.scheduled_at,
    )
    try:
        ctx = await source.fetch(db_user, ref)
    except TranscriptUnavailable as e:
        await _record(task, provider=provider, conference_id=conference_id,
                      unavailable_reason=str(e)[:250])
        return False
    except Exception as e:
        logger.warning("Transcript fetch failed for task %s: %s", task.id, e)
        return False

    if ctx is None:
        return False        # not ready yet

    summary = await summarise(ctx)
    if summary is None:
        logger.warning("Summarisation produced nothing for task %s", task.id)
        return False

    # Store BEFORE sending: a send failure must never cost another LLM call.
    await _record(task, provider=provider, conference_id=conference_id,
                  summary=summary)

    # Deliver through the deterministic card (full text + action buttons).
    # Same renderer as the pull path, so push and pull are identical. If the
    # buttons can't go out (or would clobber an active flow), fall back to
    # the plain-text rendering so the summary is never lost.
    payload = dataclasses.asdict(summary)
    payload.update({
        "task_id": task.id,
        "provider": provider,
        "provider_label": "Zoom" if provider == "zoom" else "Google Meet",
        "title": task.title or "",
        "when": task.scheduled_at.isoformat() if task.scheduled_at else "",
        "when_label": _when_label(summary.headline or task.title,
                                  task.scheduled_at),
    })
    try:
        from bot.handlers.callback_handler import send_meeting_summary_card
        ok = await send_meeting_summary_card(wa_id=task.wa_id, data=payload)
    except Exception as e:
        logger.warning("Card delivery failed for task %s: %s", task.id, e)
        text = summary.to_whatsapp()
        ok = bool(text.strip()) and await send_message_async(task.wa_id, text)
    if ok:
        await _mark_delivered(task.id)
    return bool(ok)


def _when_label(name: str, start_utc_naive) -> str:
    """'Pricing call — Mon Aug 17, 3:00 PM' for card meta lines."""
    from bot.utils.time import to_local_aware
    label = (name or "Untitled meeting").strip()[:60]
    if start_utc_naive:
        return f"{label} — {to_local_aware(start_utc_naive).strftime('%a %b %d, %I:%M %p')}"
    return f"{label} — time unknown"


async def run_once() -> int:
    """One poll cycle. Returns how many summaries were delivered."""
    if not config.MEET_TRANSCRIPTS_ENABLED:
        return 0
    delivered = 0
    for task in await _due_meetings():
        try:
            if await process_one(task):
                delivered += 1
        except Exception as e:      # one bad meeting must not stop the rest
            logger.error("Transcript pipeline error on task %s: %s", task.id, e)
    if delivered:
        logger.info("Delivered %d meeting summary(ies)", delivered)
    return delivered
