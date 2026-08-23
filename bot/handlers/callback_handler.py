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
    cancel_task, parse_datetime_from_text,
)
from bot.services.whatsapp import (
    send_message, send_message_async,
    send_buttons, send_buttons_async,
    send_list, send_list_async,
)
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
    elif reply_id == "show_terms":
        from bot.services.onboarding import send_terms
        await send_terms(wa_id)
    elif reply_id == "show_privacy":
        from bot.services.onboarding import send_privacy
        await send_privacy(wa_id)
    elif reply_id.startswith("conflict_"):
        await _handle_conflict(wa_id, reply_id)
    elif reply_id.startswith("gmeet_confirm_"):
        await _handle_gmeet_confirm(wa_id, reply_id)
    elif reply_id.startswith("calendar_reschedule_confirm_"):
        await _handle_calendar_reschedule_confirm(wa_id, reply_id)
    elif reply_id.startswith("add_attendee_confirm_"):
        await _handle_add_attendee_confirm(wa_id, reply_id)
    elif reply_id.startswith("add_attendee_contact_"):
        await _handle_add_attendee_contact_choice(wa_id, reply_id)
    elif reply_id.startswith("add_attendee_event_"):
        await _handle_add_attendee_event_choice(wa_id, reply_id)
    elif reply_id.startswith("gmeet_contact_"):
        await _handle_gmeet_contact_choice(wa_id, reply_id)
    elif reply_id.startswith("mtgsummary_"):
        await _handle_mtgsummary_choice(wa_id, reply_id)
    elif reply_id == "mtg_todos_add":
        await _handle_mtg_todos_add(wa_id)
    elif reply_id == "mtg_remind_set":
        await _handle_mtg_remind_set(wa_id)
    elif reply_id.startswith("meeting_add_calendar") or reply_text in (
        "add to the calendar", "add to the calender"
    ):
        await _handle_meeting_add_calendar(wa_id, reply_id)
    elif reply_id.startswith("meeting_decline") or reply_text == "no":
        await _handle_meeting_decline(wa_id)
    elif reply_id.startswith("onboard_"):
        await _handle_onboarding(wa_id, reply_id)
    elif reply_id == "cancel_flow":
        await _handle_cancel(wa_id)
    else:
        await send_message_async(wa_id, "unknown action. try again.")


async def _handle_meeting_add_calendar(wa_id: str, reply_id: str):
    """Handle attendee quick reply from the meeting invite template."""
    parts = reply_id.split("|", 1)
    meeting_link = parts[1].strip() if len(parts) == 2 else ""

    if meeting_link:
        await send_message_async(
            wa_id,
            "here is your meeting link:\n"
            f"{meeting_link}\n\n"
            "you can add it to your calendar from the invite details.",
        )
    else:
        await send_message_async(
            wa_id,
            "got it. use the meeting link from the invite message "
            "to add this meeting to your calendar.",
        )


async def _handle_meeting_decline(wa_id: str):
    """Handle attendee decline quick reply from the meeting invite template."""
    logger.info(f"Meeting invite declined by attendee wa_id={wa_id}")
    await send_message_async(wa_id, "no problem. i've noted that you declined this meeting invitation.")


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
        # 30 to match set_gmeet and PRD §FR-3. This defaulted to 60, so any
        # payload with a falsy duration silently doubled the meeting length.
        "duration_minutes": data.get("duration_minutes") or 30,
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
        await send_list_async(
            wa_id,
            data.get("message") or "i found multiple matching contacts. which one should i book with?",
            "Pick contact",
            [{"title": "Matching contacts", "rows": rows}],
        )
    else:
        await send_message_async(wa_id, data.get("message") or "send the attendee's gmail address.")


async def send_gmeet_email_request(wa_id: str, data: dict):
    ctx = {"flow_kind": "gmeet", "gmeet_data": _gmeet_data_from_result(data)}
    await set_conversation_state(wa_id, "awaiting_gmeet_email", ctx)
    await send_message_async(wa_id, data.get("message") or "send the attendee's gmail address.")


async def send_gmeet_time_request(wa_id: str, data: dict):
    ctx = {"flow_kind": "gmeet", "gmeet_data": _gmeet_data_from_result(data)}
    await set_conversation_state(wa_id, "awaiting_gmeet_time", ctx)
    await send_message_async(wa_id, data.get("message") or "send the meeting date and time.")


async def send_gmeet_conflict_buttons(wa_id: str, data: dict):
    ctx = {
        "flow_kind": "gmeet",
        "gmeet_data": _gmeet_data_from_result(data),
        "conflicts": data.get("conflicts") or [],
    }
    await set_conversation_state(wa_id, "awaiting_conflict_resolution", ctx)
    await send_buttons_async(wa_id, data.get("message") or "that time conflicts with another event.", [
        {"id": "conflict_keep_both", "title": "Keep both"},
        {"id": "conflict_move_later", "title": "Change time"},
        {"id": "cancel_flow", "title": "Cancel"},
    ])


# ═══════════════════════════════════════════════════════════════
# WhatsApp Flow (native form) — send + completion
# ═══════════════════════════════════════════════════════════════

_FLOW_SCREEN = "SCHEDULE"
_FLOW_SLOT_MIN, _FLOW_SLOT_MAX = 4, 12   # form offers 04:00–12:00 only


