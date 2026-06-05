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
# A.5 / FR-10 — Completion loop
# ════════════════════════════════════════════════════════════════
async def scenario_completion(sim: Simulator):
    from bot.services.task_service import (
        get_tasks_needing_completion_check, mark_completion_check_sent,
    )
    from bot.handlers.callback_handler import send_completion_buttons

    await H.make_onboarded_user(CREATOR, name="Harsh")
    sim.clear(CREATOR)
    # scheduled 2h ago, 30 min long → end+60min is in the past
    tid = await H.insert_scheduled_task(CREATOR, "Demo call", minutes_from_now=-120)

    due = await get_tasks_needing_completion_check()
    expect(any(t.id == tid for t in due), "task should need a completion check")

    send_completion_buttons(CREATOR, tid, "How did your Demo call go?")
    await mark_completion_check_sent(tid)
    last = sim.last(CREATOR)
    expect(last["kind"] == "buttons" and f"task_done_{tid}" in last["buttons"],
           "expected Done / Reschedule buttons")

    await sim.tap(CREATOR, f"task_done_{tid}", "✅ Done")
    tasks = await H.get_tasks(CREATOR)
    done = [t for t in tasks if t.id == tid][0]
    expect(done.status == "completed", f"task should be completed, got {done.status}")


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
# FR-9 — Bilateral reminders (creator + assignee), opt-out aware
# ════════════════════════════════════════════════════════════════
async def scenario_bilateral_reminders(sim: Simulator):
    import bot.main as main
    from bot.services.task_service import get_tasks_needing_reminders, upsert_assignee_user

    await H.make_onboarded_user(CREATOR, name="Harsh")
    await H.reset_user(PRIYA_WA)
    sim.clear(CREATOR); sim.clear(PRIYA_WA)

    priya_id = await upsert_assignee_user(PRIYA_PHONE, "Priya")
    tid = await H.insert_scheduled_task(CREATOR, "Project sync", minutes_from_now=1440,
                                        assignee_id=priya_id, assignee_phone=PRIYA_PHONE)

    n24, n1 = await get_tasks_needing_reminders()
    task = [t for t in n24 if t.id == tid]
    expect(task, "task ~24h out should need a T-24h reminder")
    task = task[0]

    # Creator reminder (free-form, retried) + assignee reminder (template)
    main.send_message_retry(CREATOR, f"🔔 Reminder: '{task.title}' is tomorrow.")
    await main._notify_assignee_reminder(task, "is tomorrow")
    expect("reminder" in sim.all_text(CREATOR).lower(), "creator should get a reminder")
    expect(sim.last(PRIYA_WA) and sim.last(PRIYA_WA)["kind"] == "notification",
           "assignee should get a template reminder")

    # Opt-out: assignee no longer messaged
    from bot.database import async_session, User
    from sqlalchemy import select
    async with async_session() as s:
        pu = (await s.execute(select(User).where(User.id == priya_id))).scalar_one()
        pu.consent_status = "OPT_OUT"
        await s.commit()
    sim.clear(PRIYA_WA)
    await main._notify_assignee_reminder(task, "is tomorrow")
    expect(sim.last(PRIYA_WA) is None, "opted-out assignee must NOT be messaged")


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


# Ordered list the runner executes.
ALL = [
    ("A.1 Onboarding",            scenario_onboarding),
    ("A.2 Happy path",           scenario_happy_path),
    ("A.4 Missing info",         scenario_missing_info),
    ("A.3 Conflict",             scenario_conflict),
    ("A.5 Completion loop",      scenario_completion),
    ("FR-8 STOP / opt-out",      scenario_optout),
    ("FR-9 Bilateral reminders", scenario_bilateral_reminders),
    ("§12 Delete my data",       scenario_data_deletion),
    ("Email keeps the time",     scenario_email_keeps_time),
    ("Conflict vs own meeting",  scenario_local_task_conflict),
]
