"""FollowUp Bot — Tool functions for the AI Agent.

Each tool wraps a DB or Google API operation.
The agent calls these via function calling.
User identifier is wa_id (WhatsApp phone number string).
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from bot.database import async_session, User, Note, Reminder
from bot.services.calendar import create_event, get_all_events, delete_event, update_event
from bot.services.contacts import search_contacts
from bot.config import config
from bot.utils.time import (
    IST, UTC, utcnow_naive,
    to_utc_naive, to_local_aware, ensure_aware_local,
    parse_iso_to_aware, now_local,
    disambiguate_am_pm, next_occurrence_local,
)
from bot.utils.intent import has_explicit_clock_time, has_explicit_date

logger = logging.getLogger(__name__)

NOT_CONNECTED = (
    "GOOGLE_NOT_CONNECTED: The user has not linked their Google account yet. "
    "They need to connect their Google account to use this feature."
)


# ─── Helpers ──────────────────────────────────────────────────

async def _get_user(wa_id: str):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        return result.scalar_one_or_none()


# ═══════════════════════════════════════════════════════════════
# Notes
# ═══════════════════════════════════════════════════════════════

async def save_note(wa_id: str, note_text: str, detected_date_iso: str = None) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    dt = datetime.fromisoformat(detected_date_iso) if detected_date_iso else None
    async with async_session() as session:
        note = Note(user_id=db_user.id, text=note_text, detected_date=dt)
        session.add(note)
        await session.flush()
        nid = note.id
        await session.commit()
    return f"Saved note #{nid}."


async def get_notes(wa_id: str, limit: int = 5) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.user_id == db_user.id)
            .order_by(Note.created_at.desc()).limit(limit)
        )
        notes = list(result.scalars().all())
    if not notes:
        return "No notes saved yet."
    lines = []
    for n in notes:
        t = to_local_aware(n.created_at)
        lines.append(f"[#{n.id}] {n.text}  ({t.strftime('%b %d, %H:%M')})")
    return "\n".join(lines)


async def delete_note(wa_id: str, note_id: int) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.id == note_id, Note.user_id == db_user.id)
        )
        note = result.scalar_one_or_none()
        if not note:
            return f"Note #{note_id} not found."
        await session.delete(note)
        await session.commit()
    return f"Deleted note #{note_id}."


# ═══════════════════════════════════════════════════════════════
# Calendar
# ═══════════════════════════════════════════════════════════════

async def calendar_create(wa_id: str, title: str, start_time_iso: str,
                          duration_minutes: int = 30, with_meet: bool = False,
                          attendees_csv: str = "") -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED

    dt = ensure_aware_local(datetime.fromisoformat(start_time_iso))

    try:
        from bot.services.calendar import find_conflicts
        conflicts = await find_conflicts(db_user, dt, duration_minutes)
        if conflicts:
            conflict_lines = []
            for c in conflicts:
                try:
                    cs = datetime.fromisoformat(c["start"])
                    ce = datetime.fromisoformat(c["end"])
                    conflict_lines.append(
                        f'"{c["summary"]}" on {cs.strftime("%a %b %d, %I:%M")}–{ce.strftime("%I:%M %p")}'
                    )
                except Exception:
                    conflict_lines.append(f'"{c["summary"]}"')
            return (
                f"⚠️ CONFLICT DETECTED: The time slot {dt.strftime('%b %d, %I:%M %p')} "
                f"is NOT available. You already have: {'; '.join(conflict_lines)}. "
                f"Please ask the user if they want to pick a different time or proceed anyway."
            )
    except Exception as e:
        logger.warning(f"Conflict check failed (proceeding): {e}")

    attendees = [e.strip() for e in attendees_csv.split(",") if e.strip()] if attendees_csv else None
    try:
        info = await create_event(db_user, title, dt, duration_minutes=duration_minutes,
                                   meet_link=with_meet, attendees=attendees)
        end_dt = dt + timedelta(minutes=duration_minutes)
        start_str = dt.strftime("%I:%M %p")
        end_str = end_dt.strftime("%I:%M %p")
        date_str = dt.strftime("%a %b %d")
        msg = f"EVENT_CREATED: '{title}' on {date_str} from {start_str} to {end_str} IST."
        if info.get("meet"):
            msg += f" Google Meet link: {info['meet']}"
        if attendees_csv:
            msg += f" Attendees: {attendees_csv}"
        return msg
    except Exception as e:
        return f"Failed to create event: {e}"


async def calendar_get_all(wa_id: str, limit: int = 10) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    try:
        events = await get_all_events(db_user, max_results=limit)
        if not events:
            return "No upcoming events."
        lines = []
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date", ""))
            end = ev["end"].get("dateTime", ev["end"].get("date", ""))
            try:
                s = datetime.fromisoformat(start)
                e = datetime.fromisoformat(end)
                time_str = f"{s.strftime('%a %b %d, %I:%M %p')}–{e.strftime('%I:%M %p')}"
            except Exception:
                time_str = start
            lines.append(f"- {ev.get('summary', 'Untitled')} | {time_str} | ID: {ev['id']}")
        return "Events:\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch events: {e}"


async def calendar_delete(wa_id: str, event_id: str) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    try:
        await delete_event(db_user, event_id)
        return f"Deleted event {event_id}."
    except Exception as e:
        return f"Failed to delete event: {e}"


async def calendar_update(wa_id: str, event_id: str,
                           new_title: str = "", new_start_iso: str = "",
                           new_duration_minutes: int = 0) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    try:
        link = await update_event(
            db_user, event_id,
            title=new_title or None,
            start_iso=new_start_iso or None,
            duration_minutes=new_duration_minutes or None,
        )
        return f"Updated event! Link: {link}"
    except Exception as e:
        return f"Failed to update event: {e}"


# ═══════════════════════════════════════════════════════════════
# Contacts
# ═══════════════════════════════════════════════════════════════

async def contacts_search(wa_id: str, query: str = "", limit: int = 5) -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    try:
        contacts = await search_contacts(db_user, query=query, limit=limit)
        if not contacts:
            return f"No contacts found for '{query}'." if query else "No contacts."
        lines = [f"Found {len(contacts)} contacts:"]
        for c in contacts:
            emails = ", ".join(c["emails"]) if c["emails"] else "—"
            phones = ", ".join(c["phones"]) if c["phones"] else "—"
            lines.append(f"• {c['name']}  📧 {emails}  📱 {phones}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to search contacts: {e}"


# ═══════════════════════════════════════════════════════════════
# Google Meet — Compound Workflow Tool
# ═══════════════════════════════════════════════════════════════

async def set_gmeet(wa_id: str, attendee_name: str = "", attendee_email: str = "",
                    title: str = "", start_time_iso: str = "",
                    attendee_phone: str = "",
                    duration_minutes: int = 30,   # PRD §FR-3 default = 30
                    skip_conflict_check: bool = False,
                    user_text: str = "",          # raw user message — anti-hallucination input
                    _confirmed: bool = False) -> str:
    """Thin orchestrator over `bot.services.gmeet_flow`.

    Pipeline (PRD §A.2):

        Step 1 — resolve_attendee()              name → email (skipped if email already given)
        Step 2 — propose_meeting()               conflict check + confirmation gate
        Step 3 — create_event_and_notify()       fires only when _confirmed=True

    The LLM is never permitted to set `_confirmed`; the agent layer strips
    that arg before invocation. Only the deterministic callback_handler
    can set it (on a real button tap).

    Email shortcut: ``attendee_name="harsh{yharsh499@gmail.com}"`` bypasses
    contact resolution entirely.
    """
    from bot.services.gmeet_flow import (
        parse_name_email_shortcut, resolve_attendee,
        propose_meeting, create_event_and_notify,
    )

    db_user = await _get_user(wa_id)
    if not db_user:
        return json.dumps({"action": "error", "message": "User not found."})
    if not db_user.google_token_json:
        return NOT_CONNECTED

    # Parse name{email} shortcut up-front
    name, email = parse_name_email_shortcut(attendee_name, attendee_email)
    phone = (attendee_phone or "").strip()

    # A confirmed "Yes, save" tap is the FINAL gate. The conflict prompt (if
    # any) was already shown and resolved ("keep both") BEFORE the confirm
    # card appeared — so re-running the conflict check here would bounce the
    # user back to the conflict screen forever (the confirm→conflict→keep
    # both→confirm loop). Confirm ⇒ skip the conflict check and just create.
    if _confirmed:
        skip_conflict_check = True

    # ── Anti-hallucination guard (PRD §FR-3 + Procedural Integrity) ──
    # The LLM is only allowed to pass `start_time_iso` if the user's
    # message actually contained a clock-time token. Otherwise we wipe it
    # and let the missing-time branch ask the user.
    if not _confirmed and start_time_iso and user_text:
        if not has_explicit_clock_time(user_text):
            logger.warning(
                f"Rejected LLM-invented start_time_iso for wa_id={wa_id}: "
                f"user_text={user_text!r} → start_time_iso={start_time_iso!r}"
            )
            start_time_iso = ""

    # ── Step 1 — resolve attendee (only if no email yet) ────────
    if not email and name:
        r1 = await resolve_attendee(db_user, name)
        action = r1.get("action")
        if action == "resolved":
            name  = r1["name"]
            email = r1["email"]
            phone = r1["phone"] or phone
        elif action == "pick_contact":
            return json.dumps({
                "action": "pick_contact",
                "contacts": r1["contacts"],
                "attendee_name": name,
                "title": title,
                "start_time_iso": start_time_iso,
                "duration_minutes": duration_minutes,
                "message": (
                    f"I found multiple contacts matching '{name}':\n" +
                    "\n".join(c["display"] for c in r1["contacts"]) +
                    "\nWhich one should I book the meeting with? "
                    "If none are correct, send the Gmail address."
                ),
            })
        elif action == "need_email":
            return json.dumps({
                "action": "need_email",
                "attendee_name": r1["name"],
                "attendee_phone": r1["phone"],
                "title": title,
                "start_time_iso": start_time_iso,
                "duration_minutes": duration_minutes,
                "message": (
                    f"I found {r1['name']} but they don't have an email on file. "
                    f"Please share their Gmail to send the invite. "
                    f"You can send just the Gmail, or use "
                    f"{r1['name']}{{email@example.com}}."
                ),
            })
        elif action == "no_contact":
            return json.dumps({
                "action": "no_contact",
                "attendee_name": name,
                "attendee_phone": "",
                "title": title,
                "start_time_iso": start_time_iso,
                "duration_minutes": duration_minutes,
                "message": (
                    f"I couldn't find '{name}' in your contacts. "
                    f"Please share their Gmail so I can send the calendar invite. "
                    f"You can send just the Gmail, or use "
                    f"{name}{{email@example.com}}."
                ),
            })
        else:  # error
            return json.dumps(r1)

    # ── Step 2 — propose (conflict check + confirmation gate) ───
    proposal = await propose_meeting(
        db_user, name, email, phone, start_time_iso,
        duration_minutes=duration_minutes,
        llm_title=title,
        skip_conflict_check=skip_conflict_check,
    )
    action = proposal.get("action")

    # Adapt internal {name,email,phone} keys to the long-form ones the
    # callback_handler dispatcher expects on the wire.
    def _to_wire(p: dict) -> dict:
        out = dict(p)
        if "name"  in out: out["attendee_name"]  = out.pop("name")
        if "email" in out: out["attendee_email"] = out.pop("email")
        if "phone" in out: out["attendee_phone"] = out.pop("phone")
        return out

    if action == "missing_time" or action == "conflict":
        return json.dumps(_to_wire(proposal))

    if action != "awaiting_confirmation":
        return json.dumps(_to_wire(proposal))

    # awaiting_confirmation
    if not _confirmed:
        return json.dumps(_to_wire(proposal))

    # ── Step 3 — actually create (post-Yes-tap path) ────────────
    created = await create_event_and_notify(
        db_user, wa_id,
        name, email, phone,
        start_time_iso,
        duration_minutes=duration_minutes,
        llm_title=title,
    )
    return json.dumps(_to_wire(created))


# ═══════════════════════════════════════════════════════════════
# Reminders
# ═══════════════════════════════════════════════════════════════

async def set_reminder(wa_id: str, message: str, remind_at_iso: str = "",
                       reminder_type: str = "text",
                       is_recurring: bool = False,
                       recur_time_hhmm: str = "",
                       relative_minutes: int = 0) -> str:
    """Set a one-time or recurring daily reminder.

    For relative times: pass relative_minutes (server computes exact time).
    For absolute times: pass remind_at_iso.
    For recurring: set is_recurring=True and provide recur_time_hhmm (HH:MM, 24h).

    All datetime math goes through bot.utils.time — no inline tz logic.
    """
    if reminder_type not in ("text", "notes", "calendar"):
        reminder_type = "text"
    labels = {"text": "📝", "notes": "📒", "calendar": "📅"}

    try:
        # Local import dodges a circular dep (services → tools → services)
        from bot.services.reminder_scheduler import schedule_reminder

        # ── Recurring daily message flow ──
        if is_recurring and recur_time_hhmm:
            h, m = [int(x) for x in recur_time_hhmm.strip().split(":")]
            target = next_occurrence_local(h, m)
            dt_utc = to_utc_naive(target)
            async with async_session() as session:
                rem = Reminder(wa_id=wa_id, message=message, remind_at=dt_utc,
                               reminder_type=reminder_type,
                               is_recurring=True, recur_time=f"{h:02d}:{m:02d}")
                session.add(rem)
                await session.flush()
                rid = rem.id
                await session.commit()
            # Register the precise DateTrigger job
            schedule_reminder(rid, dt_utc)
            return f'Daily reminder #{rid} set for {h:02d}:{m:02d} every day: "{message}"'

        # ── Server-side relative time (PREFERRED — no LLM math) ──
        if relative_minutes and relative_minutes > 0:
            dt_local = now_local() + timedelta(minutes=relative_minutes)
            logger.info(
                f"Reminder via relative_minutes={relative_minutes} → "
                f"{dt_local.strftime('%I:%M %p IST')}"
            )
            dt_utc = to_utc_naive(dt_local)
            async with async_session() as session:
                rem = Reminder(wa_id=wa_id, message=message, remind_at=dt_utc,
                               reminder_type=reminder_type, is_recurring=False)
                session.add(rem)
                await session.flush()
                rid = rem.id
                await session.commit()
            # Register the precise DateTrigger job — fires within ±1-2s
            schedule_reminder(rid, dt_utc)
            return (
                f'Reminder #{rid} set for {dt_local.strftime("%b %d, %I:%M %p")} IST '
                f'(in {relative_minutes} min): "{message}" {labels.get(reminder_type, "")}'
            )

        # ── Absolute ISO time (fallback) ──
        if not remind_at_iso:
            return "Please specify when to remind you (e.g. 'in 5 minutes' or 'at 3 PM')."

        dt_local = ensure_aware_local(datetime.fromisoformat(remind_at_iso))
        # AM/PM sanity check — single canonical implementation
        dt_local, flipped = disambiguate_am_pm(dt_local, now_local())
        if flipped:
            logger.warning(
                f"AM/PM auto-correct applied for wa_id={wa_id}: "
                f"final={dt_local.strftime('%I:%M %p')}"
            )

        dt_utc = to_utc_naive(dt_local)
        async with async_session() as session:
            rem = Reminder(wa_id=wa_id, message=message, remind_at=dt_utc,
                           reminder_type=reminder_type, is_recurring=False)
            session.add(rem)
            await session.flush()
            rid = rem.id
            await session.commit()
        schedule_reminder(rid, dt_utc)
        return (
            f'Reminder #{rid} set for {dt_local.strftime("%b %d, %I:%M %p")} IST: '
            f'"{message}" {labels.get(reminder_type, "")}'
        )
    except Exception as e:
        return f"Failed to set reminder: {e}"


async def set_daily_message(wa_id: str, message: str, time_hhmm: str) -> str:
    from bot.services.reminder_scheduler import schedule_reminder
    try:
        h, m = [int(x) for x in time_hhmm.strip().split(":")]
        target = next_occurrence_local(h, m)
        dt_utc = to_utc_naive(target)
        async with async_session() as session:
            rem = Reminder(wa_id=wa_id, message=message, remind_at=dt_utc,
                           is_recurring=True, recur_time=f"{h:02d}:{m:02d}")
            session.add(rem)
            await session.flush()
            rid = rem.id
            await session.commit()
        schedule_reminder(rid, dt_utc)
        return f'Daily message #{rid} set for {h:02d}:{m:02d} every day: "{message}"'
    except Exception as e:
        return f"Failed to set daily message: {e}"


async def list_reminders(wa_id: str) -> str:
    now_utc = utcnow_naive()
    recent_cutoff = now_utc - timedelta(minutes=30)

    async with async_session() as session:
        # Active (unfired) reminders
        active_result = await session.execute(
            select(Reminder).where(
                Reminder.wa_id == wa_id, Reminder.is_sent == False
            ).order_by(Reminder.remind_at)
        )
        active = list(active_result.scalars().all())

        # Recently fired (last 30 min) — so LLM knows they worked
        fired_result = await session.execute(
            select(Reminder).where(
                Reminder.wa_id == wa_id,
                Reminder.is_sent == True,
                Reminder.is_recurring == False,
                Reminder.remind_at >= recent_cutoff,
            ).order_by(Reminder.remind_at.desc()).limit(5)
        )
        fired = list(fired_result.scalars().all())

    if not active and not fired:
        return "No active reminders or daily messages."

    lines = []
    if active:
        lines.append("Active reminders:")
        for r in active:
            dt_local = to_local_aware(r.remind_at)
            rtype = "🔁 Daily" if r.is_recurring else "🔔"
            lines.append(f'#{r.id} {rtype} at {dt_local.strftime("%b %d, %I:%M %p")}: "{r.message}"')

    if fired:
        lines.append("\nRecently fired (already sent ✅):")
        for r in fired:
            dt_local = to_local_aware(r.remind_at)
            lines.append(f'#{r.id} ✅ FIRED at {dt_local.strftime("%I:%M %p")}: "{r.message}" — DO NOT create a duplicate.')

    return "\n".join(lines)


async def delete_reminder(wa_id: str, reminder_id: int) -> str:
    from bot.services.reminder_scheduler import cancel_reminder
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder_id, Reminder.wa_id == wa_id)
        )
        rem = result.scalar_one_or_none()
        if not rem:
            return f"Reminder #{reminder_id} not found."
        await session.delete(rem)
        await session.commit()
    # Cancel the scheduled DateTrigger job so it doesn't fire orphaned.
    cancel_reminder(reminder_id)
    return f"Deleted reminder #{reminder_id}."


# ═══════════════════════════════════════════════════════════════
# To-Do List (Google Sheets-backed, persistent)
# ═══════════════════════════════════════════════════════════════


async def _get_user_db(wa_id: str):
    """Helper: resolve wa_id → User ORM object (with google_token_json)."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        return result.scalar_one_or_none()


