"""FollowUp Bot — Task Service.

Core business logic: NLP parsing, conflict detection,
task CRUD + state transitions, conversation state, background jobs.
"""

import json
import logging
from datetime import datetime, timedelta

import pytz
from openai import OpenAI
from sqlalchemy import select

from bot.config import config
from bot.database import async_session, User, Task, TaskConversationState
from bot.services.calendar import create_event, find_conflicts, find_free_slots, delete_event, update_event
from bot.utils.time import (
    IST, UTC, utcnow_aware, utcnow_naive, now_prompt_str,
    to_utc_naive, ensure_aware_local,
)

logger = logging.getLogger(__name__)

# Groq client (OpenAI-compatible)
_client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)
MODEL = config.GROQ_MODEL


# ═══════════════════════════════════════════════════════════════
# NLP Parsing
# ═══════════════════════════════════════════════════════════════

_PARSE_PROMPT = """You are a task parser. Extract structured meeting/task info from the user's message.

Current date/time: {now}
Timezone: Asia/Kolkata (IST)

Return ONLY valid JSON:
{{
  "title": "short task title",
  "assignee_name": "person name or null",
  "date_iso": "ISO 8601 datetime with +05:30 offset, or null",
  "duration_minutes": 30,
  "mode": "online" or "offline",
  "is_task": true or false,
  "confidence_scores": {{
    "assignee": 0.0,
    "title": 0.0,
    "date_time": 0.0,
    "duration": 0.0,
    "mode": 0.0
  }}
}}

Rules:
- "Meet" / "Google Meet" → mode="online"
- "at office" / "in person" → mode="offline"
- Default duration 30 min
- If NOT a task/meeting, set is_task=false
- Resolve relative dates (tomorrow, Wednesday, etc.)
- Always include +05:30 in date_iso
- confidence_scores: 0.0–1.0 per field. Use a HIGH value (>0.8) only when
  the user EXPLICITLY stated that field. Use a LOW value (<0.6) when you had
  to guess or default it (e.g. duration not stated → ~0.4). NEVER invent a
  time the user didn't give — if no time was stated, date_iso=null and
  date_time confidence <0.4.

User message: "{message}"
"""

_PARSE_DATETIME_PROMPT = """You are a datetime parser for a WhatsApp scheduling bot.

Current date/time: {now}
Timezone: {timezone}

Extract ONLY the date/time the user is giving for a meeting. Return ONLY valid JSON:
{{
  "date_iso": "ISO 8601 datetime with timezone offset, or null",
  "confidence": 0.0
}}

Rules:
- Resolve relative dates such as tomorrow, today, Wednesday, next Friday.
- If the user provides only a time, use the next valid occurrence in the configured timezone.
- Always include the timezone offset in date_iso.
- If no usable date/time is present, return date_iso=null and confidence below 0.5.

User message: "{message}"
"""


async def parse_task_from_text(text: str) -> dict:
    """Use Groq to extract task details from natural language."""
    now = now_prompt_str()
    prompt = _PARSE_PROMPT.format(now=now, message=text)

    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        parsed = json.loads(raw)
        parsed.setdefault("confidence_scores", {})
        logger.info(f"Parsed task: {parsed}")
        return parsed
    except Exception as e:
        logger.error(f"Task parse error: {e}")
        return {"is_task": False, "confidence_scores": {}}


# Fields whose confidence falls below this are treated as MISSING and
# trigger an FR-4 clarification question (PRD §FR-3).
CONFIDENCE_THRESHOLD = 0.6


def low_confidence_fields(parsed: dict, threshold: float = CONFIDENCE_THRESHOLD) -> list:
    """Return the list of field names whose confidence is below threshold."""
    scores = (parsed or {}).get("confidence_scores") or {}
    return [field for field, val in scores.items()
            if isinstance(val, (int, float)) and val < threshold]


async def parse_datetime_from_text(text: str, timezone_name: str = None) -> dict:
    """Use Groq to extract only a scheduling datetime from a follow-up message."""
    timezone_name = timezone_name or config.BOT_TIMEZONE
    # Fast path: caller used the bot default → reuse cached IST.
    tz = IST if timezone_name == config.BOT_TIMEZONE else pytz.timezone(timezone_name)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z (%A)")
    prompt = _PARSE_DATETIME_PROMPT.format(
        now=now,
        timezone=timezone_name,
        message=text,
    )

    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=160,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        parsed = json.loads(raw)
        if parsed.get("date_iso"):
            # Validate ISO shape early so callers can trust the field.
            datetime.fromisoformat(parsed["date_iso"])
        return parsed
    except Exception as e:
        logger.error(f"Datetime parse error: {e}")
        return {"date_iso": None, "confidence": 0.0}


