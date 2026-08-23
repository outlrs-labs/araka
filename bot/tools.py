"""FollowUp Bot — Tool functions for the AI Agent.

Each tool wraps a DB or Google API operation.
The agent calls these via function calling.
User identifier is wa_id (WhatsApp phone number string).
"""

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from bot.database import async_session, User, Note, Reminder, Task, MeetingSummary
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
                          attendees_csv: str = "", user_text: str = "") -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED

    if user_text and not has_explicit_clock_time(user_text):
        return (
            "TIME_REQUIRED: I need a clock time before creating that event. "
            "Ask the user for a time such as 3 PM."
        )

    try:
        dt = ensure_aware_local(datetime.fromisoformat(start_time_iso))
    except (TypeError, ValueError):
        return "TIME_REQUIRED: I couldn't read that event time. Ask the user for a clear date and clock time."

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
                f"CONFLICT DETECTED: The time slot {dt.strftime('%b %d, %I:%M %p')} "
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


_CALENDAR_CANCEL_STOPWORDS = {
    "a", "an", "and", "at", "calendar", "cancel", "cancell", "cancelled",
    "delete", "event", "for", "from", "in", "meeting", "meet", "my", "of",
    "on", "please", "remove", "the", "to", "with",
}


def _event_start_local(ev: dict):
    start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
    if not start:
        return None
    try:
        raw = start.replace("Z", "+00:00")
        if "T" not in raw:
            raw = f"{raw}T00:00:00"
        return ensure_aware_local(datetime.fromisoformat(raw))
    except Exception:
        return None


# Ask instead of guessing when the runner-up scores within this margin.
#
# The old check was `candidates[0][0] == candidates[1][0]` — an EXACT tie. Two
# events differing by one incidental token ("… with Adhiraj Arora" vs "… with
# adhiraj.arora@scaler.com") score 4 vs 3, which is not a tie, so the top one
# was taken silently and the wrong event got rescheduled. Near-ambiguity is the
# case that actually happens; perfect ties are rare.
_MATCH_AMBIGUITY_MARGIN = 1


def _is_ambiguous_match(candidates: list) -> bool:
    """True when the top two candidates are too close to choose between."""
    return (
        len(candidates) > 1
        and (candidates[0][0] - candidates[1][0]) <= _MATCH_AMBIGUITY_MARGIN
    )


def _event_time_search_text(ev: dict) -> str:
    """Spoken forms of the event's start time, for matching.

    When two events share a title ("Araka Feedback - 1 with …"), the start time
    is the ONLY thing that separates them — and it used to be absent from the
    search text entirely, so "the 5 pm one" carried no signal and the matcher
    picked whichever scored higher on the shared words. Render the several ways
    a user might say it so any of them scores.
    """
    start = _event_start_local(ev)
    if not start:
        return ""
    hour12 = start.strftime("%I").lstrip("0") or "12"
    minute = start.strftime("%M")
    ampm = start.strftime("%p").lower()
    forms = [
        start.strftime("%a %A %b %B"),
        str(int(start.strftime("%d"))),
        f"{hour12}:{minute}{ampm}", f"{hour12}:{minute} {ampm}",
        f"{hour12}:{minute}", start.strftime("%H:%M"),
    ]
    if minute == "00":
        # Only an on-the-hour event answers to "5pm". Emitting it for 5:30 too
        # would make "the 5 pm one" match both events again.
        forms += [f"{hour12}{ampm}", f"{hour12} {ampm}"]
    return " ".join(forms).lower()


def _event_content_text(ev: dict) -> str:
    attendees = " ".join(a.get("email", "") for a in ev.get("attendees", []) or [])
    return " ".join([
        ev.get("summary", "") or "",
        ev.get("description", "") or "",
        attendees,
    ]).lower()


def _event_search_text(ev: dict) -> str:
    return f"{_event_content_text(ev)} {_event_time_search_text(ev)}".strip()


# A token matching the event's TIME outweighs one matching a shared title word.
# When two events differ only by start time, the time is the entire signal —
# with flat scoring, "the 5 pm one" still left the two "Araka Feedback - 1"
# events inside the ambiguity margin, so the bot asked (or guessed) anyway.
_TIME_TOKEN_WEIGHT = 2


def _score_event(tokens: list, ev: dict) -> int:
    """Weighted overlap between query tokens and one event."""
    content = _event_content_text(ev)
    when = _event_time_search_text(ev)
    total = 0
    for token in tokens:
        if token in content:
            total += 1
        elif token in when:
            total += _TIME_TOKEN_WEIGHT
    return total


def _calendar_cancel_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9@._+-]+", (query or "").lower())
    return [t for t in tokens if t not in _CALENDAR_CANCEL_STOPWORDS and len(t) > 1]


