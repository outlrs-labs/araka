#!/usr/bin/env python3
"""Standalone reminder reconcile — the external heartbeat.

Run as a one-shot every ~60s by a systemd timer, INDEPENDENT of the web
process. If gunicorn / APScheduler is wedged, restarting, or its thread has
died, this still fires due reminders, because the database is the source of
truth and the fire path claims each reminder atomically.

Safe to overlap with the in-app scheduler:
  - one-shot reminders  -> claimed via `UPDATE ... WHERE is_sent=False`
  - recurring reminders -> claimed via compare-and-swap on `remind_at`
so each reminder fires exactly once no matter how many workers run.

It also ALERTS (loud log + optional WhatsApp ping to ADMIN_WA_ID) when a
reminder is genuinely stuck — overdue and still undelivered after retries —
which usually means the reminder template isn't set or the number is blocked.

Usage:  venv/bin/python reconcile_reminders.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Make the project importable when launched by systemd (any CWD).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    format="%(asctime)s - reconcile - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("reconcile")

from datetime import timedelta                       # noqa: E402

from sqlalchemy import select                        # noqa: E402

from bot.config import config                        # noqa: E402
from bot.database import async_session, Reminder     # noqa: E402
from bot.services.reminder_scheduler import _fire_reminder_async  # noqa: E402
from bot.utils.time import utcnow_naive              # noqa: E402

# A reminder overdue by more than this and still unsent = real delivery failure.
STUCK_AFTER_MIN = 10


async def _reconcile() -> None:
    now = utcnow_naive()

    # 1. Fire everything that's due. The atomic claim inside _fire_reminder_async
    #    de-dupes against the in-app scheduler, so overlap is harmless.
    async with async_session() as session:
        due = list((await session.execute(
            select(Reminder).where(
                Reminder.is_sent == False,          # noqa: E712
                Reminder.remind_at <= now,
            )
        )).scalars().all())
        due_ids = [r.id for r in due]

    if due_ids:
        logger.info(f"firing {len(due_ids)} due reminder(s): {due_ids}")
    for rid in due_ids:
        try:
            await _fire_reminder_async(rid)
        except Exception as e:
            logger.error(f"reminder #{rid} fire error: {e}", exc_info=True)

    # 2. Alert on genuinely stuck reminders (overdue + still unsent after tries).
    stuck_cutoff = now - timedelta(minutes=STUCK_AFTER_MIN)
    async with async_session() as session:
        stuck = list((await session.execute(
            select(Reminder).where(
                Reminder.is_sent == False,          # noqa: E712
                Reminder.remind_at <= stuck_cutoff,
            )
        )).scalars().all())

    if stuck:
        ids = [r.id for r in stuck]
        msg = (f"{len(stuck)} reminder(s) stuck undelivered "
               f"(overdue >{STUCK_AFTER_MIN}m): {ids}")
        logger.error(f"ALERT: {msg}")
        _maybe_ping_admin(msg)


def _maybe_ping_admin(detail: str) -> None:
    """Best-effort operator alert. Only fires if ADMIN_WA_ID is set."""
    admin = getattr(config, "ADMIN_WA_ID", "") or ""
    if not admin:
        return
    try:
        from bot.services.whatsapp import send_message
        send_message(
            admin,
            "araka alert\n\n"
            f"{detail}\n\n"
            "likely the reminder template isn't approved/set, or the number "
            "is blocked. check journalctl -u followup-bot.",
        )
    except Exception as e:
        logger.warning(f"admin ping failed: {e}")


if __name__ == "__main__":
    asyncio.run(_reconcile())