def _epoch_ms_utc_midnight(d) -> str:
    """Date → epoch-ms string at UTC midnight (what DatePicker expects)."""
    from datetime import datetime as _dt, timezone as _tz
    return str(int(_dt(d.year, d.month, d.day, tzinfo=_tz.utc).timestamp() * 1000))


async def _first_free_slot_hhmm(wa_id: str, day_local) -> str:
    """First conflict-free half-hour between 04:00 and 12:00 on that day.

    Uses the user's Google Calendar; falls back to 09:00 when Google is
    unavailable so the form always opens with a valid default.
    """
    try:
        async with async_session() as session:
            u = (await session.execute(
                select(User).where(User.wa_id == wa_id)
            )).scalar_one_or_none()
        if not u or not u.google_token_json:
            return "09:00"
        from bot.services.calendar import find_free_slots
        start = day_local.replace(hour=_FLOW_SLOT_MIN, minute=0,
                                  second=0, microsecond=0)
        if day_local.date() == now_local().date() and now_local() > start:
            start = now_local()
        slots = await find_free_slots(u, start, duration_minutes=30, count=17)
        for s in slots:
            in_window = (_FLOW_SLOT_MIN <= s.hour < _FLOW_SLOT_MAX) or \
                        (s.hour == _FLOW_SLOT_MAX and s.minute == 0)
            if in_window and s.date() == day_local.date():
                return f"{s.hour:02d}:{s.minute:02d}"
    except Exception as e:
        logger.warning(f"free-slot lookup failed (default 09:00): {e}")
    return "09:00"


async def send_gmeet_flow(wa_id: str, data: dict):
    """Open the native meeting form, prefilled with everything known.

    Used when the attendee email is missing (PRD: full-info requests keep
    the classic conflict-check + confirm path instead).
    """
    from datetime import datetime as _dt
    from bot.services.whatsapp import send_flow

    gd = _gmeet_data_from_result(data)
    today = now_local()
    day = today
    time_init = ""

    iso = gd.get("start_time_iso") or ""
    if iso:
        try:
            known = _dt.fromisoformat(iso)
            day = known if known.date() >= today.date() else today
            in_window = (_FLOW_SLOT_MIN <= known.hour < _FLOW_SLOT_MAX) or \
                        (known.hour == _FLOW_SLOT_MAX and known.minute == 0)
            if in_window and known.minute in (0, 30):
                time_init = f"{known.hour:02d}:{known.minute:02d}"
        except ValueError:
            pass
    if not time_init:
        time_init = await _first_free_slot_hhmm(wa_id, day)

    flow_data = {
        "topic_init": gd.get("title") or "",
        "name_init": gd.get("attendee_name") or "",
        "email_init": gd.get("attendee_email") or "",
        "min_date": _epoch_ms_utc_midnight(today.date()),
        "date_init": _epoch_ms_utc_midnight(day.date()),
        "time_init": time_init,
    }

    # Keep phone (not on the form) + flow kind for the completion step.
    # Token embeds wa_id so the (dynamic) endpoint can resolve the user.
    import uuid as _uuid
    flow_token = f"{wa_id}:{_uuid.uuid4().hex[:12]}"
    await set_conversation_state(wa_id, "awaiting_gmeet_flow",
                                 {"flow_kind": "gmeet", "gmeet_data": gd})

    if config.WA_GMEET_FLOW_DYNAMIC:
        # Endpoint supplies the first screen + live availability via INIT.
        ok = send_flow(
            wa_id,
            "i need a couple of details to set this meeting up — tap below.",
            config.WA_GMEET_FLOW_ID, _FLOW_SCREEN, {}, cta="Add details",
            flow_token=flow_token, flow_action="data_exchange",
        )
    else:
        # Static Flow: prefill everything at send time.
        ok = send_flow(
            wa_id,
            "i need a couple of details to set this meeting up — tap below.",
            config.WA_GMEET_FLOW_ID, _FLOW_SCREEN, flow_data, cta="Add details",
            flow_token=flow_token,
        )
    if not ok:
        # Flow send failed (draft flow, bad id, API error) → legacy prompt.
        await send_gmeet_email_request(wa_id, data)


