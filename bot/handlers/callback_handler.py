"""FollowUp Bot — WhatsApp Interactive Reply Handler.

Routes interactive button/list replies to the correct business logic.

Button IDs mirror the old Telegram callback_data strings so the same
state-machine logic works unchanged.
"""

import logging
import json
import re

from sqlalchemy import select, delete as sql_delete

from bot.services.google_auth import handle_connect, handle_disconnect

from bot.config import config
from bot.database import async_session, ChatMemory, User
from bot.services.task_service import (
    get_conversation_state, set_conversation_state, clear_conversation_state,
    cancel_task, complete_task, parse_datetime_from_text,
)
from bot.services.whatsapp import send_message, send_buttons, send_list
from bot.utils.time import now_local
from bot.utils.intent import extract_meeting_datetime

logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


async def _purge_recent_chat_memory(wa_id: str, count: int = 6) -> int:
    """Delete the N most recent ChatMemory rows for this user.

    Called when the user taps Edit or otherwise abandons a flow — we
    don't want the LLM to see the broken half-finished turns when it
    re-engages on the next user message (the "I have scheduled" lie
    came from leaking failed-flow turns back into context).
    """
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if not u:
            return 0
        ids_to_delete = (await session.execute(
            select(ChatMemory.id)
            .where(ChatMemory.user_id == u.id)
            .order_by(ChatMemory.created_at.desc())
            .limit(count)
        )).scalars().all()
        if not ids_to_delete:
            return 0
        await session.execute(
            sql_delete(ChatMemory).where(ChatMemory.id.in_(list(ids_to_delete)))
        )
        await session.commit()
        return len(ids_to_delete)


# ═══════════════════════════════════════════════════════════════
# Main Router
# ═══════════════════════════════════════════════════════════════

async def handle_interactive_reply(wa_id: str, reply_id: str, reply_title: str = ""):
    """Route an interactive button/list reply by its ID."""
    logger.info(f"Interactive reply from {wa_id}: id={reply_id!r} title={reply_title!r}")
    reply_text = (reply_title or reply_id or "").strip().lower()

    if reply_id == "connect_google":
        await handle_connect(wa_id)
    elif reply_id == "disconnect_google":
        await handle_disconnect(wa_id)
    elif reply_id == "show_capabilities":
        await _send_capabilities(wa_id)
    elif reply_id.startswith("conflict_"):
        await _handle_conflict(wa_id, reply_id)
    elif reply_id.startswith("gmeet_confirm_"):
        await _handle_gmeet_confirm(wa_id, reply_id)
    elif reply_id.startswith("gmeet_contact_"):
        await _handle_gmeet_contact_choice(wa_id, reply_id)
    elif reply_id.startswith("meeting_add_calendar") or reply_text in (
        "add to the calendar", "add to the calender"
    ):
        await _handle_meeting_add_calendar(wa_id, reply_id)
    elif reply_id.startswith("meeting_decline") or reply_text == "no":
        await _handle_meeting_decline(wa_id)
    elif reply_id.startswith("task_"):
        await _handle_completion(wa_id, reply_id)
    elif reply_id.startswith("onboard_"):
        await _handle_onboarding(wa_id, reply_id)
    elif reply_id == "cancel_flow":
        await _handle_cancel(wa_id)
    else:
        send_message(wa_id, "❓ Unknown action. Please try again.")


async def _handle_meeting_add_calendar(wa_id: str, reply_id: str):
    """Handle attendee quick reply from the meeting invite template."""
    parts = reply_id.split("|", 1)
    meeting_link = parts[1].strip() if len(parts) == 2 else ""

    if meeting_link:
        send_message(
            wa_id,
            "Here is your meeting link:\n"
            f"🔗 {meeting_link}\n\n"
            "You can add it to your calendar manually from the invite details.",
        )
    else:
        send_message(
            wa_id,
            "I received your request. Please use the meeting link from the invite message "
            "to add this meeting to your calendar.",
        )


async def _handle_meeting_decline(wa_id: str):
    """Handle attendee decline quick reply from the meeting invite template."""
    logger.info(f"Meeting invite declined by attendee wa_id={wa_id}")
    send_message(wa_id, "No problem. I’ve noted that you declined this meeting invitation.")


# ═══════════════════════════════════════════════════════════════
# Google Meet deterministic continuation flow
# ═══════════════════════════════════════════════════════════════

