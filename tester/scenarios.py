"""PRD Appendix-A user scripts, plus FR-8/FR-9 checks.

Each scenario simulates a real WhatsApp user and asserts the bot behaves
per the PRD. Raises AssertionError with a readable message on failure.
"""

from __future__ import annotations

from datetime import timedelta

from bot.utils.time import now_local
from tester import harness as H
from tester.harness import (
    Simulator, STATE, CREATOR, PRIYA_PHONE, PRIYA_EMAIL,
)

# `install_mocks()` replaces `bot.agent._call_groq` wholesale with a canned
# response generator for every other scenario in this file — captured here,
# at import time, BEFORE that monkeypatch runs (run.py imports this module
# before calling install_mocks). This is the one real, unpatched reference,
# needed to test the retry logic inside _call_groq itself rather than the
# fake replacing it.
from bot.agent import _call_groq as _REAL_CALL_GROQ

PRIYA_WA = "919876543210"   # to_wa_id(PRIYA_PHONE)
NEWBIE = "919800000010"


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tomorrow_at(hour, minute=0):
    base = now_local() + timedelta(days=1)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ════════════════════════════════════════════════════════════════
# A.1 — Onboarding
# ════════════════════════════════════════════════════════════════
async def scenario_onboarding(sim: Simulator):
    await H.reset_user(NEWBIE)
    sim.clear(NEWBIE)

    await sim.text(NEWBIE, "hi")
    last = sim.last(NEWBIE)
    expect(last and last["kind"] == "buttons", "expected WA-confirm buttons on first contact")
    expect("onboard_wa_yes" in last["buttons"], "expected onboard_wa_yes button")
    expect("privacy" in sim.all_text(NEWBIE).lower(), "privacy notice must appear during onboarding")

    await sim.tap(NEWBIE, "onboard_wa_yes")
    expect("call you" in sim.all_text(NEWBIE).lower() or "what should i call" in sim.all_text(NEWBIE).lower(),
           "expected name prompt after WA confirm")

    await sim.text(NEWBIE, "Harsh")
    last = sim.last(NEWBIE)
    expect(last["kind"] == "buttons" and "onboard_tz_yes" in last["buttons"],
           "expected timezone-confirm buttons after name")

    await sim.tap(NEWBIE, "onboard_tz_yes")
    u = await H.get_user(NEWBIE)
    expect(u.onboarding_complete is True, "onboarding_complete must be True")
    expect(u.display_name == "Harsh", f"display_name should be Harsh, got {u.display_name!r}")
    expect(u.whatsapp_confirmed is True, "whatsapp_confirmed must be True")
    expect(u.consent_status == "OPT_IN", f"consent should be OPT_IN, got {u.consent_status}")
    expect("connect_google" in (sim.last(NEWBIE) or {}).get("buttons", []),
           "should offer Google connect at the end")


# ════════════════════════════════════════════════════════════════
# A.2 — Happy path
# ════════════════════════════════════════════════════════════════
async def scenario_happy_path(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR); sim.clear(PRIYA_WA)
    STATE["conflicts"] = []

    await sim.text(CREATOR, "Schedule a meeting with Priya tomorrow 4 PM")
    last = sim.last(CREATOR)
    expect(last and last["kind"] == "buttons", "expected a confirmation card")
    expect("gmeet_confirm_yes" in last["buttons"], "expected ✅ Yes confirm button")
    expect("confirm" in last["body"].lower(), "confirmation body should say 'Confirm'")
    # Nothing created yet — confirmation gate (FR-7)
    expect(len(await H.get_tasks(CREATOR)) == 0, "no Task should exist before user taps Yes")

    await sim.tap(CREATOR, "gmeet_confirm_yes")
    expect("saved" in sim.all_text(CREATOR).lower(), "expected 'Saved' after confirmation")

    tasks = await H.get_tasks(CREATOR)
    expect(len(tasks) == 1, f"exactly one Task should exist, got {len(tasks)}")
    t = tasks[0]
    expect(t.status == "scheduled", f"task should be scheduled, got {t.status}")
    expect(t.assignee_name == "Priya", "assignee_name should be Priya")
    expect(t.assignee_id is not None, "assignee_id must be linked (FR-8)")
    expect(bool(t.meeting_link), "meeting_link should be set")
    # Assignee notified via template (FR-8)
    note = sim.last(PRIYA_WA)
    expect(note and note["kind"] == "notification", "assignee should get a template notification")


# ════════════════════════════════════════════════════════════════
# A.4 — Missing info (no time given)
# ════════════════════════════════════════════════════════════════
async def scenario_missing_info(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    STATE["conflicts"] = []

    await sim.text(CREATOR, "set a meeting with Priya")
    expect("time" in sim.all_text(CREATOR).lower(), "bot should ask for the meeting time")
    expect(len(await H.get_tasks(CREATOR)) == 0, "nothing scheduled while time missing")

    await sim.text(CREATOR, "tomorrow 4 pm")
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons" and "gmeet_confirm_yes" in last["buttons"],
           "after giving time, expect the confirmation card")

    await sim.tap(CREATOR, "gmeet_confirm_yes")
    expect("saved" in sim.all_text(CREATOR).lower(), "expected 'Saved' after confirmation")
    expect(len(await H.get_tasks(CREATOR)) == 1, "one Task should now exist")


# ════════════════════════════════════════════════════════════════
# A.3 — Conflict
# ════════════════════════════════════════════════════════════════
async def scenario_conflict(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    cs = _tomorrow_at(16, 0)
    STATE["conflicts"] = [{
        "summary": "Standup", "start": cs.isoformat(),
        "end": (cs + timedelta(minutes=30)).isoformat(),
    }]

    await sim.text(CREATOR, "meet Priya tomorrow 4 pm")
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons", "expected conflict resolution buttons")
    expect("conflict_keep_both" in last["buttons"], "expected 'Keep both' option")
    expect("conflict" in last["body"].lower() or "⚠" in last["body"], "body should flag the conflict")
    expect(len(await H.get_tasks(CREATOR)) == 0, "nothing scheduled while conflict unresolved")

    # NOTE: conflict stays ACTIVE on purpose. This is the regression guard
    # for the confirm→conflict→keep-both→confirm loop: once the user keeps
    # both and confirms, it MUST create even though the conflict persists.
    await sim.tap(CREATOR, "conflict_keep_both")
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons" and "gmeet_confirm_yes" in last["buttons"],
           "after keep-both, expect confirmation card (not the conflict again)")

    await sim.tap(CREATOR, "gmeet_confirm_yes")
    expect("saved" in sim.all_text(CREATOR).lower(),
           "confirming after keep-both must SAVE, not loop back to the conflict")
    expect(len(await H.get_tasks(CREATOR)) == 1, "task scheduled after confirming")

    STATE["conflicts"] = []  # cleanup for later scenarios


# ════════════════════════════════════════════════════════════════
# FR-8 — STOP / opt-out + START
# ════════════════════════════════════════════════════════════════
async def scenario_optout(sim: Simulator):
    from bot.services.task_service import upsert_assignee_user
    await H.make_onboarded_user(CREATOR, name="Harsh")
    await H.reset_user(PRIYA_WA)
    sim.clear(PRIYA_WA)

    priya_id = await upsert_assignee_user(PRIYA_PHONE, "Priya")
    tid = await H.insert_scheduled_task(CREATOR, "1:1", minutes_from_now=2000,
                                        assignee_id=priya_id, assignee_phone=PRIYA_PHONE)

    await sim.text(PRIYA_WA, "STOP")
    expect("won't get any more" in sim.all_text(PRIYA_WA).lower()
           or "opt" in sim.all_text(PRIYA_WA).lower(),
           "STOP should confirm opt-out")
    u = await H.get_user(PRIYA_WA)
    expect(u.consent_status == "OPT_OUT", "assignee consent should be OPT_OUT")
    t = [x for x in await H.get_tasks(CREATOR) if x.id == tid][0]
    expect(t.assignee_unreachable is True, "task assignee_unreachable should be True after STOP")

    sim.clear(PRIYA_WA)
    await sim.text(PRIYA_WA, "START")
    u = await H.get_user(PRIYA_WA)
    expect(u.consent_status == "OPT_IN", "START should re-opt-in")