# ═══════════════════════════════════════════════════════════════
# User helper
# ═══════════════════════════════════════════════════════════════

async def _get_user(wa_id: str):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        return result.scalar_one_or_none()


# ═══════════════════════════════════════════════════════════════
# Conflict Detection (Calendar + DB)
# ═══════════════════════════════════════════════════════════════

async def _check_task_table_conflicts(wa_id: str, proposed_start: datetime, duration: int = 30):
    """Check local tasks table for scheduling conflicts."""
    proposed_end = proposed_start + timedelta(minutes=duration)
    start_utc = to_utc_naive(proposed_start)
    end_utc = to_utc_naive(proposed_end)

    async with async_session() as session:
        result = await session.execute(
            select(Task).where(
                Task.wa_id == wa_id,
                Task.status == "scheduled",
                Task.scheduled_at != None,
            )
        )
        tasks = list(result.scalars().all())

    conflicts = []
    for t in tasks:
        task_end = t.scheduled_at + timedelta(minutes=t.duration_minutes or 30)
        if t.scheduled_at < end_utc and task_end > start_utc:
            ts_ist = UTC.localize(t.scheduled_at).astimezone(IST)
            te_ist = UTC.localize(task_end).astimezone(IST)
            conflicts.append({
                "id": f"task_{t.id}",
                "summary": t.title,
                "start": ts_ist.isoformat(),
                "end": te_ist.isoformat(),
                "source": "bot",
            })
    return conflicts


async def check_calendar_conflicts(wa_id: str, proposed_start: datetime, duration: int = 30):
    """Check BOTH Google Calendar AND bot tasks for conflicts."""
    all_conflicts = []

    try:
        all_conflicts.extend(await _check_task_table_conflicts(wa_id, proposed_start, duration))
    except Exception as e:
        logger.error(f"Bot conflict check failed: {e}")

    db_user = await _get_user(wa_id)
    if db_user and db_user.google_token_json:
        try:
            gc = await find_conflicts(db_user, proposed_start, duration)
            for c in gc or []:
                c["source"] = "google"
            all_conflicts.extend(gc or [])
        except Exception as e:
            logger.error(f"Google conflict check failed: {e}")

    return all_conflicts


async def get_suggested_slots(wa_id: str, around_time: datetime, duration: int = 30, count: int = 3):
    """Get suggested free time slots near a given time."""
    db_user = await _get_user(wa_id)
    if not db_user or not db_user.google_token_json:
        return []
    try:
        return await find_free_slots(db_user, around_time, duration, count)
    except Exception as e:
        logger.error(f"Slot suggestion failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# Task CRUD + State Transitions
# ═══════════════════════════════════════════════════════════════

async def create_draft_task(wa_id: str, title: str, assignee_name: str = None,
                            scheduled_at: datetime = None, duration_minutes: int = 30,
                            mode: str = "online") -> Task:
    """Create a new task in DRAFT status."""
    scheduled_at_utc = None
    if scheduled_at:
        scheduled_at_utc = to_utc_naive(ensure_aware_local(scheduled_at))

    async with async_session() as session:
        task = Task(
            wa_id=wa_id, title=title, assignee_name=assignee_name,
            scheduled_at=scheduled_at_utc, duration_minutes=duration_minutes,
            mode=mode, status="draft",
        )
        session.add(task)
        await session.flush()
        task_id = task.id
        await session.commit()

    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one()


async def schedule_task(task_id: int) -> dict:
    """Transition task DRAFT → SCHEDULED. Creates calendar event."""
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "Task not found"}
        if task.status != "draft":
            return {"error": f"Task is '{task.status}', expected 'draft'"}

        user_result = await session.execute(select(User).where(User.wa_id == task.wa_id))
        db_user = user_result.scalar_one_or_none()

        info = {"task_id": task.id, "title": task.title, "status": "scheduled",
                "calendar_link": "", "meet_link": ""}

        if db_user and db_user.google_token_json and task.scheduled_at:
            scheduled_ist = UTC.localize(task.scheduled_at).astimezone(IST)
            try:
                desc = f"📍 {task.location_text or 'In-person'}" if task.mode == "offline" else None
                event_info = await create_event(
                    db_user, title=task.title, event_dt=scheduled_ist,
                    duration_minutes=task.duration_minutes,
                    meet_link=(task.mode == "online"), description=desc,
                )
                task.external_event_id = event_info.get("id")
                task.meeting_link = event_info.get("meet", "")
                info["calendar_link"] = event_info.get("link", "")
                info["meet_link"] = event_info.get("meet", "")

                try:
                    from bot.services.sheets import log_meeting_to_sheet
                    await log_meeting_to_sheet(db_user, {
                        "id": event_info.get("id", ""), "title": task.title,
                        "start": scheduled_ist.isoformat(),
                        "end": (scheduled_ist + timedelta(minutes=task.duration_minutes)).isoformat(),
                        "meet": event_info.get("meet", ""),
                    }, mode=task.mode)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Calendar event failed for task #{task_id}: {e}")

        task.status = "scheduled"
        task.updated_at = utcnow_aware()
        await session.commit()

        try:
            if db_user and db_user.google_token_json:
                from bot.services.sheets import log_task_to_sheet
                r2 = await session.execute(select(Task).where(Task.id == task_id))
                t2 = r2.scalar_one_or_none()
                if t2:
                    await log_task_to_sheet(db_user, t2)
        except Exception:
            pass

        return info