def _gmeet_data_from_result(data: dict) -> dict:
    return {
        "attendee_name": data.get("attendee_name") or "",
        "attendee_email": data.get("attendee_email") or "",
        "attendee_phone": data.get("attendee_phone") or "",
        "title": data.get("title") or "",
        "start_time_iso": data.get("start_time_iso") or data.get("date_iso") or "",
        "duration_minutes": data.get("duration_minutes") or 60,
    }


def _extract_email(text: str, fallback_name: str = "") -> tuple[str, str]:
    """Return (name, email) from `Name{email}` or any text containing an email."""
    braced = re.match(r"^\s*(.*?)\s*\{\s*([^{}\s]+@[^{}\s]+\.[^{}\s]+)\s*\}\s*$", text)
    if braced:
        name = braced.group(1).strip() or fallback_name
        return name, braced.group(2).strip()

    match = _EMAIL_RE.search(text)
    if not match:
        return fallback_name, ""

    email = match.group(0).strip()
    name = text[:match.start()].strip(" -:{}") or fallback_name
    return name, email


async def send_gmeet_contact_picker(wa_id: str, data: dict):
    """Persist contact choices and show them as a WhatsApp list."""
    contacts = data.get("contacts") or []
    ctx = {
        "flow_kind": "gmeet",
        "gmeet_data": _gmeet_data_from_result(data),
        "contacts": contacts,
    }
    await set_conversation_state(wa_id, "awaiting_gmeet_contact", ctx)

    rows = []
    for i, contact in enumerate(contacts[:10]):
        name = (contact.get("name") or "Unknown")[:24]
        email = contact.get("email") or "No Gmail on this contact"
        rows.append({
            "id": f"gmeet_contact_{i}",
            "title": name,
            "description": email[:72],
        })

    if rows:
        send_list(
            wa_id,
            data.get("message") or "I found multiple matching contacts. Which one should I book with?",
            "Pick contact",
            [{"title": "Matching contacts", "rows": rows}],
        )
    else:
        send_message(wa_id, data.get("message") or "Please send the attendee's Gmail address.")


async def send_gmeet_email_request(wa_id: str, data: dict):
    ctx = {"flow_kind": "gmeet", "gmeet_data": _gmeet_data_from_result(data)}
    await set_conversation_state(wa_id, "awaiting_gmeet_email", ctx)
    send_message(wa_id, data.get("message") or "Please send the attendee's Gmail address.")


async def send_gmeet_time_request(wa_id: str, data: dict):
    ctx = {"flow_kind": "gmeet", "gmeet_data": _gmeet_data_from_result(data)}
    await set_conversation_state(wa_id, "awaiting_gmeet_time", ctx)
    send_message(wa_id, data.get("message") or "Please send the meeting date and time.")


async def send_gmeet_conflict_buttons(wa_id: str, data: dict):
    ctx = {
        "flow_kind": "gmeet",
        "gmeet_data": _gmeet_data_from_result(data),
        "conflicts": data.get("conflicts") or [],
    }
    await set_conversation_state(wa_id, "awaiting_conflict_resolution", ctx)
    send_buttons(wa_id, data.get("message") or "That time conflicts with another event.", [
        {"id": "conflict_keep_both", "title": "📌 Keep both"},
        {"id": "conflict_move_later", "title": "🕐 Change time"},
        {"id": "cancel_flow", "title": "❌ Cancel"},
    ])


async def _parse_user_meeting_time(text: str) -> str:
    """Parse a user's direct reply to "when?" → ISO string, or "" if unreadable.

    Deterministic first (handles "today 12 pm", "tomorrow 5pm", "Friday 3:30"
    instantly and reliably), LLM only as a fallback for unusual phrasings.
    """
    dt = extract_meeting_datetime(text, now_local())
    if dt is not None:
        return dt.isoformat()
    try:
        parsed = await parse_datetime_from_text(text, config.BOT_TIMEZONE)
        if parsed.get("date_iso") and float(parsed.get("confidence") or 0.0) >= 0.5:
            return parsed["date_iso"]
    except Exception as e:
        logger.warning(f"LLM datetime fallback failed: {e}")
    return ""


