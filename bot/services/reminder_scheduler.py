"""Precise reminder scheduler.

Fixes the ~60s polling-lag bug. Instead of waking up every minute and
asking "any reminders due?", we register a one-shot APScheduler
DateTrigger job for each reminder at its exact fire time. Precision
goes from ±60s to ±1-2s.

Source of truth
---------------
The `reminders` table in SQLite remains authoritative. APScheduler's
in-memory jobstore is a *cache*. On process boot, `reload_pending()`
walks the table and re-registers every unfired reminder as a DateTrigger
job, so we survive restarts cleanly.

A low-frequency 5-minute reconciliation poll still runs in main.py as
defense in depth — it catches reminders that were inserted directly
into SQLite (e.g. by a future admin tool) or that slipped through
because of a scheduler restart race.

Concurrency safety
------------------
`_fire_reminder_async` performs an *atomic claim* before sending. For
one-shots that means flipping `is_sent=False → True` and only proceeding
if the row update affected exactly one row. If `send_message` then
fails, we *roll the claim back* so the 5-min reconciliation re-tries it.

For recurring daily reminders we advance `remind_at` via
`next_occurrence_local(h, m)` rather than `remind_at + 1 day` — that
anchors the next fire to wall-clock HH:MM instead of letting jitter
accumulate (the "9:00 AM → 9:01:34 → 9:03:08" drift bug).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select, update

from bot.database import async_session, Reminder
from bot.utils.time import UTC, to_utc_naive, next_occurrence_local, utcnow_aware

logger = logging.getLogger(__name__)

# ── Module state (set once at boot from main.py) ────────────────
_scheduler = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def init(scheduler, loop: asyncio.AbstractEventLoop) -> None:
    """Wire the APScheduler instance and the asyncio loop used for DB I/O."""
    global _scheduler, _loop
    _scheduler = scheduler
    _loop = loop
    logger.info("Reminder scheduler initialised")


def _run_async(coro):
    """Run an async coroutine on the registered background loop."""
    if _loop is None:
        raise RuntimeError("reminder_scheduler.init() was not called")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


def _job_id(reminder_id: int) -> str:
    return f"reminder_{reminder_id}"


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def schedule_reminder(reminder_id: int, remind_at: datetime) -> bool:
    """Register a one-shot DateTrigger for this reminder.

    `remind_at` may be naive (assumed UTC) or aware. APScheduler stores
    timezone-aware datetimes; we always pass UTC-aware.

    Returns True if the job was scheduled, False if no scheduler is wired
    (e.g. during tests).
    """
    if _scheduler is None:
        logger.warning(
            f"schedule_reminder(#{reminder_id}) called before init() — "
            f"reload_pending will pick it up on next boot"
        )
        return False

    if remind_at.tzinfo is None:
        run_date = UTC.localize(remind_at)
    else:
        run_date = remind_at.astimezone(UTC)

    # If the time is already past, fire ASAP (misfire_grace_time covers
    # boot reload of slightly-overdue reminders).
    _scheduler.add_job(
        _fire_reminder_sync,
        DateTrigger(run_date=run_date),
        args=[reminder_id],
        id=_job_id(reminder_id),
        replace_existing=True,
        misfire_grace_time=600,  # tolerate 10 min of skew (process restart)
        coalesce=True,           # collapse duplicate triggers
    )
    logger.info(
        f"Reminder #{reminder_id} scheduled for {run_date.isoformat()}"
    )
    return True


def cancel_reminder(reminder_id: int) -> bool:
    """Remove a scheduled reminder job. Safe to call if no job exists."""
    if _scheduler is None:
        return False
    try:
        _scheduler.remove_job(_job_id(reminder_id))
        logger.info(f"Reminder #{reminder_id} cancelled")
        return True
    except Exception as e:
        # Job didn't exist or already fired — both are fine
        logger.debug(f"cancel_reminder(#{reminder_id}): {e}")
        return False


async def reload_pending() -> int:
    """Re-register DateTrigger jobs for every unfired reminder.

    Called once at process boot. Returns the count reloaded.
    """
    if _scheduler is None:
        logger.warning("reload_pending called before init()")
        return 0

    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.is_sent == False)
        )
        pending = list(result.scalars().all())

    for r in pending:
        schedule_reminder(r.id, r.remind_at)

    logger.info(f"Reloaded {len(pending)} pending reminder(s) into scheduler")
    return len(pending)


# ═══════════════════════════════════════════════════════════════
# Fire path (called by APScheduler when DateTrigger expires)
# ═══════════════════════════════════════════════════════════════

def _fire_reminder_sync(reminder_id: int) -> None:
    """APScheduler invokes this in its worker thread; bridge to asyncio."""
    try:
        _run_async(_fire_reminder_async(reminder_id))
    except Exception as e:
        logger.error(f"Reminder #{reminder_id} fire failed: {e}", exc_info=True)


async def _fire_reminder_async(reminder_id: int) -> None:
    """Atomically claim → send → finalise (or rollback on failure)."""
    # Lazy import to dodge circular: whatsapp imports config which is fine,
    # but keeping it here makes this module testable without HTTP libs.
    from bot.services.whatsapp import send_message

    # ── Step 1: Atomic claim ──────────────────────────────────
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )
        r = result.scalar_one_or_none()
        if r is None:
            logger.warning(f"Reminder #{reminder_id} vanished before firing")
            return

        # Snapshot fields BEFORE claim — session is short-lived
        wa_id = r.wa_id
        message = r.message
        is_recurring = bool(r.is_recurring)
        recur_time = r.recur_time

        if is_recurring and recur_time:
            # Advance to next wall-clock occurrence (anchor, no drift)
            h, m = [int(x) for x in recur_time.split(":")]
            next_local = next_occurrence_local(h, m)
            claim_stmt = (
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(remind_at=to_utc_naive(next_local))
            )
            await session.execute(claim_stmt)
            await session.commit()
            new_remind_at = to_utc_naive(next_local)
        else:
            # One-shot: claim by flipping is_sent. UPDATE … WHERE
            # is_sent=False ensures only one fire wins if scheduler
            # double-fires for any reason.
            claim_stmt = (
                update(Reminder)
                .where(Reminder.id == reminder_id, Reminder.is_sent == False)
                .values(is_sent=True)
            )
            res = await session.execute(claim_stmt)
            await session.commit()
            if res.rowcount == 0:
                logger.info(
                    f"Reminder #{reminder_id} already claimed; skipping"
                )
                return
            new_remind_at = None

    # ── Step 2: Send (outside the DB session) ─────────────────
    ok = send_message(wa_id, f"reminder:\n\n{message}")

    # ── Step 3: Finalise ──────────────────────────────────────
    if not ok:
        if is_recurring:
            # We already advanced remind_at — next occurrence handles retry
            logger.warning(
                f"Recurring reminder #{reminder_id} send failed; will fire next cycle"
            )
        else:
            # Roll the claim back so the 5-min reconciliation picks it up
            async with async_session() as session:
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == reminder_id)
                    .values(is_sent=False)
                )
                await session.commit()
            logger.error(
                f"Reminder #{reminder_id} send failed; claim rolled back"
            )
        return

    # Recurring: schedule the next occurrence
    if is_recurring and new_remind_at is not None:
        schedule_reminder(reminder_id, new_remind_at)

    logger.info(f"Reminder #{reminder_id} fired successfully")