async def complete_task(task_id: int) -> bool:
    """Transition SCHEDULED → COMPLETED."""
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task or task.status != "scheduled":
            return False
        task.status = "completed"
        task.completion_response = "done"
        task.updated_at = utcnow_aware()
        await session.commit()

        try:
            ur = await session.execute(select(User).where(User.wa_id == task.wa_id))
            db_user = ur.scalar_one_or_none()
            if db_user and db_user.google_token_json:
                from bot.services.sheets import log_task_to_sheet, log_followup_to_sheet
                await log_task_to_sheet(db_user, task)
                await log_followup_to_sheet(db_user, task.id, "completed", f"{task.title} done")
        except Exception:
            pass
        return True


async def cancel_task(task_id: int) -> bool:
    """Cancel a task. Deletes Calendar event if it exists."""
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task or task.status in ("completed", "cancelled"):
            return False

        db_user = None
        if task.external_event_id:
            try:
                ur = await session.execute(select(User).where(User.wa_id == task.wa_id))
                db_user = ur.scalar_one_or_none()
                if db_user:
                    await delete_event(db_user, task.external_event_id)
            except Exception as e:
                logger.error(f"Calendar delete failed for task #{task_id}: {e}")

        task.status = "cancelled"
        task.updated_at = utcnow_aware()
        await session.commit()

        try:
            if not db_user:
                ur = await session.execute(select(User).where(User.wa_id == task.wa_id))
                db_user = ur.scalar_one_or_none()
            if db_user and db_user.google_token_json:
                from bot.services.sheets import log_task_to_sheet, log_followup_to_sheet
                await log_task_to_sheet(db_user, task)
                await log_followup_to_sheet(db_user, task.id, "cancelled", f"{task.title} cancelled")
        except Exception:
            pass
        return True


async def reschedule_task(task_id: int, new_start_iso: str) -> dict:
    """Mark old task RESCHEDULED, create new DRAFT with the new time."""
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        old = result.scalar_one_or_none()
        if not old:
            return {"error": "Task not found"}

        new_start = ensure_aware_local(datetime.fromisoformat(new_start_iso))
        new_start_utc = to_utc_naive(new_start)

        new_task = Task(
            wa_id=old.wa_id, title=old.title, assignee_name=old.assignee_name,
            scheduled_at=new_start_utc, duration_minutes=old.duration_minutes,
            mode=old.mode, status="draft",
        )
        session.add(new_task)
        await session.flush()
        new_id = new_task.id

        old.status = "rescheduled"
        old.rescheduled_to_id = new_id
        old.completion_response = "rescheduled"
        old.updated_at = utcnow_aware()

        if old.external_event_id:
            try:
                ur = await session.execute(select(User).where(User.wa_id == old.wa_id))
                db_user = ur.scalar_one_or_none()
                if db_user:
                    await update_event(db_user, old.external_event_id, start_iso=new_start_iso,
                                       duration_minutes=old.duration_minutes)
                    new_task.external_event_id = old.external_event_id
                    old.external_event_id = None
            except Exception as e:
                logger.error(f"Calendar update failed on reschedule: {e}")

        await session.commit()
        return {"new_task_id": new_id, "old_task_id": task_id}