async def handle_gmeet_text_reply(wa_id: str, text: str, flow_state: str, ctx: dict) -> bool:
    """Deterministic continuation for an ACTIVE gmeet flow.

    Once we're collecting fields for a specific meeting, the bot — not the
    model — accumulates them. Each reply is MERGED into the saved context,
    so giving the email never drops an already-known time (and vice-versa),
    then `set_gmeet` re-runs with everything collected so far. Small models
    can't be trusted to carry half-filled state across turns, which is what
    caused the "asks for time after you gave the email" bug.

    Outside an active flow, the model still drives (this returns False).
    Returning True = handled (LLM won't see the message).
    """
    if ctx.get("flow_kind") != "gmeet" or not flow_state:
        return False

    gd = ctx.get("gmeet_data", {})

    # ── Confirmation gate ──────────────────────────────────────
    if flow_state == "awaiting_gmeet_confirmation":
        t = text.strip().lower()
        if t in ("yes", "y", "save", "confirm", "ok", "okay", "yes save", "yep"):
            await _run_gmeet_from_context(wa_id, ctx, confirmed=True)
            return True
        if t in ("no", "n", "cancel", "abort", "edit", "change"):
            await clear_conversation_state(wa_id)
            await _purge_recent_chat_memory(wa_id, count=6)
            send_message(wa_id, "❌ Okay, cancelled. Tell me the details again whenever you're ready.")
            return True
        # Changed their mind / asked something else → drop the gate, let the model handle it.
        await clear_conversation_state(wa_id)
        return False

    # ── Conflict resolution typed instead of tapped ────────────
    if flow_state == "awaiting_conflict_resolution":
        t = text.strip().lower()
        if t in ("keep both", "keep", "yes", "proceed", "both"):
            await _run_gmeet_from_context(wa_id, ctx, skip_conflict_check=True)
            return True
        iso = await _parse_user_meeting_time(text)
        if iso:
            gd["start_time_iso"] = iso
            ctx["gmeet_data"] = gd
            await _run_gmeet_from_context(wa_id, ctx)
            return True
        send_message(wa_id, "Tap *Keep both*, or send a new date and time for this Google Meet.")
        return True

    # ── Email / contact step — MERGE email, keep the known time ─
    if flow_state in ("awaiting_gmeet_email", "awaiting_gmeet_contact"):
        if flow_state == "awaiting_gmeet_contact" and text.strip().isdigit():
            contacts = ctx.get("contacts") or []
            idx = int(text.strip()) - 1
            if 0 <= idx < len(contacts):
                await _continue_gmeet_with_contact(wa_id, ctx, contacts[idx])
                return True
        name, email = _extract_email(text, gd.get("attendee_name", ""))
        if email:
            gd["attendee_name"] = name or gd.get("attendee_name", "")
            gd["attendee_email"] = email
            ctx["gmeet_data"] = gd
            await _run_gmeet_from_context(wa_id, ctx)   # keeps existing start_time_iso
            return True
        if flow_state == "awaiting_gmeet_contact":
            send_message(wa_id, "Pick a contact, type its number, or send the Gmail address.")
        else:
            send_message(wa_id, "Please send a valid Gmail, e.g. name@example.com.")
        return True

    # ── Time step — MERGE time, keep the known email ────────────
    if flow_state == "awaiting_gmeet_time":
        iso = await _parse_user_meeting_time(text)
        if not iso:
            send_message(
                wa_id,
                "I couldn't read a time from that. Try *tomorrow 12 PM*, "
                "*today 5pm*, or *Friday 3:30 pm*.",
            )
            return True
        gd["start_time_iso"] = iso
        ctx["gmeet_data"] = gd
        await _run_gmeet_from_context(wa_id, ctx)        # keeps existing attendee_email
        return True

    return False


async def _handle_gmeet_confirm(wa_id: str, data: str):
    """Handle Yes/Edit reply on the confirmation gate.

    'Yes'  → re-run set_gmeet with _confirmed=True, which actually creates
             the event and sends the assignee notification.
    'Edit' → clear state, PURGE the last few ChatMemory turns (so the LLM
             doesn't see the failed proposal and try to re-confirm it in
             text), and prompt fresh.
    """
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_gmeet_confirmation":
        send_message(wa_id, "⏳ This confirmation has expired. Please start again.")
        return

    action = data.replace("gmeet_confirm_", "")
    if action == "yes":
        # Deterministic create — bypasses the LLM entirely
        await _run_gmeet_from_context(wa_id, ctx, confirmed=True)
    elif action == "edit":
        await clear_conversation_state(wa_id)
        # The "I have scheduled" lie came from the LLM re-reading the
        # failed-flow turns. Wipe them so it can't.
        purged = await _purge_recent_chat_memory(wa_id, count=6)
        logger.info(f"Edit pressed; purged {purged} ChatMemory rows for {wa_id}")
        send_message(
            wa_id,
            "✏️ Cancelled. Send me the meeting details fresh.\n"
            "For example: *Meet with Harsh tomorrow 12 PM* or "
            "*Call with sarah@example.com Friday 3 PM*.",
        )
    else:
        send_message(wa_id, "❓ Unknown confirmation action.")


