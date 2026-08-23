"""Test harness: a fake WhatsApp transport + a message Simulator.

`Simulator` drives the *real* `_process_update` entry point (the same
function the webhook calls), so onboarding, routing, the agent loop, the
gmeet state machine and the confirmation gate all run for real. Only the
outside world is faked:

  • WhatsApp send_* / templates  → captured into per-number inboxes
  • Google contacts / calendar   → canned data
  • Groq LLM                      → deterministic mock (tester.llm_mock)
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import tester  # noqa: F401  (sets DATABASE_URL + dummy env first)

from bot.database import init_db, async_session, User, Task
from bot.utils.time import now_local, to_utc_naive
from sqlalchemy import select, delete as sql_delete

from tester import llm_mock


# ════════════════════════════════════════════════════════════════
# Personas (PRD Appendix A)
# ════════════════════════════════════════════════════════════════

CREATOR = "919900000001"          # "Harsh" — the bot's primary user
PRIYA_PHONE = "+919876543210"     # assignee, valid E.164 → wa_id 919876543210
PRIYA_EMAIL = "priya@example.com"

# Canned Google Contacts keyed by lowercase query.
CONTACTS = {
    "priya": [
        {"name": "Priya", "emails": [PRIYA_EMAIL], "phones": [PRIYA_PHONE]},
    ],
    # Ambiguous query → picker
    "pri": [
        {"name": "Priya", "emails": [PRIYA_EMAIL], "phones": [PRIYA_PHONE]},
        {"name": "Pritam", "emails": ["pritam@example.com"], "phones": ["+919876500003"]},
    ],
}


# ════════════════════════════════════════════════════════════════
# Simulator
# ════════════════════════════════════════════════════════════════

class Simulator:
    def __init__(self):
        self.inbox = defaultdict(list)   # wa_id -> [ {kind, body, ...} ]

    # — outbound capture —
    def record(self, to, kind, **fields):
        self.inbox[to].append(dict(kind=kind, **fields))

    def last(self, wa_id):
        msgs = self.inbox.get(wa_id) or []
        return msgs[-1] if msgs else None

    def all_text(self, wa_id):
        return " || ".join(
            m.get("body", m.get("kind", "")) for m in self.inbox.get(wa_id, [])
        )

    def button_ids(self, wa_id):
        m = self.last(wa_id)
        return (m or {}).get("buttons", []) or (m or {}).get("rows", [])

    def clear(self, wa_id=None):
        if wa_id:
            self.inbox[wa_id] = []
        else:
            self.inbox.clear()

    # — inbound simulation (drives the real router) —
    async def text(self, wa_id, body):
        from bot.main import _process_update
        await _process_update(wa_id, {
            "type": "text", "from": wa_id,
            "id": f"wamid.{uuid.uuid4().hex}",
            "text": {"body": body},
        })

    async def tap(self, wa_id, button_id, title=""):
        from bot.main import _process_update
        await _process_update(wa_id, {
            "type": "interactive", "from": wa_id,
            "id": f"wamid.{uuid.uuid4().hex}",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": button_id, "title": title}},
        })

    async def pick(self, wa_id, row_id, title=""):
        from bot.main import _process_update
        await _process_update(wa_id, {
            "type": "interactive", "from": wa_id,
            "id": f"wamid.{uuid.uuid4().hex}",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": row_id, "title": title}},
        })


# ════════════════════════════════════════════════════════════════
# Mock installation
# ════════════════════════════════════════════════════════════════

# Knobs scenarios can flip:
STATE = {
    "conflicts": [],        # what gmeet_flow.find_conflicts returns
    "events": [],           # what calendar.get_all_events returns
    "attendee_adds": [],    # every calendar.add_attendees call, in order
}


def install_mocks(sim: Simulator):
    """Patch all external I/O to point at the simulator / canned data."""
    import bot.services.whatsapp as wa
    import bot.main as main
    import bot.handlers.callback_handler as cb
    import bot.services.google_auth as ga
    import bot.services.onboarding as ob
    import bot.services.gmeet_flow as gm
    import bot.agent as agent
    import bot.tools as tools
    import bot.services.calendar as cal

    def cap_text(wa_id, text):
        sim.record(wa_id, "text", body=text); return True

    def cap_buttons(wa_id, body, buttons):
        sim.record(wa_id, "buttons", body=body,
                   buttons=[b["id"] for b in buttons]); return True

    def cap_list(wa_id, body, label, sections):
        rows = [r["id"] for s in sections for r in s.get("rows", [])]
        sim.record(wa_id, "list", body=body, rows=rows); return True

    def cap_template(wa_id, name, language_code="en_US", components=None):
        sim.record(wa_id, "template", name=name); return True

    def cap_notif(attendee_phone, booker_name, meeting_title,
                  meeting_time, meet_link=""):
        sim.record(attendee_phone, "notification",
                   body=meeting_title, booker=booker_name); return True

    def cap_markread(*a, **k):
        return None

    def cap_flow(wa_id, body_text, flow_id, screen, data, cta="Open",
                 flow_token=""):
        sim.record(wa_id, "flow", body=body_text, flow_id=flow_id,
                   screen=screen, data=data); return True

    # Source module
    wa.send_message = cap_text
    wa.send_buttons = cap_buttons
    wa.send_list = cap_list
    wa.send_template = cap_template
    wa.send_meeting_notification = cap_notif
    wa.mark_read = cap_markread
    wa.send_flow = cap_flow

    # Modules that did `from whatsapp import ...` (bound copies)
    for mod in (main, cb, ga, ob):
        if hasattr(mod, "send_message"):
            mod.send_message = cap_text
        if hasattr(mod, "send_buttons"):
            mod.send_buttons = cap_buttons
        if hasattr(mod, "send_list"):
            mod.send_list = cap_list

    # Google contacts / calendar used inside gmeet_flow
    async def fake_search_contacts(db_user, query="", limit=10):
        return list(CONTACTS.get((query or "").lower().strip(), []))

    async def fake_create_event(db_user, title, event_dt, duration_minutes=30,
                                meet_link=False, attendees=None, description=None):
        return {
            "id": f"evt_{uuid.uuid4().hex[:8]}",
            "link": "https://calendar.google.com/event?eid=test",
            "meet": "https://meet.google.com/test-abc-def" if meet_link else "",
        }

    async def fake_find_conflicts(db_user, dt, duration_minutes):
        return list(STATE["conflicts"])

    async def fake_get_all_events(db_user, max_results=10):
        return list(STATE["events"])

    async def fake_add_attendees(db_user, event_id, emails):
        ev = next((e for e in STATE["events"] if e.get("id") == event_id), {})
        existing = {
            (a.get("email") or "").lower()
            for a in (ev.get("attendees") or []) if a.get("email")
        }
        added = [e for e in emails if e and e.lower() not in existing]
        already = [e for e in emails if e and e.lower() in existing]
        if added:
            ev.setdefault("attendees", []).extend({"email": e} for e in added)
        STATE["attendee_adds"].append({"event_id": event_id, "emails": list(emails),
                                       "added": added})
        return {"summary": ev.get("summary", "Untitled"),
                "start": (ev.get("start") or {}).get("dateTime", ""),
                "meet_link": ev.get("hangoutLink", ""),
                "added": added, "already": already}

    gm.search_contacts = fake_search_contacts
    gm.create_event = fake_create_event
    gm.find_conflicts = fake_find_conflicts
    # tools.py did `from ... import get_all_events` (bound copy); add_attendees
    # is imported inside the function, so patch it on the module itself.
    tools.get_all_events = fake_get_all_events
    cal.add_attendees = fake_add_attendees

    # LLM + datetime parser
    agent._call_groq = llm_mock.make_call_groq()
    cb.parse_datetime_from_text = llm_mock.make_parse_datetime()


# ════════════════════════════════════════════════════════════════
# DB helpers
# ════════════════════════════════════════════════════════════════

def reset_rate_limit(wa_id=None):
    """Clear the per-user flood guard between scenarios."""
    import bot.main as main
    if wa_id:
        main._rate_buckets.pop(wa_id, None)
        main._rate_notified.pop(wa_id, None)
    else:
        main._rate_buckets.clear()
        main._rate_notified.clear()


async def setup_db():
    await init_db()


async def reset_user(wa_id):
    """Remove a user and EVERYTHING keyed to them so a scenario starts clean.

    SQLite runs with PRAGMA foreign_keys=ON (database.py), so any surviving
    child row — notes, chat memory, contacts cache — makes the user DELETE
    fail. Wipe the full set the way the production 'delete my data' handler
    does, not just tasks.
    """
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.wa_id == wa_id))).scalar_one_or_none()
        if u:
            from bot.database import (
                Note, ChatMemory, ContactCache, MeetingSummary,
                Reminder, TaskConversationState, OAuthState, WebhookMessage,
            )
            await s.execute(sql_delete(Task).where(Task.wa_id == wa_id))
            await s.execute(sql_delete(Note).where(Note.user_id == u.id))
            await s.execute(sql_delete(ChatMemory).where(ChatMemory.user_id == u.id))
            await s.execute(sql_delete(ContactCache).where(ContactCache.user_id == u.id))
            await s.execute(sql_delete(MeetingSummary).where(MeetingSummary.wa_id == wa_id))
            await s.execute(sql_delete(Reminder).where(Reminder.wa_id == wa_id))
            await s.execute(sql_delete(TaskConversationState).where(
                TaskConversationState.wa_id == wa_id))
            await s.execute(sql_delete(OAuthState).where(OAuthState.wa_id == wa_id))
            await s.execute(sql_delete(WebhookMessage).where(WebhookMessage.wa_id == wa_id))
            await s.delete(u)
        await s.commit()
    # also clear conversation state
    from bot.services.task_service import clear_conversation_state
    await clear_conversation_state(wa_id)


async def make_onboarded_user(wa_id, name="Harsh", tz="Asia/Kolkata", google=True):
    await reset_user(wa_id)
    async with async_session() as s:
        s.add(User(
            wa_id=wa_id, display_name=name, first_name=name, timezone=tz,
            onboarding_complete=True, whatsapp_confirmed=True,
            consent_status="OPT_IN",
            google_token_json=("dummy-token" if google else None),
        ))
        await s.commit()


async def get_user(wa_id):
    async with async_session() as s:
        return (await s.execute(select(User).where(User.wa_id == wa_id))).scalar_one_or_none()


async def get_tasks(wa_id):
    async with async_session() as s:
        return list((await s.execute(
            select(Task).where(Task.wa_id == wa_id).order_by(Task.id)
        )).scalars().all())


async def insert_scheduled_task(wa_id, title, minutes_from_now, assignee_id=None,
                                assignee_phone=None, duration=30):
    """Insert a SCHEDULED task at now+minutes (negative = in the past)."""
    when_local = now_local() + timedelta(minutes=minutes_from_now)
    async with async_session() as s:
        t = Task(
            wa_id=wa_id, title=title, status="scheduled",
            scheduled_at=to_utc_naive(when_local), duration_minutes=duration,
            mode="online", meeting_link="https://meet.google.com/test-abc-def",
            assignee_id=assignee_id, assignee_phone=assignee_phone,
            timezone="Asia/Kolkata", reminder_offsets_min=[1440, 60],
        )
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
    return tid