async def upsert_assignee_user(phone_e164: str, name: str = "") -> "int | None":
    """Get-or-create a User row for an assignee, keyed by WhatsApp wa_id.

    PRD §10 + §FR-8 — the assignee must exist as a User so we can link
    `tasks.assignee_id`, run bilateral conflict checks, and honour their
    consent / opt-out independently of the creator.
    """
    from bot.services.phone import to_wa_id
    wa = to_wa_id(phone_e164)
    if not wa:
        return None
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa)
        )).scalar_one_or_none()
        if not u:
            u = User(
                wa_id=wa, phone_e164=phone_e164,
                display_name=(name or None), first_name=(name.split()[0] if name else None),
                consent_status="PENDING", whatsapp_confirmed=False,
                onboarding_complete=False,
            )
            session.add(u)
            await session.flush()
        else:
            if name and not u.display_name:
                u.display_name = name
            if not u.phone_e164:
                u.phone_e164 = phone_e164
        uid = u.id
        await session.commit()
        return uid


async def is_assignee_reachable(assignee_id: "int | None") -> bool:
    """False if the assignee has opted out (PRD §FR-8)."""
    if not assignee_id:
        return True  # no linked user → fall back to phone-only notify
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.id == assignee_id)
        )).scalar_one_or_none()
        return not (u and u.consent_status == "OPT_OUT")


async def record_meeting_task(
    creator_wa_id: str, title: str, assignee_name: str,
    assignee_phone_e164: "str | None", assignee_id: "int | None",
    scheduled_local_dt: datetime, duration_minutes: int,
    meeting_link: str, external_event_id: str, timezone_name: str,
    mode: str = "online", confidence_scores: dict = None,
) -> int:
    """Insert a SCHEDULED Task for an already-created calendar event.

    This is what closes the FR-2/FR-7 hole: the gmeet happy path used to
    create a Google event with NO Task row, so reminders / completion
    checks / conflict detection never saw it. Now every booked meeting is
    a first-class Task.
    """
    async with async_session() as session:
        task = Task(
            wa_id=creator_wa_id, title=title,
            assignee_name=assignee_name or None,
            assignee_phone=assignee_phone_e164 or None,
            assignee_id=assignee_id,
            scheduled_at=to_utc_naive(ensure_aware_local(scheduled_local_dt)),
            duration_minutes=duration_minutes, mode=mode,
            meeting_link=meeting_link or None,
            external_event_id=external_event_id or None,
            timezone=timezone_name,
            status="scheduled",
            reminder_offsets_min=[1440, 60],
            confidence_scores=confidence_scores,
        )
        session.add(task)
        await session.flush()
        tid = task.id
        await session.commit()
    logger.info(f"Recorded meeting Task #{tid} for {creator_wa_id} (assignee_id={assignee_id})")
    return tid


async def assignee_has_conflict(assignee_id: "int | None", proposed_local_dt: datetime,
                                duration: int = 30) -> list:
    """Bilateral conflict check (PRD §FR-5) — the assignee's SCHEDULED tasks."""
    if not assignee_id:
        return []
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.id == assignee_id)
        )).scalar_one_or_none()
        if not u:
            return []
        return await _check_task_table_conflicts(u.wa_id, proposed_local_dt, duration)


async def conflicts_for_assignee_phone(phone_e164: str, proposed_local_dt: datetime,
                                       duration: int = 30) -> list:
    """Bilateral conflict (PRD §FR-5) for an assignee identified by phone.

    Used at *propose* time, before the assignee User row is created. Only
    returns hits if the assignee already exists as a tracked user with
    bot-managed meetings (i.e. a repeat assignee).
    """
    from bot.services.phone import to_wa_id
    wa = to_wa_id(phone_e164 or "")
    if not wa:
        return []
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa)
        )).scalar_one_or_none()
    if not u:
        return []
    return await _check_task_table_conflicts(wa, proposed_local_dt, duration)


async def get_active_tasks(wa_id: str, status: str = None, limit: int = 10) -> list:
    """List tasks, optionally filtered by status."""
    async with async_session() as session:
        q = select(Task).where(Task.wa_id == wa_id)
        if status:
            q = q.where(Task.status == status)
        else:
            q = q.where(Task.status.notin_(["cancelled"]))
        q = q.order_by(Task.created_at.desc()).limit(limit)
        result = await session.execute(q)
        return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════════