async def handle_gmeet_flow_completion(wa_id: str, response_json: str):
    """Handle the form submission (nfm_reply) → conflict check → create.

    The form's Schedule tap IS the confirmation, so a clean proposal goes
    straight to creation — no second confirm card. Conflicts still surface
    through the classic conflict buttons.
    """
    from datetime import datetime as _dt, timezone as _tz

    try:
        form = json.loads(response_json or "{}")
    except json.JSONDecodeError:
        await send_message_async(wa_id, "i couldn't read that form. tell me the details in chat instead.")
        return

    raw_date = str(form.get("date") or "").strip()
    raw_time = str(form.get("time") or "").strip()
    start_iso = ""
    try:
        if raw_date.isdigit():                       # DatePicker epoch-ms
            d = _dt.fromtimestamp(int(raw_date) / 1000, tz=_tz.utc).date()
        else:                                        # "YYYY-MM-DD" fallback
            d = _dt.fromisoformat(raw_date).date()
        h, m = [int(x) for x in raw_time.split(":")]
        local = now_local().replace(year=d.year, month=d.month, day=d.day,
                                    hour=h, minute=m, second=0, microsecond=0)
        start_iso = local.isoformat()
    except (ValueError, TypeError):
        await send_message_async(wa_id, "that date/time didn't come through. send it in chat, e.g. *tomorrow 9 am*.")
        return

    # Phone survives outside the form via the saved context.
    _, ctx = await get_conversation_state(wa_id)
    saved = (ctx or {}).get("gmeet_data", {})

    gd = {
        "attendee_name": (form.get("name") or saved.get("attendee_name") or "").strip(),
        "attendee_email": (form.get("email") or "").strip(),
        "attendee_phone": saved.get("attendee_phone", ""),
        "title": (form.get("topic") or "").strip(),
        "start_time_iso": start_iso,
        "duration_minutes": 30,
    }
    new_ctx = {"flow_kind": "gmeet", "gmeet_data": gd}
    await set_conversation_state(wa_id, "awaiting_gmeet_flow", new_ctx)

    from bot.tools import set_gmeet
    result = await set_gmeet(
        wa_id,
        attendee_name=gd["attendee_name"],
        attendee_email=gd["attendee_email"],
        attendee_phone=gd["attendee_phone"],
        title=gd["title"],
        start_time_iso=gd["start_time_iso"],
        duration_minutes=30,
    )
    try:
        data = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        await clear_conversation_state(wa_id)
        await send_message_async(wa_id, str(result))
        return

    if data.get("action") == "awaiting_confirmation":
        # Form was the confirmation — create directly.
        await _run_gmeet_from_context(wa_id, new_ctx, confirmed=True)
        return
    await _handle_gmeet_tool_result(wa_id, data)


# ═══════════════════════════════════════════════════════════════
# Email-from-chat (WhatsApp Flow wrapper) — AI never touches content
# ═══════════════════════════════════════════════════════════════

async def send_email_flow(wa_id: str, to_init: str = "") -> bool:
    """Open the native email form (To / Subject / Body).

    Privacy contract: everything is typed by the user inside the Flow and
    goes Flow -> Gmail. The AI only opens the form; it never writes or reads
    the subject/body. `to_init` is an optional recipient prefill taken from
    a literal address the user typed — never invented.

    Returns False when no Flow is configured or the send failed. The caller
    must NOT fall back to collecting the body in chat (that would defeat the
    whole point), so a False result becomes a plain "not available" reply.
    """
    from bot.services.whatsapp import send_flow
    import uuid as _uuid

    if not config.WA_EMAIL_FLOW_ID:
        return False

    flow_token = f"email:{wa_id}:{_uuid.uuid4().hex[:12]}"
    flow_data = {"to_init": (to_init or "").strip()}
    return send_flow(
        wa_id,
        "tap below to write your email — i'll send it from your gmail, "
        "exactly as you type it. i don't read or change what you write.",
        config.WA_EMAIL_FLOW_ID, "COMPOSE", flow_data,
        cta="Write email", flow_token=flow_token,
    )


async def handle_email_flow_completion(wa_id: str, form: dict):
    """Send the email from the submitted Flow form.

    The AI is OUT of the loop: `to`, `subject`, `body` come verbatim from the
    user's form input and are passed straight to Gmail.
    """
    to = (form.get("to") or "").strip()
    subject = (form.get("subject") or "").strip()
    body = form.get("body") or ""

    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()

    if not u or not u.google_token_json:
        await send_buttons_async(
            wa_id, "connect google first so i can send from your gmail.",
            [{"id": "connect_google", "title": "Connect Google"}],
        )
        return

    from bot.services.gmail import send_email, GmailScopeError, is_valid_email

    if not is_valid_email(to):
        await send_message_async(wa_id, "that recipient address looks off. start the email again and double-check it.")
        return
    if not body.strip():
        await send_message_async(wa_id, "the message was empty, so i didn't send anything.")
        return

    try:
        await send_email(u, to=to, subject=subject, body=body)
    except GmailScopeError:
        await send_buttons_async(
            wa_id,
            "i need send permission on your gmail. tap to update google access, then try again.",
            [{"id": "connect_google", "title": "Update access"}],
        )
        return
    except ValueError as e:
        await send_message_async(wa_id, f"couldn't send — {e}.")
        return
    except Exception:
        await send_message_async(wa_id, "gmail wouldn't send that just now. give it another go in a moment.")
        return

    await send_message_async(wa_id, f"sent to {to}.\nsubject: {subject or '(no subject)'}")


