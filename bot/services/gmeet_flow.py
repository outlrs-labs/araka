"""Google Meet scheduling — explicit state-machine functions.

Matches PRD §A.2 verbatim. The LLM is OUT of the loop after it produces
the initial `attendee_name` + `start_time_iso`. From there on, three
deterministic async functions drive the flow:

    resolve_attendee()        Step 1 — name → email
    propose_meeting()         Step 2 — conflict check + confirmation gate
    create_event_and_notify() Step 3 — only after explicit user "Yes"

Shortcut: when the caller passes `Name{email@x.com}`, step 1 is skipped
entirely.

Why split this out of bot/tools.py
-----------------------------------
The previous monolithic `set_gmeet` had three intertwined bugs (title
duplication, fuzzy-match auto-resolve, LLM leakage). Splitting each
phase into a single-responsibility function makes each one separately
testable and removes the room for entity confusion across phases.

Return shape
------------
Every function returns a plain `dict` with at minimum an `action` key.
`bot.tools.set_gmeet` JSON-encodes the dict for the LLM tool protocol,
but callers from `callback_handler` can use the dict directly.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from bot.services.calendar import create_event, find_conflicts
from bot.services.contacts import search_contacts
from bot.utils.time import ensure_aware_local

logger = logging.getLogger(__name__)

_NAME_EMAIL_RX = re.compile(r"^(.+?)\{([^{}\s]+@[^{}\s]+\.[^{}\s]+)\}$")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def parse_name_email_shortcut(attendee_name: str, attendee_email: str = "") -> tuple[str, str]:
    """Detect the `Name{email}` shortcut and split out the parts.

    Returns (name, email). If `attendee_email` was already provided, it
    wins. If the shortcut isn't present, returns (attendee_name, "").
    """
    if attendee_email:
        return attendee_name.strip(), attendee_email.strip()
    m = _NAME_EMAIL_RX.match((attendee_name or "").strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (attendee_name or "").strip(), ""


def is_strong_match(query: str, contact_name: str) -> bool:
    """True only when the query is the contact's EXACT first name or full name.

    Substring matches like ``query="Priya"`` against ``contact="Priyanshu 64"``
    are rejected — the picker is shown instead. This eliminates the
    silent wrong-person auto-resolve seen in production.
    """
    q = (query or "").lower().strip()
    n = (contact_name or "").lower().strip()
    if not q or not n:
        return False
    if q == n:
        return True
    first = n.split()[0] if n else ""
    return q == first


def _display_name(name: str, email: str = "") -> str:
    """Never let a raw email address become the attendee's display name.

    A booking that stored `assignee_name="adhiraj.arora@scaler.com"` produced a
    second event titled with the address instead of the person, so the same
    person ended up with two differently-titled events — and the reschedule
    matcher could not tell them apart. Derive a readable name from the address
    instead: "adhiraj.arora@scaler.com" -> "Adhiraj Arora".
    """
    n = (name or "").strip()
    if n and "@" not in n:
        return n
    local = (n if "@" in n else (email or "")).split("@", 1)[0]
    parts = [p for p in re.split(r"[._+-]+", local) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts) or n


def _sanitize_title(llm_title: str, attendee_name: str) -> str:
    """Produce a clean event title that won't duplicate "with X".

    Rules:
      - If LLM gave nothing → "Meeting with {attendee_name}"
      - If LLM gave a title that already contains " with " → discard it,
        fall back to the default (the LLM was trying to template-paste
        and corrupted itself, as in the "Priya with Priyanshu 64" bug).
      - If LLM gave a clean topic that does NOT contain the attendee name
        → "{topic} with {attendee_name}".
      - Otherwise → use the LLM title verbatim (it already has context).
    """
    t = (llm_title or "").strip()
    default = f"Meeting with {attendee_name}"
    if not t:
        return default

    # "pricing with priya" used to be thrown away entirely, losing the topic
    # the user actually typed. Strip the trailing "with <name>" and keep the
    # topic instead — discard only if nothing meaningful is left.
    tl = t.lower()
    if " with " in tl:
        head = t[:tl.index(" with ")].strip(" -–—:,")
        if not head or head.lower() == "meeting":
            return default
        t, tl = head, head.lower()

    if attendee_name and attendee_name.lower() in tl:
        return t                                     # LLM already wrote in name
    return f"{t} with {attendee_name}"


# ═══════════════════════════════════════════════════════════════
# Step 1 — Resolve attendee
# ═══════════════════════════════════════════════════════════════

async def resolve_attendee(db_user, query: str) -> dict:
    """Look up `query` in the user's Google Contacts.

    Returns one of:
      {"action": "resolved",      "name", "email", "phone"}
      {"action": "pick_contact",  "contacts": [{index,name,email,phone,display}]}
      {"action": "need_email",    "name", "phone"}
      {"action": "no_contact",    "name"}
      {"action": "error",         "message"}

    Auto-resolves ONLY when there is exactly one contact with an email AND
    the query is an exact first-name (or full-name) match.
    """
    if not query:
        return {"action": "no_contact", "name": query}

    try:
        contacts = await search_contacts(db_user, query=query, limit=10)
    except Exception as e:
        return {"action": "error", "message": f"Contact search failed: {e}"}

    if not contacts:
        return {"action": "no_contact", "name": query}

    with_email = [c for c in contacts if c.get("emails")]
    without_email = [c for c in contacts if not c.get("emails")]

    # Auto-resolve path: exactly one email contact AND strong name match
    if len(with_email) == 1 and not without_email:
        cand = with_email[0]
        if is_strong_match(query, cand["name"]):
            return {
                "action": "resolved",
                "name":  cand["name"],
                "email": cand["emails"][0],
                "phone": (cand.get("phones") or [""])[0],
            }
        # Single match but name not strong → demote to picker so the user
        # confirms. This is the "Priya → Priyanshu 64" guard.
        logger.info(
            f"Single contact match for {query!r} is weak ('{cand['name']}'); "
            f"showing picker"
        )

    # Single contact with NO email → ask for it
    if len(with_email) == 0 and len(contacts) == 1:
        c = contacts[0]
        return {
            "action": "need_email",
            "name":  c["name"],
            "phone": (c.get("phones") or [""])[0],
        }

    # Multiple matches → picker
    picker_rows = []
    for i, c in enumerate(contacts):
        emails = ", ".join(c["emails"]) if c.get("emails") else "no email"
        picker_rows.append({
            "index": i + 1,
            "name":  c["name"],
            "email": (c.get("emails") or [None])[0],
            "phone": (c.get("phones") or [None])[0],
            "display": f"{i + 1}. {c['name']} ({emails})",
        })

    return {
        "action": "pick_contact",
        "contacts": picker_rows,
        "query": query,
    }


# ═══════════════════════════════════════════════════════════════
# Step 2 — Conflict check + confirmation gate
# ═══════════════════════════════════════════════════════════════

async def propose_meeting(
    db_user,
    name: str,
    email: str,
    phone: str,
    start_time_iso: str,
    duration_minutes: int = 30,
    llm_title: str = "",
    skip_conflict_check: bool = False,
) -> dict:
    """Validate time, check conflicts, return a confirmation proposal.

    Returns one of:
      {"action": "awaiting_confirmation", ...full payload incl. message}
      {"action": "missing_time", "name", "email", "phone", ...}
      {"action": "conflict",     "conflicts": [...], ...}
      {"action": "error",        "message"}
    """
    if not start_time_iso:
        return {
            "action": "missing_time",
            "name":  name, "email": email, "phone": phone,
            "duration_minutes": duration_minutes,
            "message": "when should this meeting be? send the date and time.",
        }

    try:
        dt = ensure_aware_local(datetime.fromisoformat(start_time_iso))
    except (TypeError, ValueError):
        return {
            "action": "missing_time",
            "name":  name, "email": email, "phone": phone,
            "duration_minutes": duration_minutes,
            "message": "i have the person, but not a valid meeting time. send the date and time.",
        }

    conflicts: list = []
    if not skip_conflict_check:
        from bot.services.task_service import (
            _check_task_table_conflicts, conflicts_for_assignee_phone,
        )
        # Creator side — Google Calendar.
        try:
            conflicts = await find_conflicts(db_user, dt, duration_minutes) or []
        except Exception as e:
            logger.warning(f"Google conflict check failed (proceeding): {e}")
            conflicts = []
        # Creator side — the bot's OWN meetings (FR-5). This is the reliable
        # path: it doesn't depend on Google Calendar having synced yet.
        try:
            conflicts.extend(
                await _check_task_table_conflicts(db_user.wa_id, dt, duration_minutes)
            )
        except Exception as e:
            logger.warning(f"Creator task conflict check failed: {e}")
        # FR-5 bilateral — the assignee's existing bot meetings (repeat assignees).
        if phone:
            try:
                ac = await conflicts_for_assignee_phone(phone, dt, duration_minutes)
                for c in ac:
                    c["summary"] = f"{c.get('summary', 'a meeting')} (with {name})"
                conflicts.extend(ac)
            except Exception as e:
                logger.warning(f"Assignee conflict check failed: {e}")
        # Dedupe — a gmeet booking exists as BOTH a Google event and a Task
        # row, so the two checks above can report the same meeting twice.
        seen, deduped = set(), []
        for c in conflicts:
            key = (str(c.get("summary", "")).strip().lower(), str(c.get("start", ""))[:16])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        conflicts = deduped

    if conflicts:
        lines = []
        for c in conflicts:
            try:
                cs = datetime.fromisoformat(c["start"])
                ce = datetime.fromisoformat(c["end"])
                lines.append(
                    f'"{c["summary"]}" on {cs.strftime("%a %b %d, %I:%M")}'
                    f'–{ce.strftime("%I:%M %p")}'
                )
            except Exception:
                lines.append(f'"{c.get("summary", "Untitled")}"')
        return {
            "action": "conflict",
            "name": name, "email": email, "phone": phone,
            "title": llm_title,
            "start_time_iso": start_time_iso,
            "duration_minutes": duration_minutes,
            "conflicts": [
                {"summary": c["summary"], "start": c["start"], "end": c["end"]}
                for c in conflicts
            ],
            "message": (
                f"conflict at {dt.strftime('%b %d, %I:%M %p')}: "
                f"you already have {'; '.join(lines)}. "
                f"pick a different time or keep both?"
            ),
        }

    # No conflict → present the confirmation proposal
    end_dt = dt + timedelta(minutes=duration_minutes)
    event_title = _sanitize_title(llm_title, name)

    return {
        "action": "awaiting_confirmation",
        "name":  name,
        "email": email,
        "phone": phone,
        "title": event_title,
        "start_time_iso": start_time_iso,
        "duration_minutes": duration_minutes,
        "start_time": dt.strftime("%a %b %d, %I:%M %p IST"),
        "end_time":   end_dt.strftime("%I:%M %p IST"),
        # No reminder promise: automatic T-24h/T-1h meeting reminders were
        # removed, so claiming them here would be a lie the user only
        # discovers when the reminder never arrives.
        "message": (
            f"confirm — {event_title}, "
            f"{dt.strftime('%a %b %d, %-I:%M %p')} IST, google meet. save?"
        ),
    }


# ═══════════════════════════════════════════════════════════════
# Step 3 — Create the Calendar + Meet event (post-confirmation)
# ═══════════════════════════════════════════════════════════════

async def create_event_and_notify(
    db_user,
    wa_id: str,
    name: str,
    email: str,
    phone: str,
    start_time_iso: str,
    duration_minutes: int = 30,
    llm_title: str = "",
) -> dict:
    """Create the Google Calendar event with a Meet link.

    Called only after the user taps "Confirm". Optionally notifies
    the assignee via WhatsApp template if their phone is on file.
    """
    try:
        dt = ensure_aware_local(datetime.fromisoformat(start_time_iso))
    except (TypeError, ValueError) as e:
        return {"action": "error", "message": f"Bad start_time_iso: {e}"}

    # Normalise before it reaches the title AND the Task row — both used to
    # inherit a raw email here.
    name = _display_name(name, email)
    event_title = _sanitize_title(llm_title, name)
    attendees = [email] if email else None

    try:
        info = await create_event(
            db_user, event_title, dt,
            duration_minutes=duration_minutes,
            meet_link=True, attendees=attendees,
        )
    except Exception as e:
        return {"action": "error", "message": f"Failed to create event: {e}"}

    end_dt = dt + timedelta(minutes=duration_minutes)
    meet_link = info.get("meet", "")
    time_str = f"{dt.strftime('%a %b %d, %I:%M %p')} to {end_dt.strftime('%I:%M %p')} IST"

    # ── FR-8: validate the attendee phone to E.164 + create assignee User ──
    from bot.services.phone import normalize_e164, to_wa_id
    from bot.services.task_service import (
        upsert_assignee_user, is_assignee_reachable, record_meeting_task,
    )

    e164 = normalize_e164(phone) if phone else None
    assignee_id = None
    if e164:
        try:
            assignee_id = await upsert_assignee_user(e164, name)
        except Exception as e:
            logger.warning(f"Assignee user upsert failed: {e}")

    # ── FR-2/FR-7: record a first-class Task for this meeting ─────────────
    # Without this, the booked meeting has no Task row, so reminders,
    # completion checks and conflict detection would never see it.
    # Deterministic per-field confidence (PRD §FR-3). We only reach Step 3
    # after the user confirmed a valid time, so date_time is certain.
    confidence = {
        "assignee": 0.95 if email else 0.6,
        "title": 0.9 if llm_title else 0.5,
        "date_time": 1.0,
        "duration": 0.6,
        "mode": 1.0,
    }

    task_id = None
    try:
        task_id = await record_meeting_task(
            creator_wa_id=wa_id,
            title=event_title,
            assignee_name=name,
            assignee_phone_e164=e164,
            assignee_id=assignee_id,
            scheduled_local_dt=dt,
            duration_minutes=duration_minutes,
            meeting_link=meet_link,
            external_event_id=info.get("id", ""),
            timezone_name=getattr(db_user, "timezone", None) or "Asia/Kolkata",
            confidence_scores=confidence,
        )
    except Exception as e:
        logger.warning(f"Could not record meeting Task: {e}")

    # ── FR-8: template-only first contact, opt-out aware ──────────────────
    notified = False
    reachable = await is_assignee_reachable(assignee_id) if e164 else True
    if e164 and reachable:
        try:
            from bot.services.whatsapp import send_meeting_notification
            booker = getattr(db_user, "display_name", None) or wa_id
            notified = send_meeting_notification(
                attendee_phone=to_wa_id(e164) or e164,
                booker_name=booker,
                meeting_title=event_title,
                meeting_time=time_str,
                meet_link=meet_link,
            )
        except Exception as e:
            logger.warning(f"Attendee notification failed: {e}")

    if notified:
        suffix = f"\n\n{name} has been notified on whatsapp."
    elif e164 and not reachable:
        suffix = f"\n\n{name} has opted out, so i didn't message them."
    elif phone and not e164:
        suffix = (
            f"\n\n{name}'s phone number isn't a valid format, "
            f"so i couldn't notify them on whatsapp."
        )
    elif e164:
        suffix = (
            f"\n\ncouldn't notify {name} on whatsapp "
            f"(their number may not be in the bot's test list)."
        )
    else:
        suffix = ""

    return {
        "action": "created",
        "task_id": task_id,
        "title": event_title,
        "name":  name,
        "email": email,
        "phone": e164 or phone,
        "start_time": dt.strftime("%a %b %d, %I:%M %p IST"),
        "end_time":   end_dt.strftime("%I:%M %p IST"),
        "meet_link":     meet_link,
        "calendar_link": info.get("link", ""),
        "event_id":      info.get("id", ""),
        "message": (
            f"saved. {event_title}.\n"
            f"{time_str}\n"
            f"meet: {meet_link or 'N/A'}"
            f"{suffix}"
        ),
        "attendee_notified": notified,
    }