# Conversation State Management
# ═══════════════════════════════════════════════════════════════

async def set_conversation_state(wa_id: str, flow_state: str, context: dict):
    async with async_session() as session:
        result = await session.execute(
            select(TaskConversationState).where(TaskConversationState.wa_id == wa_id)
        )
        state = result.scalar_one_or_none()
        if state:
            state.flow_state = flow_state
            state.context_json = json.dumps(context)
            state.updated_at = utcnow_aware()
        else:
            session.add(TaskConversationState(
                wa_id=wa_id, flow_state=flow_state,
                context_json=json.dumps(context),
            ))
        await session.commit()


async def get_conversation_state(wa_id: str) -> tuple:
    """Returns (flow_state, context_dict) or (None, {})."""
    async with async_session() as session:
        result = await session.execute(
            select(TaskConversationState).where(TaskConversationState.wa_id == wa_id)
        )
        state = result.scalar_one_or_none()
        if not state or not state.flow_state:
            return None, {}
        try:
            ctx = json.loads(state.context_json) if state.context_json else {}
        except json.JSONDecodeError:
            ctx = {}
        return state.flow_state, ctx


async def clear_conversation_state(wa_id: str):
    async with async_session() as session:
        result = await session.execute(
            select(TaskConversationState).where(TaskConversationState.wa_id == wa_id)
        )
        state = result.scalar_one_or_none()
        if state:
            state.flow_state = None
            state.context_json = None
            state.updated_at = utcnow_aware()
            await session.commit()


# ═══════════════════════════════════════════════════════════════
# Background Jobs
# ═══════════════════════════════════════════════════════════════

async def check_draft_timeouts() -> int:
    """Cancel drafts older than 24 h."""
    cutoff = utcnow_naive() - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.status == "draft", Task.created_at <= cutoff)
        )
        expired = list(result.scalars().all())
        for t in expired:
            t.status = "cancelled"
            t.updated_at = utcnow_aware()
        if expired:
            await session.commit()
    return len(expired)


async def get_tasks_needing_completion_check() -> list:
    """Scheduled tasks past end + 60 min without a completion check."""
    now_utc = utcnow_naive()
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(
                Task.status == "scheduled", Task.completion_check_sent == False,
                Task.scheduled_at != None,
            )
        )
        tasks = list(result.scalars().all())
    return [t for t in tasks
            if now_utc >= t.scheduled_at + timedelta(minutes=t.duration_minutes + 60)]


async def mark_completion_check_sent(task_id: int):
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            task.completion_check_sent = True
            task.completion_check_sent_at = utcnow_aware()
            task.updated_at = utcnow_aware()
            await session.commit()


async def check_unconfirmed_timeouts() -> int:
    """Mark tasks UNCONFIRMED if 48 h passed since completion check."""
    cutoff = utcnow_naive() - timedelta(hours=48)
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(
                Task.status == "scheduled", Task.completion_check_sent == True,
                Task.completion_check_sent_at != None, Task.completion_check_sent_at <= cutoff,
            )
        )
        stale = list(result.scalars().all())
        for t in stale:
            t.status = "unconfirmed"
            t.completion_response = "no_response"
            t.updated_at = utcnow_aware()
        if stale:
            await session.commit()
    return len(stale)


async def get_tasks_needing_reminders():
    """Find scheduled tasks needing T-24h or T-1h reminders."""
    now_utc = utcnow_naive()
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.status == "scheduled", Task.scheduled_at != None)
        )
        tasks = list(result.scalars().all())

    need_24h, need_1h = [], []
    for t in tasks:
        secs = (t.scheduled_at - now_utc).total_seconds()
        if not t.reminder_1_sent and 23.5 * 3600 <= secs <= 24.5 * 3600:
            need_24h.append(t)
        if not t.reminder_2_sent and 0.5 * 3600 <= secs <= 1.5 * 3600:
            need_1h.append(t)
    return need_24h, need_1h


async def mark_reminder_sent(task_id: int, reminder_num: int):
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            now = utcnow_aware()
            if reminder_num == 1:
                task.reminder_1_sent = now
            elif reminder_num == 2:
                task.reminder_2_sent = now
            task.updated_at = now
            await session.commit()