async def _handle_gmeet_contact_choice(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_gmeet_contact":
        send_message(wa_id, "⏳ This contact selection has expired.")
        return

    try:
        idx = int(data.replace("gmeet_contact_", ""))
    except ValueError:
        send_message(wa_id, "❌ Invalid contact selection.")
        return

    contacts = ctx.get("contacts") or []
    if idx >= len(contacts):
        send_message(wa_id, "❌ Invalid contact selection.")
        return
    await _continue_gmeet_with_contact(wa_id, ctx, contacts[idx])


async def _continue_gmeet_with_contact(wa_id: str, ctx: dict, contact: dict):
    gd = ctx.get("gmeet_data", {})
    gd["attendee_name"] = contact.get("name") or gd.get("attendee_name", "")
    gd["attendee_email"] = contact.get("email") or ""
    gd["attendee_phone"] = contact.get("phone") or ""
    ctx["gmeet_data"] = gd

    if not gd["attendee_email"]:
        await set_conversation_state(wa_id, "awaiting_gmeet_email", ctx)
        send_message(wa_id, f"I found {gd['attendee_name']}, but that contact has no Gmail. Please send their Gmail.")
        return

    await _run_gmeet_from_context(wa_id, ctx)


async def _run_gmeet_from_context(wa_id: str, ctx: dict,
                                   skip_conflict_check: bool = False,
                                   confirmed: bool = False):
    """Re-invoke set_gmeet using the saved conversation context.

    The deterministic callback handler is the ONLY caller that may set
    `confirmed=True` — the LLM cannot bypass the confirmation gate.
    """
    from bot.tools import set_gmeet

    gd = ctx.get("gmeet_data", {})
    result = await set_gmeet(
        wa_id,
        attendee_name=gd.get("attendee_name", ""),
        attendee_email=gd.get("attendee_email", ""),
        attendee_phone=gd.get("attendee_phone", ""),
        title=gd.get("title", ""),
        start_time_iso=gd.get("start_time_iso", ""),
        duration_minutes=gd.get("duration_minutes", 30),  # PRD §FR-3 default = 30
        skip_conflict_check=skip_conflict_check,
        _confirmed=confirmed,
    )
    try:
        data = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        send_message(wa_id, str(result))
        await clear_conversation_state(wa_id)
        return

    await _handle_gmeet_tool_result(wa_id, data)


async def _handle_gmeet_tool_result(wa_id: str, data: dict):
    action = data.get("action")
    if action == "created":
        await clear_conversation_state(wa_id)
        send_message(wa_id, data.get("message") or "✅ Google Meet scheduled.")
    elif action == "awaiting_confirmation":
        await send_gmeet_confirm_buttons(wa_id, data)
    elif action == "conflict":
        await send_gmeet_conflict_buttons(wa_id, data)
    elif action == "missing_time":
        await send_gmeet_time_request(wa_id, data)
    elif action == "pick_contact":
        await send_gmeet_contact_picker(wa_id, data)
    elif action in ("need_email", "no_contact"):
        await send_gmeet_email_request(wa_id, data)
    elif action == "error":
        await clear_conversation_state(wa_id)
        send_message(wa_id, data.get("message") or "❌ Could not schedule that meeting.")
    else:
        send_message(wa_id, data.get("message") or "I need one more detail to schedule that meeting.")


async def send_gmeet_confirm_buttons(wa_id: str, data: dict):
    """Persist the proposed meeting and present Yes/Edit/Cancel buttons.

    This is the deterministic confirmation gate (PRD §FR-7). The LLM is
    OUT of the loop until the user taps a button.
    """
    ctx = {
        "flow_kind": "gmeet",
        "gmeet_data": _gmeet_data_from_result(data),
    }
    await set_conversation_state(wa_id, "awaiting_gmeet_confirmation", ctx)
    send_buttons(wa_id, data.get("message") or "Save this meeting?", [
        {"id": "gmeet_confirm_yes",  "title": "✅ Yes, save"},
        {"id": "gmeet_confirm_edit", "title": "✏️ Edit"},
        {"id": "cancel_flow",        "title": "❌ Cancel"},
    ])


# ═══════════════════════════════════════════════════════════════
# Conflict Resolution
# ═══════════════════════════════════════════════════════════════

async def _handle_conflict(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_conflict_resolution":
        send_message(wa_id, "⏳ This action has expired.")
        return

    action = data.replace("conflict_", "")

    # Only the gmeet flow raises a conflict prompt. "Keep both" proceeds to
    # the confirmation gate (skip the re-check); "Change time" hands the next
    # message back to the model to re-propose at a new time.
    if action == "keep_both":
        await _run_gmeet_from_context(wa_id, ctx, skip_conflict_check=True)
    elif action in ("move_later", "move_earlier", "reschedule_existing"):
        await set_conversation_state(wa_id, "awaiting_gmeet_time", ctx)
        send_message(
            wa_id,
            "Sure — what new date and time should I use for this Google Meet? "
            "(e.g. tomorrow 2 PM)",
        )


# ═══════════════════════════════════════════════════════════════
# Completion Check
# ═══════════════════════════════════════════════════════════════

async def _handle_completion(wa_id: str, data: str):
    parts = data.split("_")
    if len(parts) < 3:
        send_message(wa_id, "❌ Invalid action.")
        return
    action = parts[1]
    try:
        task_id = int(parts[2])
    except (ValueError, IndexError):
        send_message(wa_id, "❌ Invalid task reference.")
        return

    if action == "done":
        ok = await complete_task(task_id)
        send_message(wa_id, "✅ Marked as done! Great work. 🎉" if ok else "❌ Could not mark as done.")
    elif action == "reschedule":
        send_message(
            wa_id,
            "📅 When would you like to reschedule? Send me the new date and time.\n"
            "For example: \"Thursday 3 PM\" or \"Tomorrow 10 AM\"",
        )
        await set_conversation_state(wa_id, "awaiting_reschedule_time", {"task_id": task_id})


# ═══════════════════════════════════════════════════════════════
# Cancel / Onboarding
# ═══════════════════════════════════════════════════════════════

async def _handle_cancel(wa_id: str):
    _, ctx = await get_conversation_state(wa_id)
    task_id = ctx.get("task_data", {}).get("task_id")
    if task_id:
        await cancel_task(task_id)
    await clear_conversation_state(wa_id)
    send_message(wa_id, "❌ Cancelled.")


async def _handle_onboarding(wa_id: str, data: str):
    """Route FR-1 onboarding button taps to the onboarding state machine."""
    from bot.services.onboarding import (
        confirm_whatsapp, reject_whatsapp,
        confirm_timezone, change_timezone_prompt,
    )
    if data == "onboard_wa_yes":
        await confirm_whatsapp(wa_id)
    elif data == "onboard_wa_other":
        await reject_whatsapp(wa_id)
    elif data == "onboard_tz_yes":
        await confirm_timezone(wa_id)
    elif data == "onboard_tz_change":
        await change_timezone_prompt(wa_id)
    else:
        logger.info(f"Unknown onboarding action: {data}")


# ═══════════════════════════════════════════════════════════════
# Keyboard Builders (used by main.py)
# ═══════════════════════════════════════════════════════════════

def send_completion_buttons(wa_id: str, task_id: int, message: str):
    """Send done/reschedule buttons for a completion check."""
    send_buttons(wa_id, message, [
        {"id": f"task_done_{task_id}",       "title": "✅ Done"},
        {"id": f"task_reschedule_{task_id}", "title": "📅 Reschedule"},
    ])


def send_connect_button(wa_id: str, message: str):
    """Send a 'Connect Google' button with a context message."""
    send_buttons(wa_id, message, [
        {"id": "connect_google",    "title": "🔗 Connect Google"},
        {"id": "show_capabilities", "title": "ℹ️ What can I do?"},
    ])


async def _send_capabilities(wa_id: str):
    """Send a capabilities overview."""
    send_message(
        wa_id,
        "Here's what I can do:\n\n"
        "📅 *Calendar* — Create, view, update & delete events\n"
        "🖥 *Google Meet* — Schedule meetings with Meet links\n"
        "📋 *Tasks* — Track commitments with reminders\n"
        "📝 *Notes* — Save & retrieve quick notes\n"
        "📇 *Contacts* — Search your Google Contacts\n"
        "⏰ *Reminders* — One-time & daily recurring\n"
        "🎧 *Voice Notes* — Send voice, I'll transcribe & act\n\n"
        "Just tell me what you need in plain language!",
    )