def _format_calendar_event(ev: dict) -> str:
    start = _event_start_local(ev)
    when = start.strftime("%a %b %d, %I:%M %p") if start else "time unknown"
    return f"{ev.get('summary', 'Untitled')} | {when}"


async def _mark_event_task_cancelled(wa_id: str, event_id: str) -> None:
    """Mark any bot Task linked to this calendar event as cancelled."""
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(
                Task.wa_id == wa_id,
                Task.external_event_id == event_id,
                Task.status.notin_(["completed", "cancelled"]),
            )
        )
        changed = False
        for task in result.scalars().all():
            task.status = "cancelled"
            task.updated_at = utcnow_naive()
            changed = True
        if changed:
            await session.commit()


async def calendar_cancel(wa_id: str, query: str = "", start_time_iso: str = "",
                          cancel_all: bool = False, limit: int = 50,
                          user_text: str = "") -> str:
    """Cancel upcoming Google Calendar event(s) and delete them from Calendar.

    Three modes:
      - cancel_all=True (or the query says all/everything/both) → delete every
        upcoming event, or just today's if the query mentions "today".
      - a clear single match (by person/title/time) → delete that one.
      - a vague request with several events → LIST them so the user can choose.
    Any linked bot Task is marked cancelled too.
    """
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED

    # A model-supplied timestamp may narrow the deletion target. Ignore it
    # unless the user themselves wrote a clock time; matching by title/person
    # remains available for a genuinely time-free cancellation request.
    if start_time_iso and user_text and not has_explicit_clock_time(user_text):
        logger.warning("Ignored ungrounded calendar-cancel time for wa_id=%s", wa_id)
        start_time_iso = ""

    try:
        events = await get_all_events(db_user, max_results=max(10, min(int(limit or 50), 100)))
    except Exception as e:
        return f"Failed to fetch events: {e}"
    if not events:
        return "NO_EVENTS: There are no upcoming events to cancel."

    ql = (query or "").lower()
    now_l = now_local()

    def _is_today(ev):
        s = _event_start_local(ev)
        return bool(s and s.date() == now_l.date())

    # ── Cancel-all (optionally just today) ──
    if cancel_all or re.search(r"\b(all|everything|every meeting|both)\b", ql):
        wants_today = "today" in ql
        targets = [e for e in events if (not wants_today or _is_today(e))]
        if not targets:
            return "NO_EVENTS: No matching events to cancel."
        deleted = []
        for ev in targets:
            eid = ev.get("id")
            if not eid:
                continue
            try:
                await delete_event(db_user, eid)
                await _mark_event_task_cancelled(wa_id, eid)
                deleted.append(_format_calendar_event(ev))
            except Exception as e:
                logger.warning(f"cancel-all: failed to delete {eid}: {e}")
        if not deleted:
            return "Failed to cancel the events. Please try again."
        return (f"CALENDAR_EVENTS_CANCELLED: Deleted {len(deleted)} event(s):\n- "
                + "\n- ".join(deleted))

    tokens = _calendar_cancel_tokens(query)
    target = None
    target_has_time = False
    if start_time_iso:
        try:
            target = ensure_aware_local(datetime.fromisoformat(start_time_iso.replace("Z", "+00:00")))
            target_has_time = bool(target.hour or target.minute)
        except Exception:
            target = None

    # ── Vague (no usable words, no time) → show the list to pick from ──
    if not tokens and not target:
        lines = ["CHOOSE_EVENT: Which one should I cancel? Reply with the title or time, or say 'all'."]
        for ev in events[:10]:
            lines.append(f"- {_format_calendar_event(ev)}")
        return "\n".join(lines)

    # ── Scored match ──
    candidates = []
    for ev in events:
        score = 0
        if tokens:
            score = _score_event(tokens, ev)
            if score == 0:
                continue
        ev_start = _event_start_local(ev)
        if target:
            if not ev_start or ev_start.date() != target.date():
                continue
            if target_has_time:
                delta_min = abs((ev_start - target).total_seconds()) / 60
                if delta_min > 120:
                    continue
                score += max(1, 8 - int(delta_min // 15))
            else:
                score += 2
        if score > 0:
            candidates.append((score, ev))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return f"NO_CALENDAR_MATCH: I could not find an upcoming event matching {query!r}."

    # Ambiguous only when the top two tie — otherwise take the clear winner.
    if _is_ambiguous_match(candidates):
        lines = ["MULTIPLE_CALENDAR_MATCHES: More than one event matches — which "
                 "one? Reply with the time, or say 'all':"]
        for _, ev in candidates[:5]:
            lines.append(f"- {_format_calendar_event(ev)}")
        return "\n".join(lines)

    ev = candidates[0][1]
    event_id = ev.get("id")
    if not event_id:
        return "Failed to delete event: Google did not return an event id."
    try:
        await delete_event(db_user, event_id)
    except Exception as e:
        return f"Failed to delete event: {e}"
    await _mark_event_task_cancelled(wa_id, event_id)
    return f"CALENDAR_EVENT_CANCELLED: Deleted {_format_calendar_event(ev)}."


def _event_duration_minutes(ev: dict, default: int = 30) -> int:
    """Return an event duration without trusting model-supplied defaults."""
    start = _event_start_local(ev)
    end_raw = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
    if not start or not end_raw:
        return default
    try:
        end = ensure_aware_local(datetime.fromisoformat(end_raw.replace("Z", "+00:00")))
        minutes = int((end - start).total_seconds() // 60)
        return minutes if minutes > 0 else default
    except (TypeError, ValueError):
        return default


async def _update_linked_task_schedule(wa_id: str, event_id: str, start_local: datetime,
                                       duration_minutes: int) -> None:
    """Keep a bot-managed task aligned when its Google event is moved."""
    async with async_session() as session:
        result = await session.execute(select(Task).where(
            Task.wa_id == wa_id,
            Task.external_event_id == event_id,
            Task.status.notin_(["completed", "cancelled"]),
        ))
        changed = False
        for task in result.scalars().all():
            task.scheduled_at = to_utc_naive(start_local)
            task.duration_minutes = duration_minutes
            task.updated_at = utcnow_naive()
            changed = True
        if changed:
            await session.commit()


async def calendar_reschedule(wa_id: str, query: str, new_start_time_iso: str,
                              duration_minutes: int = 0, user_text: str = "",
                              _confirmed: bool = False) -> str:
    """Propose, then deterministically confirm, a Calendar event time update.

    This replaces the unsafe cancel-then-create sequence. The model may only
    propose a target; a WhatsApp confirmation button is required before the
    update call reaches Google Calendar.
    """
    db_user = await _get_user(wa_id)
    if not db_user:
        return json.dumps({"action": "error", "message": "User not found."})
    if not db_user.google_token_json:
        return NOT_CONNECTED
    # Fail CLOSED on the unconfirmed path. The old `user_text and ...` clause
    # short-circuited whenever user_text was empty, so a caller that supplied no
    # text skipped the anti-hallucination check entirely and the model's chosen
    # time was accepted unverified. Only the deterministic confirm path
    # (_confirmed=True) may move an event without fresh user-typed time.
    if not new_start_time_iso or (not _confirmed and not has_explicit_clock_time(user_text)):
        return json.dumps({
            "action": "missing_time",
            "message": "Please send the new date and a clock time, for example tomorrow 3 PM.",
        })
    try:
        new_start = ensure_aware_local(
            datetime.fromisoformat(new_start_time_iso.replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return json.dumps({
            "action": "missing_time",
            "message": "I couldn't read the new time. Please send a clear date and clock time.",
        })

    try:
        events = await get_all_events(db_user, max_results=50)
    except Exception as e:
        return json.dumps({"action": "error", "message": f"Failed to fetch events: {e}"})
    if not events:
        return json.dumps({"action": "no_calendar_match", "message": "There are no upcoming events to reschedule."})

    tokens = _calendar_cancel_tokens(query)
    if not tokens:
        lines = ["Which event should I move? Send its title, attendee, or current time."]
        lines.extend(f"- {_format_calendar_event(ev)}" for ev in events[:10])
        return json.dumps({"action": "choose_event", "message": "\n".join(lines)})

    candidates = []
    for ev in events:
        score = _score_event(tokens, ev)
        if score:
            candidates.append((score, ev))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return json.dumps({
            "action": "no_calendar_match",
            "message": f"I couldn't find an upcoming event matching {query!r}.",
        })
    if _is_ambiguous_match(candidates):
        lines = ["More than one event matches. Which one should I move?"]
        lines.extend(f"- {_format_calendar_event(ev)}" for _, ev in candidates[:5])
        return json.dumps({"action": "multiple_calendar_matches", "message": "\n".join(lines)})

    event = candidates[0][1]
    event_id = event.get("id")
    if not event_id:
        return json.dumps({"action": "error", "message": "Google did not return an event id."})
    final_duration = max(1, int(duration_minutes or _event_duration_minutes(event)))

    try:
        from bot.services.calendar import find_conflicts
        conflicts = [
            conflict for conflict in await find_conflicts(db_user, new_start, final_duration)
            if conflict.get("id") != event_id
        ]
    except Exception as e:
        logger.warning("Reschedule conflict check failed for wa_id=%s: %s", wa_id, e)
        conflicts = []
    if conflicts:
        names = "; ".join(_format_calendar_event(conflict) for conflict in conflicts[:3])
        return json.dumps({
            "action": "conflict",
            "message": f"That new time conflicts with: {names}. Please choose another time.",
        })

    old_label = _format_calendar_event(event)
    new_label = new_start.strftime("%a %b %d, %I:%M %p")
    if not _confirmed:
        return json.dumps({
            "action": "awaiting_calendar_reschedule_confirmation",
            "event_id": event_id,
            "event_label": old_label,
            "new_start_time_iso": new_start.isoformat(),
            "duration_minutes": final_duration,
            "message": f"Move {old_label} to {new_label}?",
        })

    try:
        await update_event(
            db_user, event_id, start_iso=new_start.isoformat(),
            duration_minutes=final_duration,
        )
        await _update_linked_task_schedule(wa_id, event_id, new_start, final_duration)
    except Exception as e:
        return json.dumps({"action": "error", "message": f"Failed to reschedule the event: {e}"})
    return json.dumps({
        "action": "calendar_rescheduled",
        "message": f"Moved {old_label} to {new_label}.",
    })


# Words that carry no signal when picking WHICH event to add a guest to.
# "this meet" / "that meeting as well" must reduce to zero tokens so the
# single-upcoming-event shortcut can fire instead of scoring garbage.
_ADD_ATTENDEE_STOPWORDS = _CALENDAR_CANCEL_STOPWORDS | {
    "add", "also", "as", "attendee", "guest", "invite", "into", "join",
    "same", "that", "this", "too", "well",
}


def _add_attendee_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9@._+-]+", (query or "").lower())
    return [t for t in tokens if t not in _ADD_ATTENDEE_STOPWORDS and len(t) > 1]


async def calendar_add_attendee(wa_id: str, query: str = "", attendee_name: str = "",
                                attendee_email: str = "", start_time_iso: str = "",
                                user_text: str = "", _confirmed: bool = False) -> str:
    """Add someone to an EXISTING calendar event. Never creates a new event.

    This is the tool for "also invite X", "add X to that meeting" — requests
    that used to have no home, which left the model with only `set_gmeet` and
    pushed it into re-asking for a title and time it already had.

    Two things must resolve before anything is written: WHICH event, and WHO.
    Either can come back ambiguous, and each returns its own picker action
    rather than guessing. The write itself is gated behind a real button tap
    (`_confirmed`), like every other state-changing calendar path.
    """
    from bot.services.calendar import add_attendees
    from bot.services.gmeet_flow import parse_name_email_shortcut, resolve_attendee
    from bot.services.gmail import is_valid_email

    db_user = await _get_user(wa_id)
    if not db_user:
        return json.dumps({"action": "error", "message": "User not found."})
    if not db_user.google_token_json:
        return NOT_CONNECTED

    name, email = parse_name_email_shortcut(attendee_name, attendee_email)
    phone = ""

    # ── Who? ────────────────────────────────────────────────────
    if email and not is_valid_email(email):
        return json.dumps({
            "action": "error",
            "message": f"{email} doesn't look like a valid email address.",
        })
    if not email:
        if not name:
            return json.dumps({
                "action": "need_attendee",
                "message": "Who should I add? Send their name or Gmail address.",
            })
        r = await resolve_attendee(db_user, name)
        act = r.get("action")
        if act == "resolved":
            name, email, phone = r["name"], r["email"], r.get("phone") or ""
        elif act == "pick_contact":
            return json.dumps({
                "action": "pick_contact_for_add",
                "contacts": r["contacts"],
                "attendee_name": name,
                "query": query,
                "message": (
                    f"I found multiple contacts matching '{name}':\n"
                    + "\n".join(c["display"] for c in r["contacts"])
                    + "\nWhich one should I add? If none are correct, send the Gmail address."
                ),
            })
        elif act == "need_email":
            return json.dumps({
                "action": "need_attendee_email",
                "attendee_name": r["name"],
                "query": query,
                "message": (
                    f"I found {r['name']} but there's no email on that contact. "
                    f"Send their Gmail to add them."
                ),
            })
        elif act == "no_contact":
            return json.dumps({
                "action": "need_attendee_email",
                "attendee_name": name,
                "query": query,
                "message": f"I couldn't find {name} in your contacts. Send their Gmail to add them.",
            })
        else:
            return json.dumps({
                "action": "error",
                "message": r.get("message") or "Contact lookup failed.",
            })

    # ── Which event? ────────────────────────────────────────────
    # Same anti-hallucination rule as cancel/reschedule: a model-supplied
    # timestamp only narrows the search if the USER typed a clock time.
    if start_time_iso and user_text and not has_explicit_clock_time(user_text):
        logger.warning("Ignored ungrounded add-attendee time for wa_id=%s", wa_id)
        start_time_iso = ""

    try:
        events = await get_all_events(db_user, max_results=50)
    except Exception as e:
        return json.dumps({"action": "error", "message": f"Failed to fetch events: {e}"})
    if not events:
        return json.dumps({
            "action": "no_calendar_match",
            "message": "There are no upcoming events to add anyone to.",
        })

    tokens = _add_attendee_tokens(query)
    target = None
    if start_time_iso:
        try:
            target = ensure_aware_local(
                datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            target = None

    if not tokens and not target:
        # "add X to this meet" — unambiguous only when there's exactly one
        # upcoming event. Otherwise ask; never guess at "this".
        if len(events) == 1:
            event = events[0]
        else:
            lines = [f"Which event should I add {name or email} to?"]
            lines.extend(f"- {_format_calendar_event(ev)}" for ev in events[:10])
            return json.dumps({
                "action": "choose_event_for_add",
                "events": [
                    {"id": ev.get("id"), "label": _format_calendar_event(ev)}
                    for ev in events[:10] if ev.get("id")
                ],
                "attendee_name": name, "attendee_email": email,
                "attendee_phone": phone,
                "message": "\n".join(lines),
            })
    else:
        candidates = []
        for ev in events:
            score = 0
            if tokens:
                score = _score_event(tokens, ev)
                if not score:
                    continue
            if target:
                ev_start = _event_start_local(ev)
                if not ev_start or ev_start.date() != target.date():
                    continue
                if target.hour or target.minute:
                    delta_min = abs((ev_start - target).total_seconds()) / 60
                    if delta_min > 120:
                        continue
                    score += max(1, 8 - int(delta_min // 15))
                else:
                    score += 1
            candidates.append((score, ev))

        if not candidates:
            return json.dumps({
                "action": "no_calendar_match",
                "message": f"I couldn't find an upcoming event matching {query!r}.",
            })
        candidates.sort(key=lambda item: item[0], reverse=True)
        if _is_ambiguous_match(candidates):
            lines = [f"More than one event matches. Which should I add {name or email} to?"]
            lines.extend(f"- {_format_calendar_event(ev)}" for _, ev in candidates[:5])
            return json.dumps({
                "action": "choose_event_for_add",
                "events": [
                    {"id": ev.get("id"), "label": _format_calendar_event(ev)}
                    for _, ev in candidates[:5] if ev.get("id")
                ],
                "attendee_name": name, "attendee_email": email,
                "attendee_phone": phone,
                "message": "\n".join(lines),
            })
        event = candidates[0][1]

    event_id = event.get("id")
    if not event_id:
        return json.dumps({"action": "error", "message": "Google did not return an event id."})

    already_on = {
        (a.get("email") or "").strip().lower()
        for a in (event.get("attendees") or []) if a.get("email")
    }
    if email.lower() in already_on:
        return json.dumps({
            "action": "attendee_already_present",
            "message": f"{name or email} is already on {_format_calendar_event(event)}.",
        })

    # ── Confirmation gate ───────────────────────────────────────
    if not _confirmed:
        return json.dumps({
            "action": "awaiting_add_attendee_confirmation",
            "event_id": event_id,
            "event_label": _format_calendar_event(event),
            "attendee_name": name, "attendee_email": email,
            "attendee_phone": phone,
            "message": f"Add {name or email} to {_format_calendar_event(event)}?",
        })

    try:
        result = await add_attendees(db_user, event_id, [email])
    except Exception as e:
        logger.error("add_attendee failed for wa_id=%s: %s", wa_id, e)
        return json.dumps({
            "action": "error",
            "message": f"Failed to add them to the event: {e}",
        })

    if not result.get("added"):
        return json.dumps({
            "action": "attendee_already_present",
            "message": f"{name or email} was already on {_format_calendar_event(event)}.",
        })

    # Bilateral notify — Google emails them; WhatsApp is best-effort on top
    # and must never turn a successful add into a reported failure.
    if phone:
        try:
            from bot.services.whatsapp import send_meeting_notification
            start = _event_start_local(event)
            send_meeting_notification(
                phone,
                db_user.display_name or db_user.first_name or "Someone",
                result.get("summary") or "Meeting",
                start.strftime("%a %b %d, %I:%M %p IST") if start else "TBD",
                result.get("meet_link") or "",
            )
        except Exception as e:
            logger.warning("Attendee WhatsApp notify failed for wa_id=%s: %s", wa_id, e)

    return json.dumps({
        "action": "attendee_added",
        "message": f"Added {name or email} to {_format_calendar_event(event)}. They've been emailed the invite.",
    })


# ═══════════════════════════════════════════════════════════════
# Post-meeting summaries (pull path over meeting_summaries)
# ═══════════════════════════════════════════════════════════════

def _summary_tokens(query: str) -> list[str]:
    """Content words from a free-text query, minus filler."""
    stop = {
        "the", "a", "an", "from", "with", "about", "was", "were", "what",
        "show", "me", "my", "meeting", "call", "summary", "recap", "notes",
        "items", "item", "actions", "action", "and", "for", "of", "in",
        "on", "at", "to", "last", "recent", "latest", "yesterdays", "todays",
    }
    return [t for t in re.findall(r"[a-z0-9]{2,}", (query or "").lower())
            if t not in stop]


def _summary_haystack(payload: dict) -> str:
    """Everything a query could plausibly match against one summary."""
    parts = [payload.get("title") or "", payload.get("headline") or ""]
    parts += [t for t in (payload.get("key_takeaways") or []) if isinstance(t, str)]
    parts += [d.get("decision", "") for d in (payload.get("decisions") or [])
              if isinstance(d, dict)]
    parts += [a.get("task", "") for a in (payload.get("action_items") or [])
              if isinstance(a, dict)]
    return " ".join(parts).lower()


def _summary_label(task_title: str, headline: str, start_local) -> str:
    """One-line picker label: 'Pricing call — Mon Aug 17, 3:00 PM'."""
    name = (headline or task_title or "Untitled meeting").strip()
    when = start_local.strftime("%a %b %d, %I:%M %p") if start_local else "time unknown"
    return f"{name[:60]} — {when}"


async def get_meeting_summaries(wa_id: str, query: str = "") -> str:
    """Return stored post-meeting summaries (transcript-based), as JSON actions.

    Read-only. The deterministic renderer in main.py/callback_handler turns the
    winning payload into a card with follow-up buttons; this tool only finds
    and shapes it.

    Actions:
      meeting_summary  — one clear match (or exactly one summary exists)
      pick_summary     — several candidates → list picker
      no_summaries     — nothing transcribed yet
      no_match         — summaries exist but none match the query
    """
    async with async_session() as session:
        rows = list((await session.execute(
            select(MeetingSummary)
            .where(
                MeetingSummary.wa_id == wa_id,
                MeetingSummary.summary_json.isnot(None),
            )
            .order_by(MeetingSummary.created_at.desc())
            .limit(20)
        )).scalars().all())

        # task_id → Task, for title + scheduled time on each row.
        task_ids = {r.task_id for r in rows if r.task_id}
        tasks = {}
        if task_ids:
            for t in (await session.execute(
                select(Task).where(Task.id.in_(task_ids))
            )).scalars().all():
                tasks[t.id] = t

    payloads = []
    for row in rows:
        try:
            data = json.loads(row.summary_json or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        task = tasks.get(row.task_id)
        start_local = None
        if task is not None and task.scheduled_at is not None:
            start_local = to_local_aware(task.scheduled_at)
        payloads.append({
            "task_id": row.task_id,
            "provider": row.provider or "google_meet",
            "provider_label": ("Zoom" if row.provider == "zoom"
                               else "Google Meet"),
            "title": (task.title if task else "") or "",
            "when": start_local.isoformat() if start_local else "",
            "when_label": _summary_label(
                task.title if task else "",
                data.get("headline", ""),
                start_local,
            ),
            "headline": data.get("headline", "") or "",
            "key_takeaways": data.get("key_takeaways") or [],
            "decisions": data.get("decisions") or [],
            "action_items": data.get("action_items") or [],
            "per_person": data.get("per_person") or [],
            "unclear": data.get("unclear") or [],
        })

    if not payloads:
        return json.dumps({
            "action": "no_summaries",
            "message": (
                "No transcribed meetings yet. Summaries appear here after a "
                "booked Google Meet or Zoom call that produced a transcript "
                "ends."
            ),
        })

    tokens = _summary_tokens(query)

    if not tokens:
        if len(payloads) == 1:
            return json.dumps({"action": "meeting_summary", **payloads[0]})
        return json.dumps({
            "action": "pick_summary",
            "summaries": payloads[:10],
            "message": "Which meeting? Pick one below.",
        })

    scored = []
    for p in payloads:
        hay = _summary_haystack(p)
        score = sum(1 for tok in tokens if tok in hay)
        if score >= max(1, len(tokens) // 2):
            scored.append((score, p))
    scored.sort(key=lambda sp: -sp[0])

    if not scored:
        return json.dumps({
            "action": "no_match",
            "query": query,
            "summaries": payloads[:10],
            "message": f"No meeting summary matches {query!r}.",
        })
    if len(scored) == 1 or scored[0][0] > scored[1][0]:
        return json.dumps({"action": "meeting_summary", **scored[0][1]})
    return json.dumps({
        "action": "pick_summary",
        "summaries": [p for _, p in scored[:10]],
        "message": "More than one meeting matches — which one?",
    })


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

def _mask_contact_value(value: str, keep: int = 3) -> str:
    value = (value or "").strip()
    if not value:
        return "-"
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    return f"***{digits[-keep:]}" if digits else "***"


async def contacts_search(wa_id: str, query: str = "", limit: int = 5,
                          requested_fields: str = "") -> str:
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    try:
        contacts = await search_contacts(db_user, query=query, limit=min(int(limit or 5), 3))
        if not contacts:
            return f"No contacts found for '{query}'." if query else "No contacts."
        requested = {field.strip() for field in requested_fields.split(",") if field.strip()}
        lines = [f"Found {len(contacts)} contacts:"]
        for c in contacts:
            emails = ", ".join(c["emails"]) if "email" in requested else \
                ", ".join(_mask_contact_value(value) for value in c["emails"])
            phones = ", ".join(c["phones"]) if "phone" in requested else \
                ", ".join(_mask_contact_value(value) for value in c["phones"])
            emails = emails or "-"
            phones = phones or "-"
            lines.append(f"- {c['name']} — email: {emails}, phone: {phones}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to search contacts: {e}"


# ═══════════════════════════════════════════════════════════════
# Gmail
# ═══════════════════════════════════════════════════════════════

async def gmail_search(wa_id: str, query: str = "", limit: int = 0,
                       include_body: bool = False) -> str:
    """Search Gmail and return recent matching messages."""
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED

    try:
        from bot.services.gmail import GmailScopeError, search_messages
        # This was hard-capped at 3, so EVERY request — "today", "this week",
        # an explicit date range — came back with the same three messages and
        # a summary that silently under-reported the inbox. The real limit is
        # context, not correctness, so it now depends on payload size: full
        # bodies are large, metadata lines are ~100 chars each.
        ceiling = 5 if include_body else 30
        messages = await search_messages(
            db_user,
            query=query,
            max_results=min(int(limit or ceiling), ceiling),
            include_body=include_body,
        )
    except GmailScopeError:
        return (
            "GMAIL_SCOPE_MISSING: Google is connected, but Gmail permission is "
            "missing. Ask the user to type connect and grant the updated Gmail "
            "permission. SHOW_CONNECT_BUTTON"
        )
    except Exception as e:
        return f"Failed to search Gmail: {e}"

    if not messages:
        return "No matching email found."

    lines = [f"Gmail results for {query!r}:" if query else "Recent Gmail messages:"]
    for i, msg in enumerate(messages, start=1):
        lines.append(
            f"{i}. From: {msg.get('from') or 'unknown'} | "
            f"Subject: {msg.get('subject') or '(no subject)'} | "
            f"Date: {msg.get('date') or 'unknown'}"
        )
        snippet = msg.get("snippet") or ""
        # Snippets dominate the payload — 240 chars x 30 messages is ~7k of
        # context on its own, which left the model no room to actually write
        # the summary. On a long listing the sender and subject already carry
        # the categorisation ("Tata CLiQ Fashion — Win Rakhi This Year" needs
        # no snippet to be read as promotional), so drop them entirely there
        # and keep them only when there are few enough messages to matter.
        if snippet and len(messages) <= 12:
            lines.append(f"   Snippet: {snippet[:240]}")
        if include_body:
            body = msg.get("body") or ""
            if body:
                lines.append(f"   Body: {body[:500]}")
    return "\n".join(lines)


async def compose_email(wa_id: str, to: str = "") -> str:
    """Open the WhatsApp email form so the USER can write and send an email.

    This tool does NOT write or send anything itself — it just opens a native
    form where the user types the recipient, subject and body, which are then
    sent from their own Gmail. The AI never sees or edits the content.

    `to` is an optional recipient prefill — pass it ONLY if the user typed a
    literal email address. Never invent it, never pass a subject or body.
    """
    db_user = await _get_user(wa_id)
    if not db_user:
        return "User not found."
    if not db_user.google_token_json:
        return NOT_CONNECTED
    if not config.WA_EMAIL_FLOW_ID:
        return (
            "EMAIL_FLOW_NOT_CONFIGURED: Emailing from chat isn't set up yet. "
            "Tell the user it's not available right now. Do NOT offer to write "
            "or send the email yourself."
        )

    # Local import avoids a tools <-> handlers import cycle at module load.
    from bot.handlers.callback_handler import send_email_flow

    # Only forward a recipient that is actually an email address.
    to_init = to.strip() if to and _looks_like_email(to) else ""
    ok = await send_email_flow(wa_id, to_init=to_init)
    if ok:
        return (
            "EMAIL_FORM_OPENED: The email form is now open for the user to fill "
            "in and send themselves. Do NOT write the email, and do NOT ask for "
            "the subject or body in chat. Just briefly confirm the form is open."
        )
    return (
        "EMAIL_FLOW_SEND_FAILED: Couldn't open the email form just now. Ask the "
        "user to try again shortly. Do NOT write or send the email yourself."
    )


def _looks_like_email(value: str) -> bool:
    return bool(re.match(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$", (value or "").strip()))


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
    from bot.services.task_service import load_gmeet_slots, save_gmeet_slots

    db_user = await _get_user(wa_id)
    if not db_user:
        return json.dumps({"action": "error", "message": "User not found."})
    if not db_user.google_token_json:
        return NOT_CONNECTED

    # Parse name{email} shortcut up-front
    name, email = parse_name_email_shortcut(attendee_name, attendee_email)
    phone = (attendee_phone or "").strip()

    # ── Carry the booking across turns ──────────────────────────
    # A booking is collected over several messages. Fill anything this call
    # left blank from the slots already gathered, so "akshay@gmail.com" on
    # turn 2 doesn't erase the "7 pm today" from turn 1.
    stored = {} if _confirmed else await load_gmeet_slots(wa_id)
    name  = name  or stored.get("attendee_name", "")
    email = email or stored.get("attendee_email", "")
    phone = phone or stored.get("attendee_phone", "")
    title = title or stored.get("title", "")
    duration_minutes = duration_minutes or stored.get("duration_minutes", 30)

    # A confirmed "Yes, save" tap is the FINAL gate. The conflict prompt (if
    # any) was already shown and resolved ("keep both") BEFORE the confirm
    # card appeared — so re-running the conflict check here would bounce the
    # user back to the conflict screen forever (the confirm→conflict→keep
    # both→confirm loop). Confirm ⇒ skip the conflict check and just create.
    if _confirmed:
        skip_conflict_check = True

    # ── Anti-hallucination guard (PRD §FR-3 + Procedural Integrity) ──
    # The model may only introduce a NEW time if this turn's message actually
    # contained a clock token. But a time already banked in the slot store was
    # itself verified that way when it arrived, so carrying it forward is
    # grounded — the old guard wiped it anyway and made the bot re-ask.
    stored_time = stored.get("start_time_iso", "")
    if not _confirmed:
        turn_has_clock = bool(user_text) and has_explicit_clock_time(user_text)
        if start_time_iso and user_text and not turn_has_clock:
            if start_time_iso == stored_time:
                pass                      # recalled, already verified — keep it
            else:
                logger.warning("Rejected ungrounded Google Meet time for wa_id=%s", wa_id)
                start_time_iso = ""
        if not start_time_iso:
            start_time_iso = stored_time  # fall back to the banked slot

    # Bank whatever is known now. Only a time this turn actually grounded (or
    # one already banked) can be stored, which is what keeps the guard honest.
    if not _confirmed:
        await save_gmeet_slots(wa_id, {
            "attendee_name": name, "attendee_email": email,
            "attendee_phone": phone, "title": title,
            "start_time_iso": start_time_iso,
            "duration_minutes": duration_minutes,
        })

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
                       relative_minutes: int = 0, user_text: str = "") -> str:
    """Set a one-time or recurring daily reminder.

    For relative times: pass relative_minutes (server computes exact time).
    For absolute times: pass remind_at_iso.
    For recurring: set is_recurring=True and provide recur_time_hhmm (HH:MM, 24h).

    All datetime math goes through bot.utils.time — no inline tz logic.
    """
    if reminder_type not in ("text", "notes", "calendar"):
        reminder_type = "text"
    labels = {"text": "", "notes": "", "calendar": ""}

    # Relative minutes are computed by the server. Every other reminder time
    # must be grounded in a clock token the user actually supplied.
    if (remind_at_iso or (is_recurring and recur_time_hhmm)) and user_text and not has_explicit_clock_time(user_text):
        return (
            "TIME_REQUIRED: I need a clock time before setting that reminder. "
            "Ask the user for a time such as 3 PM."
        )

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
            rtype = "Daily" if r.is_recurring else "One-time"
            lines.append(f'#{r.id} {rtype} at {dt_local.strftime("%b %d, %I:%M %p")}: "{r.message}"')

    if fired:
        lines.append("\nRecently fired (already sent):")
        for r in fired:
            dt_local = to_local_aware(r.remind_at)
            lines.append(f'#{r.id} FIRED at {dt_local.strftime("%I:%M %p")}: "{r.message}" — DO NOT create a duplicate.')

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

    lines = [f"Added {result['added']} item(s) to your to-do list:\n"]
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
        status_icon = "[done]" if "Done" in item.get("status", "") else "[ ]"
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
    return f'To-do #{result["item_index"]} "{result["task"]}" marked as {action}.'


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

    return f'Deleted to-do #{result["item_index"]} "{result["task"]}".'