async def handle_flow_completion(wa_id: str, response_json: str):
    """Route any WhatsApp Flow submission (nfm_reply) to the right handler.

    The email and meeting forms both arrive as nfm_reply; we tell them apart
    by the payload — the email form stamps `intent=send_email` and carries a
    `body` field, which the meeting form never has.
    """
    try:
        form = json.loads(response_json or "{}")
    except json.JSONDecodeError:
        await send_message_async(wa_id, "i couldn't read that form. tell me what you need in chat.")
        return

    is_email = form.get("intent") == "send_email" or ("body" in form and "to" in form)
    if is_email:
        await handle_email_flow_completion(wa_id, form)
        return

    # Default: the meeting form (parses the raw json itself).
    await handle_gmeet_flow_completion(wa_id, response_json)


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
    # Add-attendee runs its own small accumulator: the only thing it can be
    # waiting on is an email or a picker choice, so a typed reply must never
    # reach the model — that's where the re-asking starts.
    if ctx.get("flow_kind") == "add_attendee" and flow_state:
        return await _handle_add_attendee_text_reply(wa_id, text, flow_state, ctx)

    # Post-meeting summary flow: action gate + reminder-time question.
    if ctx.get("flow_kind") == "mtg_summary" and flow_state:
        return await _handle_mtg_summary_text_reply(wa_id, text, flow_state, ctx)

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
            await send_message_async(wa_id, "okay, cancelled. tell me the details again whenever you're ready.")
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
        await send_message_async(wa_id, "tap *Keep both*, or send a new date and time for this google meet.")
        return True

    # ── Native form open, but the user typed instead ────────────
    # Accept an email or a time in chat exactly like the legacy steps;
    # anything else falls through to the model.
    if flow_state == "awaiting_gmeet_flow":
        name, email = _extract_email(text, gd.get("attendee_name", ""))
        if email:
            gd["attendee_name"] = name or gd.get("attendee_name", "")
            gd["attendee_email"] = email
            ctx["gmeet_data"] = gd
            await _run_gmeet_from_context(wa_id, ctx)
            return True
        iso = await _parse_user_meeting_time(text)
        if iso:
            gd["start_time_iso"] = iso
            ctx["gmeet_data"] = gd
            await _run_gmeet_from_context(wa_id, ctx)
            return True
        return False

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
            await send_message_async(wa_id, "pick a contact, type its number, or send the gmail address.")
        else:
            await send_message_async(wa_id, "send a valid gmail, e.g. name@example.com.")
        return True

    # ── Time step — MERGE time, keep the known email ────────────
    if flow_state == "awaiting_gmeet_time":
        iso = await _parse_user_meeting_time(text)
        if not iso:
            await send_message_async(
                wa_id,
                "i couldn't read a time from that. try *tomorrow 12 pm*, "
                "*today 5pm*, or *friday 3:30 pm*.",
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
        await send_message_async(wa_id, "this confirmation has expired. start again when ready.")
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
        await send_message_async(
            wa_id,
            "cancelled. send me the meeting details fresh.\n"
            "for example: *meet with harsh tomorrow 12 pm* or "
            "*call with sarah@example.com friday 3 pm*.",
        )
    else:
        await send_message_async(wa_id, "unknown confirmation action.")


async def _handle_calendar_reschedule_confirm(wa_id: str, data: str):
    """Apply a calendar move only after the user confirms the proposal."""
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_calendar_reschedule_confirmation":
        await send_message_async(wa_id, "this confirmation has expired. start again when ready.")
        return

    action = data.replace("calendar_reschedule_confirm_", "")
    if action == "yes":
        from bot.tools import calendar_reschedule

        rd = ctx.get("calendar_reschedule_data", {})
        result = await calendar_reschedule(
            wa_id,
            query=rd.get("query", ""),
            new_start_time_iso=rd.get("new_start_time_iso", ""),
            duration_minutes=rd.get("duration_minutes", 0),
            _confirmed=True,
        )
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            payload = {"action": "error", "message": str(result)}
        if payload.get("action") == "calendar_rescheduled":
            await clear_conversation_state(wa_id)
        await send_message_async(wa_id, payload.get("message") or "couldn't reschedule that event.")
    elif action == "edit":
        await clear_conversation_state(wa_id)
        await send_message_async(wa_id, "okay. send the event name and the new date and time.")
    else:
        await send_message_async(wa_id, "unknown confirmation action.")


async def _handle_gmeet_contact_choice(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_gmeet_contact":
        await send_message_async(wa_id, "this contact selection has expired.")
        return

    try:
        idx = int(data.replace("gmeet_contact_", ""))
    except ValueError:
        await send_message_async(wa_id, "invalid contact selection.")
        return

    contacts = ctx.get("contacts") or []
    if idx >= len(contacts):
        await send_message_async(wa_id, "invalid contact selection.")
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
        await send_message_async(wa_id, f"I found {gd['attendee_name']}, but that contact has no Gmail. Please send their Gmail.")
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
        await send_message_async(wa_id, str(result))
        await clear_conversation_state(wa_id)
        return

    await _handle_gmeet_tool_result(wa_id, data)


async def _handle_gmeet_tool_result(wa_id: str, data: dict):
    action = data.get("action")
    if action == "created":
        await clear_conversation_state(wa_id)
        await send_message_async(wa_id, data.get("message") or "google meet scheduled.")
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
        await send_message_async(wa_id, data.get("message") or "couldn't schedule that meeting.")
    else:
        await send_message_async(wa_id, data.get("message") or "I need one more detail to schedule that meeting.")


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
    await send_buttons_async(wa_id, data.get("message") or "save this meeting?", [
        {"id": "gmeet_confirm_yes",  "title": "Confirm"},
        {"id": "gmeet_confirm_edit", "title": "Edit"},
        {"id": "cancel_flow",        "title": "Cancel"},
    ])


async def send_calendar_reschedule_confirm_buttons(wa_id: str, data: dict):
    """Persist a proposed move and make the deterministic callback the gate."""
    ctx = {
        "flow_kind": "calendar_reschedule",
        "calendar_reschedule_data": {
            "query": data.get("event_label") or "",
            "new_start_time_iso": data.get("new_start_time_iso") or "",
            "duration_minutes": data.get("duration_minutes") or 0,
        },
    }
    await set_conversation_state(wa_id, "awaiting_calendar_reschedule_confirmation", ctx)
    await send_buttons_async(wa_id, data.get("message") or "move this event?", [
        {"id": "calendar_reschedule_confirm_yes", "title": "Confirm"},
        {"id": "calendar_reschedule_confirm_edit", "title": "Edit"},
        {"id": "cancel_flow", "title": "Cancel"},
    ])


# ═══════════════════════════════════════════════════════════════
# Add-attendee to an existing event (deterministic, LLM out of the loop)
# ═══════════════════════════════════════════════════════════════

def _add_attendee_ctx(data: dict, **extra) -> dict:
    ctx = {
        "flow_kind": "add_attendee",
        "add_attendee_data": {
            "query": data.get("query") or data.get("event_label") or "",
            "event_id": data.get("event_id") or "",
            "event_label": data.get("event_label") or "",
            "attendee_name": data.get("attendee_name") or "",
            "attendee_email": data.get("attendee_email") or "",
            "attendee_phone": data.get("attendee_phone") or "",
        },
    }
    ctx.update(extra)
    return ctx


async def send_add_attendee_confirm_buttons(wa_id: str, data: dict):
    """Final gate before a guest is actually written to the event."""
    await set_conversation_state(
        wa_id, "awaiting_add_attendee_confirmation", _add_attendee_ctx(data),
    )
    await send_buttons_async(wa_id, data.get("message") or "add this person to the event?", [
        {"id": "add_attendee_confirm_yes", "title": "Confirm"},
        {"id": "cancel_flow", "title": "Cancel"},
    ])


async def send_add_attendee_contact_picker(wa_id: str, data: dict):
    """Several contacts match the NEW guest's name — let the user pick."""
    contacts = data.get("contacts") or []
    await set_conversation_state(
        wa_id, "awaiting_add_attendee_contact",
        _add_attendee_ctx(data, contacts=contacts),
    )
    rows = [
        {
            "id": f"add_attendee_contact_{i}",
            "title": (c.get("name") or "Unknown")[:24],
            "description": (c.get("email") or "No Gmail on this contact")[:72],
        }
        for i, c in enumerate(contacts[:10])
    ]
    if rows:
        await send_list_async(
            wa_id,
            data.get("message") or "which one should i add?",
            "Pick contact",
            [{"title": "Matching contacts", "rows": rows}],
        )
    else:
        await send_message_async(wa_id, data.get("message") or "send that person's gmail address.")


async def send_add_attendee_event_picker(wa_id: str, data: dict):
    """Ambiguous which existing event — never guess, always ask."""
    events = data.get("events") or []
    await set_conversation_state(
        wa_id, "awaiting_add_attendee_event",
        _add_attendee_ctx(data, events=events),
    )
    rows = [
        {
            "id": f"add_attendee_event_{i}",
            "title": (ev.get("label") or "Event")[:24],
            "description": (ev.get("label") or "")[:72],
        }
        for i, ev in enumerate(events[:10])
    ]
    if rows:
        await send_list_async(
            wa_id,
            data.get("message") or "which event should i add them to?",
            "Pick event",
            [{"title": "Upcoming events", "rows": rows}],
        )
    else:
        await send_message_async(wa_id, data.get("message") or "which event should i add them to?")


async def send_add_attendee_email_request(wa_id: str, data: dict):
    """We know the event, not the address. Ask for the email and NOTHING else."""
    await set_conversation_state(
        wa_id, "awaiting_add_attendee_email", _add_attendee_ctx(data),
    )
    await send_message_async(wa_id, data.get("message") or "send that person's gmail address.")


async def _run_add_attendee_from_context(wa_id: str, ctx: dict, confirmed: bool = False):
    """Re-run the tool with everything collected so far."""
    from bot.tools import calendar_add_attendee

    ad = ctx.get("add_attendee_data", {})
    result = await calendar_add_attendee(
        wa_id,
        query=ad.get("query", ""),
        attendee_name=ad.get("attendee_name", ""),
        attendee_email=ad.get("attendee_email", ""),
        _confirmed=confirmed,
    )
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        payload = {"action": "error", "message": str(result)}

    action = payload.get("action")
    if action == "awaiting_add_attendee_confirmation":
        await send_add_attendee_confirm_buttons(wa_id, payload)
        return
    if action == "pick_contact_for_add":
        await send_add_attendee_contact_picker(wa_id, payload)
        return
    if action == "choose_event_for_add":
        await send_add_attendee_event_picker(wa_id, payload)
        return
    if action in ("need_attendee_email", "need_attendee"):
        await send_add_attendee_email_request(wa_id, payload)
        return

    await clear_conversation_state(wa_id)
    await send_message_async(wa_id, payload.get("message") or "couldn't add them to that event.")


async def _handle_add_attendee_text_reply(wa_id: str, text: str, flow_state: str,
                                          ctx: dict) -> bool:
    """Typed replies during an add-attendee flow. True = handled."""
    ad = ctx.get("add_attendee_data", {})
    t = text.strip().lower()

    if t in ("cancel", "stop", "no", "nevermind", "never mind"):
        await clear_conversation_state(wa_id)
        await send_message_async(wa_id, "okay, dropped it.")
        return True

    if flow_state == "awaiting_add_attendee_confirmation":
        if t in ("yes", "y", "confirm", "ok", "okay", "yep", "add", "add them"):
            await _run_add_attendee_from_context(wa_id, ctx, confirmed=True)
            return True
        # Changed their mind or asked something else → drop the gate.
        await clear_conversation_state(wa_id)
        return False

    if flow_state in ("awaiting_add_attendee_email", "awaiting_add_attendee_contact"):
        if flow_state == "awaiting_add_attendee_contact" and t.isdigit():
            contacts = ctx.get("contacts") or []
            idx = int(t) - 1
            if 0 <= idx < len(contacts):
                await _handle_add_attendee_contact_choice(
                    wa_id, f"add_attendee_contact_{idx}",
                )
                return True
        name, email = _extract_email(text, ad.get("attendee_name", ""))
        if email:
            ad["attendee_name"] = name or ad.get("attendee_name", "")
            ad["attendee_email"] = email
            ctx["add_attendee_data"] = ad
            await _run_add_attendee_from_context(wa_id, ctx)
            return True
        await send_message_async(wa_id, "send a valid gmail, e.g. name@example.com.")
        return True

    if flow_state == "awaiting_add_attendee_event":
        if t.isdigit():
            events = ctx.get("events") or []
            idx = int(t) - 1
            if 0 <= idx < len(events):
                await _handle_add_attendee_event_choice(
                    wa_id, f"add_attendee_event_{idx}",
                )
                return True
        # Treat free text as a fresh event query rather than re-asking.
        ad["query"] = text.strip()
        ctx["add_attendee_data"] = ad
        await _run_add_attendee_from_context(wa_id, ctx)
        return True

    return False


async def _handle_add_attendee_confirm(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_add_attendee_confirmation":
        await send_message_async(wa_id, "this confirmation has expired. start again when ready.")
        return
    await _run_add_attendee_from_context(wa_id, ctx, confirmed=True)


async def _handle_add_attendee_contact_choice(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_add_attendee_contact":
        await send_message_async(wa_id, "this contact selection has expired.")
        return
    try:
        idx = int(data.replace("add_attendee_contact_", ""))
    except ValueError:
        await send_message_async(wa_id, "invalid contact selection.")
        return
    contacts = ctx.get("contacts") or []
    if idx >= len(contacts):
        await send_message_async(wa_id, "invalid contact selection.")
        return

    contact = contacts[idx]
    ad = ctx.get("add_attendee_data", {})
    ad["attendee_name"] = contact.get("name") or ad.get("attendee_name", "")
    ad["attendee_email"] = contact.get("email") or ""
    ad["attendee_phone"] = contact.get("phone") or ""
    ctx["add_attendee_data"] = ad

    if not ad["attendee_email"]:
        await set_conversation_state(wa_id, "awaiting_add_attendee_email", ctx)
        await send_message_async(wa_id, f"{ad['attendee_name']} has no gmail on file. send their gmail.")
        return
    await _run_add_attendee_from_context(wa_id, ctx)


async def _handle_add_attendee_event_choice(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_add_attendee_event":
        await send_message_async(wa_id, "this event selection has expired.")
        return
    try:
        idx = int(data.replace("add_attendee_event_", ""))
    except ValueError:
        await send_message_async(wa_id, "invalid event selection.")
        return
    events = ctx.get("events") or []
    if idx >= len(events):
        await send_message_async(wa_id, "invalid event selection.")
        return

    ad = ctx.get("add_attendee_data", {})
    # The label is what the matcher scores against, so a picked event
    # re-resolves to exactly one candidate on the next pass.
    ad["query"] = events[idx].get("label") or ad.get("query", "")
    ctx["add_attendee_data"] = ad
    await _run_add_attendee_from_context(wa_id, ctx)


# ═══════════════════════════════════════════════════════════════
# Post-meeting summaries — card, picker, and ticket actions
#
# The pull path for transcripts. `get_meeting_summaries` (tools.py) finds a
# stored summary; everything the user sees below is deterministic. The LLM
# never narrates a summary and never creates tickets from one — buttons do.
# ═══════════════════════════════════════════════════════════════

_MAX_TICKET_ITEMS = 5      # never create more than this many todos/reminders


def _mtg_ctx(data: dict) -> dict:
    """Conversation-state context for an active summary card."""
    return {"flow_kind": "mtg_summary", "mtg_data": data}


def _action_items_of(data: dict) -> list[dict]:
    items = [a for a in (data.get("action_items") or [])
             if isinstance(a, dict) and (a.get("task") or "").strip()]
    return items[:_MAX_TICKET_ITEMS]


def _render_summary_text(data: dict) -> str:
    """MeetingSummary payload → WhatsApp text (plain, no markdown tables)."""
    lines: list[str] = []
    title = (data.get("title") or "").strip()
    headline = (data.get("headline") or "").strip()
    if title and headline:
        lines.append(f"*{title}* — {headline}")
    elif title or headline:
        lines.append(f"*{title or headline}*")
    prov = data.get("provider_label") or ""
    when = data.get("when_label") or ""
    meta = " · ".join(x for x in (prov, when.split("— ", 1)[-1]) if x)
    if meta:
        lines.append(meta)
    if data.get("key_takeaways"):
        lines.append("\n*Takeaways*")
        lines += [f"• {t}" for t in data["key_takeaways"]]
    if data.get("decisions"):
        lines.append("\n*Decisions*")
        for d in data["decisions"]:
            who = f" — {d.get('decided_by')}" if d.get("decided_by") else ""
            lines.append(f"• {d.get('decision', '')}{who}")
    items = data.get("action_items") or []
    if items:
        lines.append("\n*Action items*")
        for a in items:
            owner = a.get("owner") or "unassigned"
            due = f" ({a['due']})" if a.get("due") else ""
            lines.append(f"• {a.get('task', '')} — {owner}{due}")
    if data.get("per_person"):
        lines.append("\n*Who said what*")
        for p in data["per_person"]:
            name = p.get("name") or p.get("display_name") or "?"
            lines.append(f"• {name}: {p.get('summary', '')}")
    return "\n".join(lines).strip() or "summary unavailable."


async def send_meeting_summary_card(wa_id: str, data: dict) -> bool:
    """Render one meeting summary + action buttons.

    Used by BOTH paths: the agent's pull path (via main.py's dispatcher) and
    the poller's push path (pipeline.py after a transcript lands). Returns
    True when the summary text went out.

    Never steals an active conversation slot: if the user is mid-booking,
    the summary is still delivered but the action buttons are skipped —
    their tap would otherwise clobber the booking state machine.
    """
    ok = await send_message_async(wa_id, _render_summary_text(data))

    items = _action_items_of(data)
    if not items:
        # Only clear OUR slot — never a booking flow the user is mid-way
        # through (the poller can deliver a summary at any moment).
        state, mctx = await get_conversation_state(wa_id)
        if not state or (mctx or {}).get("flow_kind") == "mtg_summary":
            await clear_conversation_state(wa_id)
        return bool(ok)

    state, _ = await get_conversation_state(wa_id)
    if not state:
        await set_conversation_state(wa_id, "awaiting_mtg_action", _mtg_ctx(data))
        await send_buttons_async(
            wa_id,
            f"{len(items)} action item(s) above — want them as todos or reminders?",
            [
                {"id": "mtg_todos_add", "title": "Add as todos"},
                {"id": "mtg_remind_set", "title": "Set reminders"},
            ],
        )
    return bool(ok)


async def send_meeting_summary_picker(wa_id: str, data: dict):
    """Several meetings match — list them, never guess."""
    summaries = [s for s in (data.get("summaries") or [])
                 if isinstance(s, dict)][:10]
    await set_conversation_state(wa_id, "awaiting_mtg_pick", {
        "flow_kind": "mtg_summary",
        "summaries": summaries,
    })
    rows = [
        {
            "id": f"mtgsummary_{i}",
            "title": (s.get("title") or s.get("headline")
                      or "Meeting")[:24],
            "description": (s.get("when_label")
                            or s.get("headline") or "")[:72],
        }
        for i, s in enumerate(summaries)
    ]
    if rows:
        await send_list_async(
            wa_id,
            data.get("message") or "which meeting do you mean?",
            "Pick meeting",
            [{"title": "Recent meetings", "rows": rows}],
        )
    else:
        await send_message_async(
            wa_id, data.get("message") or "which meeting do you mean?",
        )


async def _handle_mtgsummary_choice(wa_id: str, reply_id: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_mtg_pick":
        await send_message_async(wa_id, "this selection has expired.")
        return
    try:
        idx = int(reply_id.replace("mtgsummary_", ""))
    except ValueError:
        await send_message_async(wa_id, "invalid selection.")
        return
    summaries = ctx.get("summaries") or []
    if idx >= len(summaries):
        await send_message_async(wa_id, "invalid selection.")
        return
    chosen = summaries[idx]
    await clear_conversation_state(wa_id)
    await send_meeting_summary_card(wa_id, chosen)


async def _handle_mtg_todos_add(wa_id: str):
    """Turn the card's action items into real to-do rows (one sheet append)."""
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_mtg_action":
        await send_message_async(wa_id, "this action has expired.")
        return
    items = _action_items_of(ctx.get("mtg_data") or {})
    await clear_conversation_state(wa_id)

    from bot.database import User
    from bot.services.sheets import add_todo_to_sheet
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
    if not u or not u.google_token_json:
        await send_message_async(wa_id, "connect your google account first — "
                                 "tap connect above or type 'connect'.")
        return

    tasks = [(a.get("task") or "").strip() for a in items]
    result = add_todo_to_sheet(u, tasks)
    if hasattr(result, "__await__"):
        result = await result
    if "error" in result:
        await send_message_async(wa_id, "couldn't reach your to-do sheet — "
                                 "try again in a bit.")
        return
    added = result.get("added", len(tasks))
    listing = "\n".join(f"• {t}" for t in tasks)
    await send_message_async(
        wa_id, f"done — added {added} to your to-do list:\n{listing}",
    )


async def _handle_mtg_remind_set(wa_id: str):
    """Reminders need a TIME from the user — same anti-hallucination rule
    as every other reminder. Ask; never invent one from the transcript."""
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_mtg_action":
        await send_message_async(wa_id, "this action has expired.")
        return
    n = len(_action_items_of(ctx.get("mtg_data") or {}))
    await set_conversation_state(wa_id, "awaiting_mtg_reminder_time", ctx)
    await send_message_async(
        wa_id,
        f"sure — what time should i remind you about "
        f"{'that item' if n == 1 else f'all {n} items'}? (e.g. tomorrow 9 am)",
    )


async def _handle_mtg_summary_text_reply(wa_id: str, text: str,
                                         flow_state: str, ctx: dict) -> bool:
    """Typed replies during the summary flow. True = handled."""
    t = text.strip().lower()

    if flow_state == "awaiting_mtg_action":
        if t in ("cancel", "stop", "no", "close", "dismiss",
                 "nevermind", "never mind"):
            await clear_conversation_state(wa_id)
            return True
        if any(k in t for k in ("todo", "to-do", "task")):
            await _handle_mtg_todos_add(wa_id)
            return True
        if any(k in t for k in ("remind", "reminder")):
            await _handle_mtg_remind_set(wa_id)
            return True
        # Anything else leaves the gate open for the agent to handle normally.
        await clear_conversation_state(wa_id)
        return False

    if flow_state == "awaiting_mtg_reminder_time":
        if t in ("cancel", "stop", "no", "nevermind", "never mind"):
            await clear_conversation_state(wa_id)
            await send_message_async(wa_id, "okay, skipped the reminders.")
            return True
        parsed = await parse_datetime_from_text(text)
        iso = (parsed or {}).get("date_iso") or ""
        if not iso:
            await send_message_async(
                wa_id, "i need a clock time — e.g. 'tomorrow 9 am' or 'today 6 pm'.",
            )
            return True

        data = ctx.get("mtg_data") or {}
        title = (data.get("title") or data.get("headline") or "").strip()
        items = _action_items_of(data)
        from bot.tools import set_reminder
        created, failed = [], []
        for a in items:
            msg_text = a.get("task", "").strip()
            if title:
                msg_text = f"{msg_text} (from {title})"
            result = await set_reminder(
                wa_id, message=msg_text, remind_at_iso=iso, user_text=text,
            )
            low = result.lower()
            if low.startswith("reminder #") or "daily reminder #" in low:
                created.append(a.get("task", ""))
            else:
                failed.append(result if not low.startswith("time_required")
                              else "TIME_REQUIRED")

        await clear_conversation_state(wa_id)
        if "TIME_REQUIRED" in failed:
            await set_conversation_state(
                wa_id, "awaiting_mtg_reminder_time", ctx)
            await send_message_async(
                wa_id, "that needs a clock time (like '9 am') so i don't "
                "guess — what time works?")
            return True
        if created:
            listing = "\n".join(f"• {c}" for c in created)
            extra = (f"\n{len(failed)} couldn't be set." if failed else "")
            await send_message_async(
                wa_id,
                f"done — {len(created)} reminder"
                f"{'s' if len(created) != 1 else ''} set:\n{listing}{extra}",
            )
        else:
            await send_message_async(
                wa_id, "couldn't set those reminders — try again?")
        return True

    return False


# ═══════════════════════════════════════════════════════════════
# Conflict Resolution
# ═══════════════════════════════════════════════════════════════

async def _handle_conflict(wa_id: str, data: str):
    flow_state, ctx = await get_conversation_state(wa_id)
    if flow_state != "awaiting_conflict_resolution":
        await send_message_async(wa_id, "this action has expired.")
        return

    action = data.replace("conflict_", "")

    # Only the gmeet flow raises a conflict prompt. "Keep both" proceeds to
    # the confirmation gate (skip the re-check); "Change time" hands the next
    # message back to the model to re-propose at a new time.
    if action == "keep_both":
        await _run_gmeet_from_context(wa_id, ctx, skip_conflict_check=True)
    elif action in ("move_later", "move_earlier", "reschedule_existing"):
        await set_conversation_state(wa_id, "awaiting_gmeet_time", ctx)
        await send_message_async(
            wa_id,
            "sure — what new date and time should i use for this google meet? "
            "(e.g. tomorrow 2 pm)",
        )


# ═══════════════════════════════════════════════════════════════
# Cancel / Onboarding
# ═══════════════════════════════════════════════════════════════

async def _handle_cancel(wa_id: str):
    _, ctx = await get_conversation_state(wa_id)
    task_id = ctx.get("task_data", {}).get("task_id")
    if task_id:
        await cancel_task(task_id)
    await clear_conversation_state(wa_id)
    await send_message_async(wa_id, "cancelled.")


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

async def send_connect_button(wa_id: str, message: str):
    """Send a 'Connect Google' button with a context message."""
    await send_buttons_async(wa_id, message, [
        {"id": "connect_google",    "title": "Connect Google"},
        {"id": "show_capabilities", "title": "What can I do?"},
    ])


async def _send_capabilities(wa_id: str):
    """Send a capabilities overview."""
    await send_message_async(
        wa_id,
        "here's what i can do:\n\n"
        "*calendar* — create, view, update and delete events\n"
        "*google meet* — schedule meetings with meet links\n"
        "*gmail* — answer questions about your inbox\n"
        "*tasks* — track commitments with reminders\n"
        "*notes* — save and retrieve quick notes\n"
        "*contacts* — search your google contacts\n"
        "*reminders* — one-time and daily recurring\n"
        "*voice notes* — send voice, i'll respond\n\n"
        "just tell me what you need in plain language.",
    )