async def add_todo(wa_id: str, items: str) -> str:
    """Add to-do items (comma-separated) to Google Sheets."""
    from bot.services.sheets import add_todo_to_sheet
    user_db = await _get_user_db(wa_id)
    if not user_db or not user_db.google_token_json:
        return "GOOGLE_NOT_CONNECTED"

    item_list = [i.strip() for i in items.split(",") if i.strip()]
    if not item_list:
        return "No items provided."

    result = add_todo_to_sheet(user_db, item_list)
    if hasattr(result, '__await__'):
        result = await result

    if "error" in result:
        return result["error"]

    lines = [f"✅ Added {result['added']} item(s) to your to-do list:\n"]
    for item in result.get("items", []):
        lines.append(f"  {item['index']}. {item['task']}")
    lines.append(f"\nTotal items: {result.get('total', '?')}")
    return "\n".join(lines)


async def get_todos(wa_id: str) -> str:
    """Get all to-do items from Google Sheets."""
    from bot.services.sheets import get_todos_from_sheet
    user_db = await _get_user_db(wa_id)
    if not user_db or not user_db.google_token_json:
        return "GOOGLE_NOT_CONNECTED"

    result = get_todos_from_sheet(user_db)
    if hasattr(result, '__await__'):
        result = await result

    if "error" in result:
        return result["error"]

    items = result.get("items", [])
    if not items:
        return "Your to-do list is empty."

    lines = ["Your to-do list:\n"]
    for item in items:
        status_icon = "✅" if "Done" in item.get("status", "") else "⬜"
        lines.append(f"  {item['index']}. {status_icon} {item['task']}")
    return "\n".join(lines)


async def complete_todo(wa_id: str, item_number: int, done: bool = True) -> str:
    """Mark a to-do item as done/undone in Google Sheets."""
    from bot.services.sheets import update_todo_status
    user_db = await _get_user_db(wa_id)
    if not user_db or not user_db.google_token_json:
        return "GOOGLE_NOT_CONNECTED"

    result = update_todo_status(user_db, item_number, done)
    if hasattr(result, '__await__'):
        result = await result

    if "error" in result:
        return result["error"]

    action = "completed" if done else "reopened"
    return f'✅ To-do #{result["item_index"]} "{result["task"]}" marked as {action}.'


async def delete_todo(wa_id: str, item_number: int) -> str:
    """Delete a to-do item from Google Sheets."""
    from bot.services.sheets import delete_todo_from_sheet
    user_db = await _get_user_db(wa_id)
    if not user_db or not user_db.google_token_json:
        return "GOOGLE_NOT_CONNECTED"

    result = delete_todo_from_sheet(user_db, item_number)
    if hasattr(result, '__await__'):
        result = await result

    if "error" in result:
        return result["error"]

    return f'🗑️ Deleted to-do #{result["item_index"]} "{result["task"]}".'
