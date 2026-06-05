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
STATE = {"conflicts": []}   # what gmeet_flow.find_conflicts returns


def install_mocks(sim: Simulator):
    """Patch all external I/O to point at the simulator / canned data."""
    import bot.services.whatsapp as wa
    import bot.main as main
    import bot.handlers.callback_handler as cb
    import bot.services.google_auth as ga
    import bot.services.onboarding as ob
    import bot.services.gmeet_flow as gm
    import bot.agent as agent

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

    # Source module
    wa.send_message = cap_text
    wa.send_buttons = cap_buttons
    wa.send_list = cap_list
    wa.send_template = cap_template
    wa.send_meeting_notification = cap_notif
    wa.mark_read = cap_markread

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

    gm.search_contacts = fake_search_contacts
    gm.create_event = fake_create_event
    gm.find_conflicts = fake_find_conflicts

    # LLM + datetime parser
    agent._call_groq = llm_mock.make_call_groq()
    cb.parse_datetime_from_text = llm_mock.make_parse_datetime()


# ════════════════════════════════════════════════════════════════
# DB helpers
# ════════════════════════════════════════════════════════════════

async def setup_db():
    await init_db()


async def reset_user(wa_id):
    """Remove a user and their tasks so a scenario starts clean."""
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.wa_id == wa_id))).scalar_one_or_none()
        if u:
            await s.execute(sql_delete(Task).where(Task.wa_id == wa_id))
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