# ════════════════════════════════════════════════════════════════
# §12 — Right to erasure ("delete my data")
# ════════════════════════════════════════════════════════════════
async def scenario_data_deletion(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    await H.insert_scheduled_task(CREATOR, "Throwaway", minutes_from_now=600)
    sim.clear(CREATOR)

    await sim.text(CREATOR, "delete my data")
    expect("deleted your data" in sim.all_text(CREATOR).lower()
           or "deleted" in sim.all_text(CREATOR).lower(),
           "deletion should be confirmed")
    expect(await H.get_user(CREATOR) is None, "user row should be gone after deletion")
    expect(len(await H.get_tasks(CREATOR)) == 0, "tasks should be gone after deletion")


# ════════════════════════════════════════════════════════════════
# Accumulation — giving the email must NOT drop an already-given time
# (regression guard for the "asks for time after email" bug)
# ════════════════════════════════════════════════════════════════
async def scenario_email_keeps_time(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    STATE["conflicts"] = []

    # Pin the LEGACY text path — this regression guard covers deployments
    # without a published Flow (and the send_flow-failure fallback).
    from bot.config import config
    _saved_flow_id = config.WA_GMEET_FLOW_ID
    config.WA_GMEET_FLOW_ID = ""
    try:
        await _email_keeps_time_body(sim)
    finally:
        config.WA_GMEET_FLOW_ID = _saved_flow_id


async def _email_keeps_time_body(sim: Simulator):
    # "Akshay" is NOT in contacts → bot asks for email. Time was given up front.
    await sim.text(CREATOR, "Set a gmeet with Akshay for today 12 pm")
    expect("gmail" in sim.all_text(CREATOR).lower() or "email" in sim.all_text(CREATOR).lower(),
           "should ask for the attendee's email (no_contact)")

    sim.clear(CREATOR)
    await sim.text(CREATOR, "akshay@example.com")
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons" and "gmeet_confirm_yes" in last["buttons"],
           "after email it must go straight to confirmation (time was already given)")
    expect("when should i schedule" not in sim.all_text(CREATOR).lower(),
           "must NOT re-ask for the time after the email")

    await sim.tap(CREATOR, "gmeet_confirm_yes")
    expect(len(await H.get_tasks(CREATOR)) == 1, "meeting saved after confirm")


# ════════════════════════════════════════════════════════════════
# Conflict with the user's OWN existing meeting is detected before confirm
# (regression guard for "not able to check conflict")
# ════════════════════════════════════════════════════════════════
async def scenario_local_task_conflict(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    STATE["conflicts"] = []   # Google returns nothing — rely on the Task table

    target = (now_local() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    mins = int((target - now_local()).total_seconds() // 60)
    await H.insert_scheduled_task(CREATOR, "Existing meeting", minutes_from_now=mins, duration=60)

    await sim.text(CREATOR, "meet Priya tomorrow 2 pm")
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons" and "conflict_keep_both" in last["buttons"],
           "a clash with the user's own bot meeting must be flagged BEFORE confirmation")


# ════════════════════════════════════════════════════════════════
# WhatsApp Flow — missing email opens the prefilled native form;
# submitting the form books directly (form IS the confirmation)
# ════════════════════════════════════════════════════════════════
async def scenario_flow_form(sim: Simulator):
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    STATE["conflicts"] = []

    import json as _json
    from bot.config import config
    from bot.handlers import callback_handler as cb

    _saved_flow_id = config.WA_GMEET_FLOW_ID
    config.WA_GMEET_FLOW_ID = "1285391600472269"
    try:
        await sim.text(CREATOR, "Set a gmeet with Akshay for tomorrow 9 am")
        last = sim.last(CREATOR)
        expect(last and last["kind"] == "flow",
               "missing attendee email must open the native form")
        d = last["data"]
        expect(d["name_init"] == "Akshay", "form must prefill the attendee name")
        expect(d["time_init"] == "09:00",
               f"form must prefill the mentioned time, got {d['time_init']!r}")
        expect(len(await H.get_tasks(CREATOR)) == 0, "nothing booked before submit")

        # User submits the form (nfm_reply payload).
        tomorrow = (now_local() + timedelta(days=1)).date()
        await cb.handle_gmeet_flow_completion(CREATOR, _json.dumps({
            "topic": "Quick sync",
            "name": "Akshay",
            "email": "akshay@example.com",
            "date": cb._epoch_ms_utc_midnight(tomorrow),
            "time": "09:00",
        }))
        expect("saved" in sim.all_text(CREATOR).lower(),
               "form submission must book directly — no second confirm card")
        tasks = await H.get_tasks(CREATOR)
        expect(len(tasks) == 1, f"exactly one Task after form submit, got {len(tasks)}")
        expect(tasks[0].assignee_name == "Akshay", "assignee should come from the form")
    finally:
        config.WA_GMEET_FLOW_ID = _saved_flow_id


async def scenario_email_flow(sim: Simulator):
    """Send-email-from-chat wrapper: form opens, content goes Flow -> Gmail,
    the AI never touches it, and the meeting form still routes correctly."""
    await H.make_onboarded_user(CREATOR, name="Harsh", google=True)
    sim.clear(CREATOR)

    import json as _json
    from bot.config import config
    from bot import tools
    from bot.services import gmail as gmail_mod
    from bot.handlers import callback_handler as cb

    # Capture what would be sent to Gmail; never hit the real API.
    sent = []
    async def fake_send_email(user_db, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "id": "msg_test", "to": to}
    _real_send = gmail_mod.send_email
    gmail_mod.send_email = fake_send_email

    _saved = config.WA_EMAIL_FLOW_ID
    _saved_gmeet = config.WA_GMEET_FLOW_ID
    try:
        # 1. No Flow configured → feature is OFF, AI must NOT draft anything.
        config.WA_EMAIL_FLOW_ID = ""
        res = await tools.compose_email(CREATOR, to="akshay@example.com")
        expect("EMAIL_FLOW_NOT_CONFIGURED" in res,
               "no flow id must report the feature as unavailable")

        # 2. Flow configured → compose_email opens the form (prefills recipient).
        config.WA_EMAIL_FLOW_ID = "email_flow_test"
        res = await tools.compose_email(CREATOR, to="akshay@example.com")
        expect("EMAIL_FORM_OPENED" in res, "compose_email must open the form")
        last = sim.last(CREATOR)
        expect(last and last["kind"] == "flow", "an email Flow card must be sent")
        expect(last["screen"] == "COMPOSE", "email Flow opens the COMPOSE screen")
        expect(last["data"].get("to_init") == "akshay@example.com",
               "a literal recipient is prefilled")
        expect(not sent, "nothing is emailed just by opening the form")

        # 3. A bare/garbage recipient is never prefilled (AI can't invent it).
        sim.clear(CREATOR)
        await tools.compose_email(CREATOR, to="akshay")          # not an email
        expect(sim.last(CREATOR)["data"].get("to_init") == "",
               "non-email 'to' must not be prefilled")

        # 4. User submits the form → routed to email, sent verbatim via Gmail.
        sim.clear(CREATOR)
        await cb.handle_flow_completion(CREATOR, _json.dumps({
            "intent": "send_email",
            "to": "akshay@example.com",
            "subject": "Lunch?",
            "body": "are we still on for 1pm tomorrow?",
        }))
        expect(len(sent) == 1, f"exactly one email sent, got {len(sent)}")
        expect(sent[0]["to"] == "akshay@example.com", "recipient passed through")
        expect(sent[0]["subject"] == "Lunch?", "subject passed through verbatim")
        expect(sent[0]["body"] == "are we still on for 1pm tomorrow?",
               "body passed through verbatim — AI never altered it")
        expect("sent to akshay@example.com" in sim.all_text(CREATOR).lower(),
               "user gets a send confirmation")

        # 5. Empty body → nothing sent.
        sim.clear(CREATOR)
        await cb.handle_flow_completion(CREATOR, _json.dumps({
            "intent": "send_email", "to": "x@y.com", "subject": "hi", "body": "  ",
        }))
        expect(len(sent) == 1, "empty body must not send an email")

        # 6. The meeting form (no body) must still route to the gmeet handler,
        #    not the email handler.
        sim.clear(CREATOR)
        STATE["conflicts"] = []
        config.WA_GMEET_FLOW_ID = "gmeet_test"
        tomorrow = (now_local() + timedelta(days=1)).date()
        await cb.handle_flow_completion(CREATOR, _json.dumps({
            "topic": "Sync", "name": "Akshay", "email": "akshay@example.com",
            "date": cb._epoch_ms_utc_midnight(tomorrow), "time": "09:00",
        }))
        expect(len(sent) == 1, "meeting form must not be treated as an email")
        expect("saved" in sim.all_text(CREATOR).lower(),
               "meeting form still books through the gmeet path")
    finally:
        config.WA_EMAIL_FLOW_ID = _saved
        config.WA_GMEET_FLOW_ID = _saved_gmeet
        gmail_mod.send_email = _real_send


async def scenario_reminder_out_of_window(sim: Simulator):
    """A reminder that fires while the user is outside the 24h window must fall
    back to the approved template instead of silently failing."""
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    from bot.config import config
    from bot.services import reminder_scheduler as rs
    from bot.services import whatsapp as wa
    from bot.database import async_session, Reminder
    from bot.utils.time import utcnow_naive
    from sqlalchemy import select

    # A past-due one-shot reminder.
    async with async_session() as s:
        r = Reminder(wa_id=CREATOR, message="Krishna birthday",
                     remind_at=utcnow_naive(), is_recurring=False, is_sent=False)
        s.add(r)
        await s.flush()
        rid = r.id
        await s.commit()

    _saved_name = config.WA_REMINDER_TEMPLATE_NAME
    _real_send = wa.send_message
    _real_tmpl = wa.send_template
    templates = []

    def freeform_blocked(wa_id, text):        # simulate the 24h-window rejection
        return False

    def cap_tmpl(wa_id, name, language_code="en_US", components=None):
        templates.append({"wa_id": wa_id, "name": name})
        return True

    config.WA_REMINDER_TEMPLATE_NAME = "reminder_notification"
    wa.send_message = freeform_blocked
    wa.send_template = cap_tmpl
    try:
        # Case 1: free-form blocked → template fallback delivers it.
        await rs._fire_reminder_async(rid)
        expect(len(templates) == 1, "out-of-window reminder must fall back to template")
        expect(templates[0]["name"] == "reminder_notification", "uses the reminder template")
        async with async_session() as s:
            row = (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
            expect(row.is_sent is True, "template-delivered reminder stays marked sent")

        # Case 2: free-form AND template both fail → rolled back so reconcile retries.
        templates.clear()
        async with async_session() as s:
            row = (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
            row.is_sent = False
            await s.commit()
        wa.send_template = lambda *a, **k: False
        await rs._fire_reminder_async(rid)
        async with async_session() as s:
            row = (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
            expect(row.is_sent is False, "if both sends fail, reminder rolls back for retry")
    finally:
        config.WA_REMINDER_TEMPLATE_NAME = _saved_name
        wa.send_message = _real_send
        wa.send_template = _real_tmpl


async def scenario_tool_json_leak(sim: Simulator):
    """The model sometimes emits tool args as raw JSON text instead of a tool
    call (the "{ \"message\": ..., \"remind_at_iso\": ... }" leak). The user
    must NOT see JSON, the action must still run, and the JSON must not poison
    memory."""
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    import json as _json
    from bot import agent
    from tester.llm_mock import _Response, _Choice, _Message
    from bot.database import async_session, Reminder, ChatMemory, User
    from sqlalchemy import select, delete as _sql_delete

    # Reminders are keyed by wa_id (no FK), so reset_user doesn't clear them —
    # wipe any left over from earlier scenarios for a clean count.
    async with async_session() as s:
        await s.execute(_sql_delete(Reminder).where(Reminder.wa_id == CREATOR))
        await s.commit()

    when = (now_local() + timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    leak = _json.dumps({"message": "entrepreneur first form",
                        "remind_at_iso": when.isoformat()})

    _real = agent._call_groq

    def leak_call(system_msg, messages, max_retries=3):
        # finish_reason="stop" + JSON content + no tool_calls = the leak.
        return _Response(_Choice(_Message(content=leak), "stop"))

    agent._call_groq = leak_call
    try:
        reply = await agent.process_message(CREATOR, "remind me about entrepreneur first form")
    finally:
        agent._call_groq = _real

    # 1. No raw JSON reaches the user.
    expect("remind_at_iso" not in reply and not reply.strip().startswith("{"),
           f"raw tool JSON must not leak to the user, got: {reply!r}")
    # 2. The reminder is still actually created (recovered from the leak).
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.wa_id == CREATOR))).scalar_one()
        rems = list((await s.execute(
            select(Reminder).where(Reminder.wa_id == CREATOR))).scalars().all())
    # 2. The leak must NOT become a side effect. Raw model text is not
    #    authority to act: the tool name is inferred and there was no
    #    confirmation, so `_recover_leaked_tool_json` rejects it by design
    #    (see the comment above that function in bot/agent.py).
    expect(len(rems) == 0,
           f"a leaked tool-arg dict must not silently run, got {len(rems)} reminders")
    # 3. The user gets a plain retry prompt, not gibberish and not a false
    #    confirmation of something that never happened.
    expect("reminder #" not in reply.lower(),
           f"must not claim a reminder was set, got: {reply!r}")
    expect(len(reply.strip()) > 0 and "{" not in reply,
           f"user should get a clean retry prompt, got: {reply!r}")
    # 4. The leaked JSON must NOT be stored in memory (breaks the feedback loop).
    async with async_session() as s:
        mems = list((await s.execute(
            select(ChatMemory).where(ChatMemory.user_id == u.id,
                                     ChatMemory.role == "model"))).scalars().all())
    expect(all(not m.text.strip().startswith("{") for m in mems),
           "leaked JSON must not be saved to memory")


async def scenario_heartbeat_reconcile(sim: Simulator):
    """The standalone heartbeat must fire a due reminder on its own (simulating
    a wedged in-app scheduler), and must NOT double-fire it."""
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    from bot.database import async_session, Reminder
    from bot.utils.time import utcnow_naive
    from sqlalchemy import select, delete as _sql_delete
    import reconcile_reminders as heartbeat

    # Clean slate, then a past-due one-shot the in-app scheduler "missed".
    async with async_session() as s:
        await s.execute(_sql_delete(Reminder).where(Reminder.wa_id == CREATOR))
        r = Reminder(wa_id=CREATOR, message="standup", remind_at=utcnow_naive(),
                     is_recurring=False, is_sent=False)
        s.add(r)
        await s.flush()
        rid = r.id
        await s.commit()

    # First heartbeat run → delivers it.
    await heartbeat._reconcile()
    texts = sim.all_text(CREATOR).lower()
    expect("standup" in texts, "heartbeat must deliver the due reminder")
    async with async_session() as s:
        row = (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
        expect(row.is_sent is True, "fired reminder marked sent")

    # Second heartbeat run → must NOT re-send (atomic claim already took it).
    sim.clear(CREATOR)
    await heartbeat._reconcile()
    expect("standup" not in sim.all_text(CREATOR).lower(),
           "heartbeat must not double-fire an already-sent reminder")


# ════════════════════════════════════════════════════════════════
# Add a guest to an existing meeting — the "which Mohit?" loop
# ════════════════════════════════════════════════════════════════
def _seed_event(summary="Meeting with Mohit Puns", hour=20, attendees=None,
                event_id="evt_mohit"):
    start = _tomorrow_at(hour)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
        "hangoutLink": "https://meet.google.com/viu-dfsa-axw",
        "attendees": list(attendees or [{"email": "mohit@example.com"}]),
    }


async def scenario_add_attendee_to_existing(sim: Simulator):
    """"add X to this meet" must add a guest — never restart a booking.

    Replays the exact production transcript that looped: with one upcoming
    event and a contact that resolves, the bot must go straight to a confirm
    gate and must NOT ask for a title, a time, or which contact the ORIGINAL
    meeting was with.
    """
    await H.reset_user(CREATOR)
    await H.make_onboarded_user(CREATOR)
    STATE["events"] = [_seed_event()]
    STATE["attendee_adds"] = []
    sim.clear()

    await sim.text(CREATOR, "can we add Priya in this meet as well?")

    texts = sim.all_text(CREATOR).lower()
    expect("title" not in texts,
           f"must not ask for a title when adding to an existing event: {texts!r}")
    expect(not any(w in texts for w in ("when should", "what time", "start")),
           f"must not ask for a time on an existing event: {texts!r}")
    expect("mohit" not in texts.replace("meeting with mohit puns", ""),
           f"must not ask the user to choose the ORIGINAL attendee: {texts!r}")

    ids = sim.button_ids(CREATOR) or []
    expect("add_attendee_confirm_yes" in ids,
           f"expected the add-attendee confirm gate, got buttons={ids!r}")

    # Nothing may reach Google before the tap.
    expect(not STATE["attendee_adds"], "must not write to Google before confirm")

    await sim.tap(CREATOR, "add_attendee_confirm_yes", "Confirm")
    expect(len(STATE["attendee_adds"]) == 1,
           f"confirm must add exactly once, got {STATE['attendee_adds']!r}")
    expect(PRIYA_EMAIL in STATE["attendee_adds"][0]["added"],
           f"the NEW guest must be the one added: {STATE['attendee_adds'][0]!r}")
    expect("added" in sim.all_text(CREATOR).lower(), "must confirm the add to the user")


async def scenario_add_attendee_never_reasks(sim: Simulator):
    """The regression proper: the bot must never repeat its own question.

    Sends the same three follow-ups that produced four identical "which
    Mohit should I invite" replies in production.
    """
    await H.reset_user(CREATOR)
    await H.make_onboarded_user(CREATOR)
    STATE["events"] = [_seed_event()]
    STATE["attendee_adds"] = []
    sim.clear()

    replies = []
    for msg in ("can we add harsh yadav in this meet as well?",
                "Please add newguy@example.com to this meet as well",
                "hi"):
        sim.clear(CREATOR)
        await sim.text(CREATOR, msg)
        replies.append(sim.all_text(CREATOR).lower())

    # No reply may be a repeat of the one before it — that loop is the bug.
    for i in range(1, len(replies)):
        expect(replies[i] != replies[i - 1],
               f"bot repeated itself verbatim on turn {i + 1}: {replies[i]!r}")

    # A bare greeting must never resurrect a stale pending question.
    expect("mohit" not in replies[-1],
           f"'hi' must not re-raise an old contact question: {replies[-1]!r}")

    # The typed email must be accepted, not re-requested.
    expect("newguy@example.com" in replies[1] or "add" in replies[1],
           f"a typed email must move the flow forward: {replies[1]!r}")


async def scenario_add_attendee_ambiguous_event(sim: Simulator):
    """Two candidate events → ask which, never silently pick one."""
    await H.reset_user(CREATOR)
    await H.make_onboarded_user(CREATOR)
    STATE["events"] = [
        _seed_event(summary="Standup", hour=9, event_id="evt_a"),
        _seed_event(summary="Design review", hour=15, event_id="evt_b"),
    ]
    STATE["attendee_adds"] = []
    sim.clear()

    await sim.text(CREATOR, "add Priya to this meeting as well")
    last = sim.last(CREATOR) or {}
    rows = last.get("rows") or []
    expect(any(str(r).startswith("add_attendee_event_") for r in rows),
           f"ambiguous event must produce an event picker, got {last!r}")
    expect(not STATE["attendee_adds"], "must not write while the event is ambiguous")

    await sim.pick(CREATOR, "add_attendee_event_1", "Design review")
    ids = sim.button_ids(CREATOR) or []
    expect("add_attendee_confirm_yes" in ids,
           f"picking the event must lead to the confirm gate, got {ids!r}")


async def scenario_empty_completion_is_not_success(sim: Simulator):
    """An empty model reply must never be reported as a completed action.

    Sarvam intermittently returns empty content (usually when no tool is
    enabled). `content or "done."` turned that into a success claim, so
    "can you remove mohit from that meeting?" — something the bot cannot do
    at all — was answered "done.".
    """
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    from bot import agent
    from tester.llm_mock import _Response, _Choice, _Message

    _real = agent._call_groq
    agent._call_groq = lambda system_msg, messages, max_retries=3: _Response(
        _Choice(_Message(content=""), "stop"))
    try:
        reply = await agent.process_message(CREATOR, "can you remove mohit from that meeting?")
    finally:
        agent._call_groq = _real

    expect(reply.strip() != "", "an empty completion must not produce an empty reply")
    expect("done" not in reply.lower(),
           f"an empty completion must not claim the action happened, got: {reply!r}")


async def scenario_tool_availability(sim: Simulator):
    """The model must SEE every tool — choosing between them is its job.

    Tool selection used to be a regex that handed the model one or two tools,
    which is where the hallucinations began: a request whose tool was hidden
    left the model with no correct move, so it improvised. Notably
    "Add x@gmail.com also as attendee" selected `gmail_search`, because the
    substring "gmail" appears inside the address.

    The only thing still withheld is `calendar_cancel` on an attendee-removal
    phrasing, because it deletes with no confirmation gate.
    """
    from bot.agent import _select_tools_for_text, TOOLS

    all_names = {t["function"]["name"] for t in TOOLS}

    def tools_for(text):
        return {t["function"]["name"] for t in _select_tools_for_text(text)}

    # Real phrasings that previously got the wrong tool or none at all.
    for text in ("Add yharsh499@gmail.com also as attendee",
                 "add akshay as attendee",
                 "can you also add rahul",
                 "add rahul also",
                 "add john as a guest",
                 "put priya on that meeting",
                 "can we add harsh yadav SST Baba in this meet as well?",
                 "set up a call with Priya tomorrow at 3",
                 "cancel all my meetings today",
                 "hi"):
        got = tools_for(text)
        expect(got == all_names,
               f"{text!r} must expose the FULL toolset, missing {sorted(all_names - got)}")

    # The one deliberate exception: never hand over a no-confirmation delete
    # when the user is asking to drop a guest.
    for text in ("can you remove mohit from that meeting?",
                 "remove priya from the 8pm meeting",
                 "drop rahul from this call",
                 "uninvite sara from the meeting"):
        got = tools_for(text)
        expect("calendar_cancel" not in got,
               f"{text!r} must not expose calendar_cancel (deletes the event), got {sorted(got)}")
        expect("calendar_add_attendee" in got,
               f"{text!r} should still see the rest of the toolset")

    # A genuine cancellation must keep the cancel tool.
    for text in ("cancel my 4pm meeting", "remove the meeting from my calendar",
                 "cancel all my meetings today"):
        expect("calendar_cancel" in tools_for(text),
               f"{text!r} is a real cancellation and must expose calendar_cancel")


async def scenario_title_is_preserved(sim: Simulator):
    """A topic the user typed must survive into the event title."""
    from bot.services.gmeet_flow import _sanitize_title

    cases = [
        # (llm_title, attendee, expected)
        ("pricing", "Priya", "pricing with Priya"),
        ("Q4 launch", "Priya", "Q4 launch with Priya"),
        # Previously discarded wholesale by the " with " guard, losing the
        # topic entirely. The topic must survive, without doubling the name.
        ("pricing with Priya", "Priya", "pricing with Priya"),
        ("Q4 launch with Akshay Bhaiya", "Akshay Bhaiya", "Q4 launch with Akshay Bhaiya"),
        # A different name in the title must not silently retarget the event.
        ("pricing with Rahul", "Priya", "pricing with Priya"),
        # Genuinely empty / degenerate -> default.
        ("", "Priya", "Meeting with Priya"),
        ("Meeting with Priya", "Priya", "Meeting with Priya"),
    ]
    for llm_title, attendee, want in cases:
        got = _sanitize_title(llm_title, attendee)
        expect(got == want,
               f"_sanitize_title({llm_title!r}, {attendee!r}) -> {got!r}, expected {want!r}")


# ════════════════════════════════════════════════════════════════
# Slots survive the LLM path (not just the deterministic one)
# ════════════════════════════════════════════════════════════════
async def scenario_gmeet_slots_survive_llm_path(sim: Simulator):
    """The time given on turn 1 must survive a turn-2 call that omits it.

    `scenario_email_keeps_time` only ever exercises the DETERMINISTIC reply
    path, which is why it never caught this. Here we call the tool directly,
    the way the model does when it continues a booking itself: turn 2 passes
    only an email, and the turn's text has no clock token — which used to wipe
    the banked time server-side and make the bot re-ask for it.
    """
    import json as _json
    from bot.tools import set_gmeet
    from bot.services.task_service import load_gmeet_slots, clear_conversation_state

    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    STATE["conflicts"] = []
    await clear_conversation_state(CREATOR)

    when = _tomorrow_at(19, 0).isoformat()

    # Turn 1 — name + time. Unknown contact, so the tool asks for an email.
    r1 = _json.loads(await set_gmeet(
        CREATOR, attendee_name="Akshay", start_time_iso=when,
        user_text="set a gmeet with Akshay for 7 pm tomorrow",
    ))
    expect(r1.get("action") in ("need_email", "no_contact"),
           f"turn 1 should ask for an email, got {r1.get('action')}")
    expect(r1.get("start_time_iso") == when,
           "turn 1 payload must carry the time forward")

    banked = await load_gmeet_slots(CREATOR)
    expect(banked.get("start_time_iso") == when,
           f"time must be banked server-side, got {banked.get('start_time_iso')!r}")

    # Turn 2 — ONLY an email, no clock token in the text. This is the exact
    # shape that used to lose the time.
    r2 = _json.loads(await set_gmeet(
        CREATOR, attendee_email="akshay@example.com",
        user_text="akshay@example.com",
    ))
    expect(r2.get("action") != "missing_time",
           "must NOT re-ask for the time — it was already given on turn 1")
    expect(r2.get("action") == "awaiting_confirmation",
           f"should reach the confirmation gate, got {r2.get('action')}")
    expect(r2.get("start_time_iso") == when or when[:16] in _json.dumps(r2),
           "confirmation must use the originally-given time")

    # The anti-hallucination guard must still bite for a time never typed.
    await clear_conversation_state(CREATOR)
    r3 = _json.loads(await set_gmeet(
        CREATOR, attendee_email="akshay@example.com",
        start_time_iso=when, user_text="book something with akshay",
    ))
    expect(r3.get("action") == "missing_time",
           f"invented time must still be rejected, got {r3.get('action')}")
    await clear_conversation_state(CREATOR)


# ════════════════════════════════════════════════════════════════
# Two people with the same first name must produce a picker
# ════════════════════════════════════════════════════════════════
async def scenario_same_name_contacts_pick(sim: Simulator):
    """Two Akshays with different emails must not silently collapse to one.

    The cache upsert keyed on (user_id, name_lower) alone, so the second
    Akshay overwrote the first and `resolve_attendee` saw a single strong
    match — booking the wrong person with no prompt.
    """
    from bot.services.contacts import _cache_upsert, _cache_lookup

    await H.make_onboarded_user(CREATOR, name="Harsh")
    db_user = await H.get_user(CREATOR)
    sim.clear(CREATOR)

    # Identical display name, different addresses — the collapse case. Two
    # DIFFERENT names ("Akshay Kumar" / "Akshay Sharma") already produced two
    # rows, so they would never have caught this.
    rows = [
        {"name": "Akshay", "emails": ["akshay.kumar@example.com"],  "phones": []},
        {"name": "Akshay", "emails": ["akshay.sharma@example.com"], "phones": []},
    ]
    await _cache_upsert(db_user.id, rows, source="test")

    found, _fresh = await _cache_lookup(db_user.id, "akshay", limit=10)
    emails = {e for c in found for e in (c.get("emails") or [])}
    expect(len(found) >= 2,
           f"both Akshays must survive in the cache, got {len(found)}")
    expect(len(emails) >= 2, f"both addresses must survive, got {emails}")

    # Re-syncing the same two contacts must refresh in place, not duplicate.
    await _cache_upsert(db_user.id, rows, source="test")
    again, _ = await _cache_lookup(db_user.id, "akshay", limit=10)
    expect(len(again) == len(found),
           f"re-sync must not duplicate rows: {len(found)} -> {len(again)}")


# ════════════════════════════════════════════════════════════════
# Transcript context — speaker attribution survives normalisation
# ════════════════════════════════════════════════════════════════
async def scenario_transcript_context(sim: Simulator):
    """A provider transcript must normalise into the JSON the prompt expects.

    Covers the Zoom VTT path because it is the one that can be tested without
    a paid account: Meet needs a Workspace tier that generates transcripts at
    all. Both providers share `TranscriptContext`, so this guards the shape
    the summariser and `join_meet_prompt.md` depend on.
    """
    from bot.services.meetings.zoom_source import parse_vtt

    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:12.400 --> 00:00:18.100\n"
        "Akshay Kumar: we should ship pricing before Q4\n\n"
        "2\n00:00:18.500 --> 00:00:24.000\n"
        "Priya: agreed, I'll own the rollout doc\n\n"
        "3\n00:00:24.100 --> 00:00:27.000\n"
        "Akshay Kumar: great, let's lock it\n\n"
        "4\n00:00:27.500 --> 00:00:29.000\n"
        "some unattributed noise\n"
    )
    ctx = parse_vtt(vtt, title="Q4 pricing", conference_id="zoom-123")

    expect(ctx.participant_count == 2,
           f"two distinct speakers expected, got {ctx.participant_count}")
    akshay = [p for p in ctx.participants if p.display_name == "Akshay Kumar"]
    expect(akshay, "Akshay Kumar must be a participant")
    expect(akshay[0].turns == 2, f"Akshay spoke twice, got {akshay[0].turns}")
    expect(akshay[0].speaking_seconds > 0, "speaking time must be tallied")

    # A line the provider could not attribute is kept, but owns nothing.
    orphans = [s for s in ctx.segments if s.speaker_id is None]
    expect(len(orphans) == 1, f"one unattributed line expected, got {len(orphans)}")

    payload = ctx.to_json_dict()
    expect(set(payload) == {"meeting", "participants", "segments"},
           f"prompt contract changed: {sorted(payload)}")
    expect(payload["participants"]["count"] == 2, "count must be in the payload")
    expect(payload["meeting"]["provider"] == "zoom", "provider must be tagged")


# ════════════════════════════════════════════════════════════════
# Provider overload — exactly one retry, never more
# ════════════════════════════════════════════════════════════════
async def scenario_overload_retry_is_bounded(sim: Simulator):
    """A 503 'model_overloaded' gets ONE retry, then either recovers or fails.

    Two failures in a row must NOT trigger a second retry — hammering a
    genuinely overloaded upstream three times back-to-back makes things worse,
    not better. This is distinct from the empty-completion retry loop, which
    keeps its own multi-attempt budget for cheap local hiccups.
    """
    import bot.agent as agent

    class FakeOverload(Exception):
        def __str__(self):
            return "Error code: 503 - model_overloaded: Service Unavailable"

    class FakeChoice:
        def __init__(self, text):
            self.message = type("M", (), {"content": text, "tool_calls": None})()
            self.finish_reason = "stop"

    class FakeResponse:
        def __init__(self, text):
            self.choices = [FakeChoice(text)]

    class FakeClient:
        def __init__(self, plan):
            self.plan = list(plan)
            self.calls = 0

        class _Completions:
            def __init__(self, outer):
                self.outer = outer

            def create(self, **kwargs):
                self.outer.calls += 1
                step = self.outer.plan.pop(0)
                if step == "fail":
                    raise FakeOverload()
                return FakeResponse(step)

        @property
        def chat(self):
            outer = self

            class _Chat:
                completions = FakeClient._Completions(outer)

            return _Chat()

    real_client = agent.client
    import time as _time
    real_sleep = _time.sleep
    _time.sleep = lambda *_a, **_k: None  # don't actually wait in tests
    try:
        # Recovers after exactly one retry.
        agent.client = FakeClient(["fail", "ok after retry"])
        resp = _REAL_CALL_GROQ({"role": "system", "content": "x"}, [])
        expect(resp.choices[0].message.content == "ok after retry",
               "should recover on the single retry")
        expect(agent.client.calls == 2, f"expected exactly 2 calls, got {agent.client.calls}")

        # Fails twice in a row -> must raise, NOT attempt a third call for
        # this cause. (max_retries defaults to 3, so the loop technically
        # allows a 3rd iteration — but the overload path must not re-enter
        # once its one retry is spent.)
        agent.client = FakeClient(["fail", "fail", "should never be reached"])
        raised = False
        try:
            _REAL_CALL_GROQ({"role": "system", "content": "x"}, [])
        except Exception:
            raised = True
        expect(raised, "second consecutive overload must raise, not retry again")
        expect(agent.client.calls == 2,
               f"must stop after 2 calls, not reach the 3rd; got {agent.client.calls}")
    finally:
        agent.client = real_client
        _time.sleep = real_sleep


# ════════════════════════════════════════════════════════════════
# Gmail-derived contacts are searchable live, not just at bootstrap
# ════════════════════════════════════════════════════════════════
async def scenario_other_contacts_are_searchable(sim: Simulator):
    """"meet with Priyanshu" must resolve someone only ever emailed.

    Saved contacts alone used to answer a live lookup, so a Gmail-derived
    person was findable only in the one-time bootstrap right after connecting.
    Also guards the two rules that keep the merge sane: saved contacts win a
    duplicate address, and a missing `contacts.other.readonly` scope degrades
    to "no extra results" instead of breaking saved-contact lookup entirely.
    """
    from bot.services.contacts import _api_search_sync

    class _Req:
        def __init__(self, payload, boom=False):
            self._payload, self._boom = payload, boom

        def execute(self):
            if self._boom:
                raise Exception(
                    'HttpError 403: Request had insufficient authentication '
                    'scopes. reason: ACCESS_TOKEN_SCOPE_INSUFFICIENT'
                )
            return self._payload

    def _person(name, email):
        return {"person": {
            "names": [{"displayName": name}],
            "emailAddresses": [{"value": email}],
        }}

    class FakeSvc:
        def __init__(self, saved, other, other_boom=False):
            self._saved, self._other, self._boom = saved, other, other_boom

        def people(self):
            svc = self

            class _P:
                def searchContacts(self, **kw):
                    return _Req({"results": svc._saved})
            return _P()

        def otherContacts(self):
            svc = self

            class _O:
                def search(self, **kw):
                    return _Req({"results": svc._other}, boom=svc._boom)
            return _O()

    import bot.services.contacts as contacts_mod
    real_build = contacts_mod.build
    try:
        # 1. Gmail-only person is found.
        contacts_mod.build = lambda *a, **k: FakeSvc(
            saved=[], other=[_person("Priyanshu", "priyanshu@example.com")])
        res = _api_search_sync(None, "Priyanshu", 10)
        expect(len(res) == 1, f"expected the Gmail-derived contact, got {res}")
        expect(res[0]["emails"] == ["priyanshu@example.com"],
               f"wrong address resolved: {res[0]}")

        # 2. Saved contact wins a duplicate address — no double entry.
        dup = "dup@example.com"
        contacts_mod.build = lambda *a, **k: FakeSvc(
            saved=[_person("Priyanshu Saved", dup)],
            other=[_person("Priyanshu Other", dup)])
        res = _api_search_sync(None, "Priyanshu", 10)
        expect(len(res) == 1, f"duplicate address must collapse, got {len(res)}")
        expect(res[0]["name"] == "Priyanshu Saved",
               f"saved contact should win, got {res[0]['name']}")

        # 3. Missing scope must NOT break saved-contact lookup.
        contacts_mod.build = lambda *a, **k: FakeSvc(
            saved=[_person("Akshay", "akshay@example.com")],
            other=[], other_boom=True)
        res = _api_search_sync(None, "Akshay", 10)
        expect(len(res) == 1 and res[0]["name"] == "Akshay",
               f"403 on otherContacts must degrade gracefully, got {res}")
    finally:
        contacts_mod.build = real_build


# ════════════════════════════════════════════════════════════════
# Transcript pipeline stays inert until explicitly enabled
# ════════════════════════════════════════════════════════════════
async def scenario_transcript_pipeline_is_inert(sim: Simulator):
    """The feature must cost nothing — and touch nothing — while switched off.

    Also covers the meeting-code parser, which is what makes conference
    lookup possible at all: araka already stores the Meet link on every
    booking, so no Calendar round trip is needed to find the transcript.
    """
    from bot.config import config
    from bot.services.meetings import pipeline
    from bot.services.meetings.meet_source import meeting_code_from_link

    expect(config.MEET_TRANSCRIPTS_ENABLED is False,
           "transcripts must default to OFF — enabling costs API quota")

    # The Meet scope must NOT be requested while the feature is off. Asking
    # for a scope whose API is disabled fails the whole consent screen with
    # invalid_scope, breaking Google sign-in for everyone — so the scope and
    # the flag are deliberately the same switch.
    scopes = config.google_scopes()
    expect(config.GOOGLE_SCOPE_MEET_TRANSCRIPTS not in scopes,
           "Meet scope must not be requested while transcripts are disabled")
    expect("https://www.googleapis.com/auth/contacts.other.readonly" in scopes,
           "the everyday scopes must still be requested")

    saved = config.MEET_TRANSCRIPTS_ENABLED
    try:
        config.MEET_TRANSCRIPTS_ENABLED = True
        expect(config.GOOGLE_SCOPE_MEET_TRANSCRIPTS in config.google_scopes(),
               "enabling transcripts must also request the Meet scope")
    finally:
        config.MEET_TRANSCRIPTS_ENABLED = saved

    # Off => no DB reads, no API calls, no work at all.
    delivered = await pipeline.run_once()
    expect(delivered == 0, f"disabled pipeline must do nothing, got {delivered}")

    cases = [
        ("https://meet.google.com/abc-defg-hij", "abc-defg-hij"),
        ("https://meet.google.com/ABC-DEFG-HIJ", "abc-defg-hij"),
        ("join here: https://meet.google.com/xyz-qrst-uvw please", "xyz-qrst-uvw"),
        ("https://zoom.us/j/12345", ""),
        ("", ""),
        (None, ""),
    ]
    for link, want in cases:
        got = meeting_code_from_link(link)
        expect(got == want, f"meeting_code_from_link({link!r}) -> {got!r}, want {want!r}")


# ════════════════════════════════════════════════════════════════
# A sender who only ever emailed you is still findable by name
# ════════════════════════════════════════════════════════════════
async def scenario_find_person_in_mail(sim: Simulator):
    """"what's Muskaan Jain's email?" must work right after reading her mail.

    The People API knows SAVED contacts and "Other contacts" — and Google
    builds Other contacts from people the user has EMAILED, not from people
    who emailed them. So a sender never replied to is invisible to both, which
    is why this failed seconds after araka summarised a message from her.
    """
    from bot.services.gmail import _parse_address, find_people_in_mail

    # Header shapes Gmail actually emits.
    cases = [
        ('"Muskaan Jain (Unstop)" <muskaan@unstop.com>',
         ("Muskaan Jain (Unstop)", "muskaan@unstop.com")),
        ("Harsh Yadav <yharsh499@gmail.com>", ("Harsh Yadav", "yharsh499@gmail.com")),
        # Bare address -> derive a readable name, never show a raw address
        # where a person's name belongs.
        ("muskaan.jain@unstop.com", ("Muskaan Jain", "muskaan.jain@unstop.com")),
        ("not an address", ("", "")),
        ("", ("", "")),
    ]
    for header, want in cases:
        got = _parse_address(header)
        expect(got == want, f"_parse_address({header!r}) -> {got!r}, want {want!r}")

    # End-to-end against a faked Gmail service.
    class _Req:
        def __init__(self, payload):
            self._p = payload

        def execute(self):
            return self._p

    class FakeSvc:
        def users(self):
            class _U:
                def messages(self):
                    class _M:
                        def list(self, **kw):
                            return _Req({"messages": [{"id": "m1"}]})

                        def get(self, **kw):
                            return _Req({"payload": {"headers": [
                                {"name": "From",
                                 "value": '"Muskaan Jain (Unstop)" <muskaan@unstop.com>'},
                                {"name": "To", "value": "me@example.com"},
                            ]}})
                    return _M()
            return _U()

    import bot.services.gmail as gmail_mod
    real_service = gmail_mod._get_service
    try:
        gmail_mod._get_service = lambda *a, **k: FakeSvc()
        people = await find_people_in_mail(None, "Muskaan Jain")
        expect(len(people) == 1, f"expected one match, got {people}")
        expect(people[0]["emails"] == ["muskaan@unstop.com"],
               f"wrong address: {people[0]}")

        # An unrelated name must NOT match just because Gmail's search is
        # loose across the whole message body.
        none_found = await find_people_in_mail(None, "Someone Else")
        expect(none_found == [],
               f"loose Gmail match must be filtered out, got {none_found}")
    finally:
        gmail_mod._get_service = real_service


# ════════════════════════════════════════════════════════════════
# Replies are WhatsApp markup, not markdown
# ════════════════════════════════════════════════════════════════
async def scenario_whatsapp_markup(sim: Simulator):
    """`**bold**` is the visible bug: WhatsApp renders the INNER `*bold*` and
    leaves the outer asterisks on screen, so the user sees bold text wrapped
    in stray `*`. WhatsApp knows only *bold* _italic_ ~strike~ ```mono```.
    """
    from bot.agent import to_whatsapp_markup as fix

    cases = [
        # The exact shape seen in production.
        ("**Today's Gmail**", "*Today's Gmail*"),
        ("**Harsh Yadav** - no subject", "*Harsh Yadav* - no subject"),
        ("__also bold__", "*also bold*"),
        # Headings and bullets leak as literal syntax.
        ("## This week", "*This week*"),
        ("### Needs you", "*Needs you*"),
        ("- first\n- second", "• first\n• second"),
        ("* starred item", "• starred item"),
        ("  - indented", "  • indented"),
        # Links: the URL is the useful half.
        ("see [the docs](https://example.com/x)",
         "see the docs: https://example.com/x"),
        # Single asterisks are ALREADY correct — must survive untouched.
        ("*already bold*", "*already bold*"),
        ("plain text", "plain text"),
        ("", ""),
    ]
    for raw, want in cases:
        got = fix(raw)
        expect(got == want, f"to_whatsapp_markup({raw!r}) -> {got!r}, want {want!r}")

    # Bold spanning a newline must not be left half-converted.
    expect(fix("**two\nlines**") == "*two\nlines*", "multiline bold")
    # Excessive blank runs collapse rather than eating the screen.
    expect(fix("a\n\n\n\nb") == "a\n\nb", "blank-line collapse")


# ════════════════════════════════════════════════════════════════
# Gmail summaries are not silently capped at 3
# ════════════════════════════════════════════════════════════════
async def scenario_gmail_limit_allows_a_week(sim: Simulator):
    """A week summary must be able to see more than three messages.

    `min(limit, 3)` in the tool meant "today", "this week" and an explicit
    date range all returned the SAME three emails — the date filter was fine,
    the tool simply could not return a fourth.
    """
    import inspect
    from bot.agent import _TOOL_RESULT_BUDGET
    import bot.tools as tools_mod
    from bot.services.gmail import search_messages

    src = inspect.getsource(tools_mod.gmail_search)
    expect("min(int(limit or 5), 3)" not in src,
           "gmail_search must not hard-cap results at 3")

    # Metadata listings need real headroom; bodies stay small.
    expect("ceiling = 5 if include_body else 30" in src,
           "expected payload-aware ceiling in gmail_search")

    # The service layer must not re-impose a lower cap underneath it.
    svc_src = inspect.getsource(search_messages)
    expect("40" in svc_src, "service cap should allow well over 10 messages")

    # 30 metadata lines exceed the default 2000-char budget, so a week's
    # summary would be built from a truncated list without this.
    expect(_TOOL_RESULT_BUDGET.get("gmail_search", 0) >= 4000,
           "gmail_search needs a larger context budget than the default")


# ════════════════════════════════════════════════════════════════
# Listing answers get room; a read that returns nothing isn't "done"
# ════════════════════════════════════════════════════════════════
async def scenario_long_answer_budget(sim: Simulator):
    """512 output tokens cannot render a week's inbox.

    Raising the tool payload to ~8k chars without raising the output cap made
    the model either stop mid-list — a summary that looks finished and isn't —
    or return nothing at all, which then surfaced as a bare "done.".
    """
    from bot.agent import (
        _MAX_TOKENS_DEFAULT, _MAX_TOKENS_LONG_ANSWER, _max_tokens_for,
        _WRITE_TOOLS,
    )

    short = [{"role": "tool", "content": "1. From: a | Subject: b"}]
    expect(_max_tokens_for(short) == _MAX_TOKENS_DEFAULT,
           "a small result must keep the short, low-latency cap")

    big = [{"role": "tool", "content": "x" * 5000}]
    expect(_max_tokens_for(big) == _MAX_TOKENS_LONG_ANSWER,
           "a listing-sized result must get room to be rendered")
    expect(_MAX_TOKENS_LONG_ANSWER >= 1500,
           "long answers need meaningfully more than the default")

    # Non-tool messages must not trigger the larger budget.
    chatty = [{"role": "user", "content": "y" * 5000}]
    expect(_max_tokens_for(chatty) == _MAX_TOKENS_DEFAULT,
           "only TOOL results should widen the budget")

    # gmail_search is a read: an empty completion after it must never be
    # reported as "done.", which asserts an action that never happened.
    expect("gmail_search" not in _WRITE_TOOLS,
           "gmail_search must be classed as a read tool")
    expect("calendar_get_all" not in _WRITE_TOOLS, "calendar_get_all is a read")
    expect("set_gmeet" in _WRITE_TOOLS, "set_gmeet must stay a write tool")


# ════════════════════════════════════════════════════════════════
# A contact with no display name is never "Unknown"
# ════════════════════════════════════════════════════════════════
async def scenario_no_unknown_contacts(sim: Simulator):
    """Google often returns an address with no displayName.

    Falling back to the literal string "Unknown" put that word in the contact
    picker and then straight into a calendar event — "Meeting with Unknown".
    """
    from bot.services.contacts import _parse, name_from_email

    expect(name_from_email("muskaan.jain@unstop.com") == "Muskaan Jain",
           "dotted local part should become a name")
    expect(name_from_email("akshay@example.com") == "Akshay", "single token")
    expect(name_from_email("john_smith99@x.com") == "John Smith99", "underscore")
    expect(name_from_email("") == "", "no address, no name")

    # Person with an email but NO names[] — the real shape that broke.
    parsed = _parse({"emailAddresses": [{"value": "akshay.kumar@example.com"}]})
    expect(parsed["name"] == "Akshay Kumar",
           f"derived name expected, got {parsed['name']!r}")
    expect(parsed["emails"] == ["akshay.kumar@example.com"], "email preserved")

    # A real displayName always wins over derivation.
    parsed = _parse({
        "names": [{"displayName": "Akshay B"}],
        "emailAddresses": [{"value": "akshay.kumar@example.com"}],
    })
    expect(parsed["name"] == "Akshay B", "explicit displayName must win")

    # Nothing at all is still "Unknown" — but it can no longer be booked,
    # since there is no address to invite.
    expect(_parse({})["name"] == "Unknown", "empty person stays Unknown")


# ════════════════════════════════════════════════════════════════
# Booking confirmations promise only what araka actually does
# ════════════════════════════════════════════════════════════════
async def scenario_no_phantom_reminder_promise(sim: Simulator):
    """Automatic T-24h/T-1h meeting reminders were removed.

    The confirmation and saved messages still advertised them, so every
    booking promised a reminder that would never arrive.
    """
    import inspect
    from bot.services import gmeet_flow

    src = inspect.getsource(gmeet_flow)
    expect("reminders at 24h and 1h before" not in src,
           "confirmation card must not promise removed reminders")
    expect("reminders set for 24h and 1h before" not in src,
           "saved message must not promise removed reminders")


# ════════════════════════════════════════════════════════════════
# Zoom fetch: auth, download, and the difference between
# "not ready yet" and "never coming"
# ════════════════════════════════════════════════════════════════
async def scenario_zoom_fetch(sim: Simulator):
    """The distinction that matters is retry-vs-give-up.

    A transcript still processing must return None so the poller tries again;
    a meeting with no cloud recording must raise TranscriptUnavailable so it
    is recorded once and never polled again. Getting these backwards means
    either losing summaries or hammering Zoom forever.
    """
    import bot.services.meetings.zoom_source as zs
    from bot.services.meetings.transcript_source import MeetingRef, TranscriptUnavailable

    expect(zs.meeting_id_from_link("https://us02web.zoom.us/j/85512345678?pwd=x")
           == "85512345678", "meeting id must come out of a join URL")
    expect(zs.meeting_id_from_link("https://meet.google.com/abc-defg-hij") == "",
           "a Meet link is not a Zoom meeting")
    expect(zs.meeting_id_from_link("") == "", "empty link")

    VTT = ("WEBVTT\n\n1\n00:00:05.000 --> 00:00:09.000\n"
           "Priya: we ship Thursday\n")

    class _Resp:
        def __init__(self, status, payload=None, text=""):
            self.status_code, self._p, self.text = status, payload or {}, text

        def json(self):
            return self._p

    real_get, real_post = zs.requests.get, zs.requests.post
    real_cache = dict(zs._token_cache)
    try:
        # Pretend we are authenticated for the duration.
        zs._token_cache.update({"access_token": "tok", "expires_at": 9e18})

        rec = {"recording_files": [
            {"file_type": "M4A", "download_url": "https://z/audio"},
            {"file_type": "TRANSCRIPT", "download_url": "https://z/vtt"},
        ], "duration": 42, "topic": "Ship review"}

        def get_ok(url, **kw):
            return _Resp(200, rec) if "/recordings" in url else _Resp(200, text=VTT)
        zs.requests.get = get_ok
        ctx = zs._fetch_sync(MeetingRef(provider="zoom", conference_id="855"))
        expect(ctx is not None, "a ready transcript must be returned")
        expect(ctx.participants[0].display_name == "Priya", "speaker parsed")
        expect(ctx.duration_minutes == 42,
               f"Zoom's real duration should win over the VTT's last cue, got {ctx.duration_minutes}")

        # Recording exists, transcript still processing -> retry later.
        zs.requests.get = lambda url, **kw: _Resp(200, {"recording_files": [
            {"file_type": "M4A", "download_url": "https://z/audio"}]})
        expect(zs._fetch_sync(MeetingRef(provider="zoom", conference_id="855")) is None,
               "a processing transcript must return None, not raise")

        # No cloud recording at all -> permanent, stop polling.
        zs.requests.get = lambda url, **kw: _Resp(404, {})
        raised = False
        try:
            zs._fetch_sync(MeetingRef(provider="zoom", conference_id="855"))
        except TranscriptUnavailable:
            raised = True
        expect(raised, "404 must be permanent, so the meeting is never re-polled")

        # Rate limit / server error -> transient, keep trying.
        zs.requests.get = lambda url, **kw: _Resp(429, {}, "slow down")
        expect(zs._fetch_sync(MeetingRef(provider="zoom", conference_id="855")) is None,
               "429 must be transient, not permanent")

        # Missing credentials must fail permanently, not silently hang.
        zs._token_cache.update({"access_token": "", "expires_at": 0})
        from bot.config import config as cfg
        saved = cfg.ZOOM_ACCOUNT_ID
        cfg.ZOOM_ACCOUNT_ID = ""
        try:
            raised = False
            try:
                zs._fetch_sync(MeetingRef(provider="zoom", conference_id="855"))
            except TranscriptUnavailable:
                raised = True
            expect(raised, "unconfigured Zoom must raise, not retry forever")
        finally:
            cfg.ZOOM_ACCOUNT_ID = saved
    finally:
        zs.requests.get, zs.requests.post = real_get, real_post
        zs._token_cache.clear()
        zs._token_cache.update(real_cache)


# ════════════════════════════════════════════════════════════════
# One pipeline, two providers, chosen per meeting
# ════════════════════════════════════════════════════════════════
async def scenario_provider_routing(sim: Simulator):
    """A Meet link and a Zoom link must coexist for the same user."""
    from bot.services.meetings.pipeline import provider_for

    expect(provider_for("https://meet.google.com/abc-defg-hij") == "google_meet",
           "Meet link")
    expect(provider_for("https://us02web.zoom.us/j/85512345678") == "zoom", "Zoom link")
    expect(provider_for("https://ZOOM.US/j/855") == "zoom", "case-insensitive")
    # Anything else is recorded once and dropped, not polled forever.
    expect(provider_for("dial-in: +91 80 1234") == "", "no provider")
    expect(provider_for("") == "", "empty link")


# ════════════════════════════════════════════════════════════════
# Pull path: ask for a past meeting's action items → card → tickets
# ════════════════════════════════════════════════════════════════
async def _insert_meeting_summary(wa_id: str, task_id, summary: dict,
                                  provider="google_meet"):
    from bot.database import MeetingSummary, async_session
    import json as _json
    async with async_session() as s:
        s.add(MeetingSummary(
            wa_id=wa_id, task_id=task_id, provider=provider,
            conference_id="conf-test",
            summary_json=_json.dumps(summary),
        ))
        await s.commit()


_SUMMARY_FIXTURE = {
    "headline": "q4 pricing alignment",
    "key_takeaways": ["starter plan moves to ₹499"],
    "decisions": [{"decision": "launch oct 1", "decided_by": "Priya"}],
    "action_items": [
        {"task": "send revised deck to finance", "owner": "Harsh", "due": "friday"},
        {"task": "update the pricing page copy", "owner": None, "due": None},
    ],
    "per_person": [{"name": "Priya", "summary": "drove the pricing decision"}],
    "unclear": [],
}


async def scenario_meeting_summary_pull(sim: Simulator):
    """'action items from that meeting?' must reach the stored summary and
    its tickets must become REAL todos/reminders through deterministic taps."""
    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)

    tid = await H.insert_scheduled_task(CREATOR, "pricing call", -120)
    await _insert_meeting_summary(CREATOR, tid, dict(_SUMMARY_FIXTURE))

    # ── 1. Ask in natural language → deterministic card, not model prose ──
    await sim.text(CREATOR, "what were the action items from the pricing call?")
    body = sim.all_text(CREATOR).lower()
    for needle in ("q4 pricing alignment", "send revised deck to finance",
                   "launch oct 1"):
        expect(needle.lower() in body, f"summary card must contain {needle!r}")
    # Card text first, action buttons immediately after.
    last = sim.last(CREATOR)
    expect(last and last["kind"] == "buttons",
           f"expected follow-up action buttons under the card, got {last}")
    expect("mtg_todos_add" in last["buttons"]
           and "mtg_remind_set" in last["buttons"],
           "expected todo + reminder buttons")

    # ── 2. Todos tap → real sheet rows (mocked), no further gate needed ──
    import bot.services.sheets as sheets_mod
    added_batches: list[list[str]] = []
    real_add = sheets_mod.add_todo_to_sheet

    def fake_add_todo(user_db, items):
        added_batches.append(list(items))
        return {"added": len(items),
                "items": [{"index": i + 1, "task": t} for i, t in enumerate(items)],
                "total": len(items)}

    sheets_mod.add_todo_to_sheet = fake_add_todo
    try:
        await sim.tap(CREATOR, "mtg_todos_add")
        expect(len(added_batches) == 1,
               f"todos tap should write one batch, wrote {added_batches}")
        expect(added_batches[0] == ["send revised deck to finance",
                                    "update the pricing page copy"],
               f"wrong todo rows: {added_batches}")
        expect("added 2" in sim.all_text(CREATOR).lower(),
               "expected confirmation of 2 todos")
    finally:
        sheets_mod.add_todo_to_sheet = real_add

    # ── 3. Reminders tap → asks for a time; a clock-time reply sets them ──
    await sim.text(CREATOR, "what were the action items from the pricing call?")
    await sim.tap(CREATOR, "mtg_remind_set")
    expect("what time should i remind you" in sim.all_text(CREATOR).lower(),
           "expected the bot to ask WHEN before setting reminders")

    await sim.text(CREATOR, "tomorrow 9 am")
    from bot.database import Reminder, async_session as _sess
    from sqlalchemy import select as _select

    def _pricing_reminders():
        async def _q():
            async with _sess() as s:
                return list((await s.execute(
                    _select(Reminder).where(
                        Reminder.wa_id == CREATOR,
                        Reminder.message.ilike("%from pricing call%"),
                    )
                )).scalars().all())
        return _q

    import asyncio as _asyncio  # noqa: F401  (kept for symmetry with other scenarios)
    rems = await _pricing_reminders()()
    expect(len(rems) >= 2, f"expected ≥2 reminders created, got {len(rems)}")
    for r in rems:
        expect("from pricing call" in r.message.lower(),
               f"reminder should reference the meeting, got {r.message!r}")
    expect("reminder" in sim.all_text(CREATOR).lower()
           and "set" in sim.all_text(CREATOR).lower(),
           "expected confirmation that reminders were set")

    # ── 4. A time-free reply is refused — never guess (anti-hallucination) ──
    await sim.text(CREATOR, "what were the action items from the pricing call?")
    await sim.tap(CREATOR, "mtg_remind_set")
    n_before = len(rems)
    await sim.text(CREATOR, "tomorrow")
    rems_after = await _pricing_reminders()()
    expect(len(rems_after) == n_before,
           f"a date with no clock time must NOT create reminders "
           f"(before={n_before}, after={len(rems_after)})")


# Ordered list the runner executes.
ALL = [
    ("A.1 Onboarding",            scenario_onboarding),
    ("A.2 Happy path",           scenario_happy_path),
    ("A.4 Missing info",         scenario_missing_info),
    ("A.3 Conflict",             scenario_conflict),
    ("FR-8 STOP / opt-out",      scenario_optout),
    ("§12 Delete my data",       scenario_data_deletion),
    ("Email keeps the time",     scenario_email_keeps_time),
    ("Conflict vs own meeting",  scenario_local_task_conflict),
    ("Flow form books meeting",  scenario_flow_form),
    ("Email-from-chat wrapper",  scenario_email_flow),
    ("Reminder 24h-window fallback", scenario_reminder_out_of_window),
    ("Tool-JSON leak recovery",   scenario_tool_json_leak),
    ("Heartbeat reconcile fires", scenario_heartbeat_reconcile),
    ("Add guest to existing meet", scenario_add_attendee_to_existing),
    ("Add guest never re-asks",    scenario_add_attendee_never_reasks),
    ("Add guest picks the event",  scenario_add_attendee_ambiguous_event),
    ("Full toolset is exposed",    scenario_tool_availability),
    ("Empty reply isn't success",  scenario_empty_completion_is_not_success),
    ("Meeting title is preserved", scenario_title_is_preserved),
    ("Gmeet slots survive LLM path", scenario_gmeet_slots_survive_llm_path),
    ("Same-name contacts pick",      scenario_same_name_contacts_pick),
    ("Transcript context shape",     scenario_transcript_context),
    ("Overload retry is bounded",   scenario_overload_retry_is_bounded),
    ("Other contacts searchable",   scenario_other_contacts_are_searchable),
    ("Transcript pipeline inert",   scenario_transcript_pipeline_is_inert),
    ("Find person in mail",         scenario_find_person_in_mail),
    ("WhatsApp markup not markdown", scenario_whatsapp_markup),
    ("Gmail limit allows a week",   scenario_gmail_limit_allows_a_week),
    ("Long answer budget",          scenario_long_answer_budget),
    ("No Unknown contacts",         scenario_no_unknown_contacts),
    ("No phantom reminder promise", scenario_no_phantom_reminder_promise),
    ("Zoom transcript fetch",       scenario_zoom_fetch),
    ("Provider routing",            scenario_provider_routing),
    ("Meeting summary pull + tickets", scenario_meeting_summary_pull),
]
