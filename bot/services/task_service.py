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
from bot.services.calendar import create_event, find_conflicts, delete_event, update_event
from bot.utils.time import (
    IST, UTC, utcnow_aware, utcnow_naive, now_prompt_str,
    to_utc_naive, ensure_aware_local,
)

logger = logging.getLogger(__name__)

# Groq client (OpenAI-compatible)
_client = OpenAI(base_url=config.SARVAM_BASE_URL, api_key=config.SARVAM_API_KEY)
MODEL = config.SARVAM_MODEL


# ═══════════════════════════════════════════════════════════════
# NLP Parsing
# ═══════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════
# Task CRUD + State Transitions
# ═══════════════════════════════════════════════════════════════


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


# A half-finished WhatsApp flow is only live for so long. Without this, a
# days-old `awaiting_gmeet_email` hijacks the next message that happens to
# contain an email address.
CONVERSATION_STATE_TTL = timedelta(minutes=30)


async def get_conversation_state(wa_id: str) -> tuple:
    """Returns (flow_state, context_dict) or (None, {}).

    State older than CONVERSATION_STATE_TTL is treated as absent.
    """
    async with async_session() as session:
        result = await session.execute(
            select(TaskConversationState).where(TaskConversationState.wa_id == wa_id)
        )
        state = result.scalar_one_or_none()
        if not state or not state.flow_state:
            return None, {}
        updated = state.updated_at
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if utcnow_aware() - updated > CONVERSATION_STATE_TTL:
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
# Gmeet slot store
#
# A booking is collected over several turns (who → email → when). Those
# half-filled slots used to live ONLY in the UI layer, and only when the model
# happened to call set_gmeet and the result parsed. When the model instead
# answered in prose ("what's Akshay's email?"), nothing was persisted — so the
# next turn reached the LLM with no memory of the time the user had already
# given, and the bot asked for it a second time.
#
# These helpers let the tool layer read and write those slots directly, so the
# accumulated booking survives regardless of which path handled the turn.
# ═══════════════════════════════════════════════════════════════

GMEET_SLOT_FIELDS = (
    "attendee_name", "attendee_email", "attendee_phone",
    "title", "start_time_iso", "duration_minutes",
)


async def load_gmeet_slots(wa_id: str) -> dict:
    """Return the partially-collected gmeet booking, or {} if none is live.

    Respects the conversation-state TTL, so a stale booking never leaks into
    an unrelated conversation.
    """
    flow_state, ctx = await get_conversation_state(wa_id)
    if not flow_state or ctx.get("flow_kind") != "gmeet":
        return {}
    data = ctx.get("gmeet_data") or {}
    return {k: v for k, v in data.items() if k in GMEET_SLOT_FIELDS and v}


async def save_gmeet_slots(wa_id: str, slots: dict, flow_state: str = "awaiting_gmeet_slots"):
    """Merge `slots` into the live gmeet booking without dropping known fields.

    Only non-empty values overwrite; a turn that supplies just an email must
    not blank out the time collected two turns ago.
    """
    existing_state, ctx = await get_conversation_state(wa_id)
    if ctx.get("flow_kind") != "gmeet":
        ctx = {"flow_kind": "gmeet"}
    data = dict(ctx.get("gmeet_data") or {})
    for key in GMEET_SLOT_FIELDS:
        value = slots.get(key)
        if value not in (None, "", 0):
            data[key] = value
    ctx["gmeet_data"] = data
    await set_conversation_state(wa_id, flow_state or existing_state or "awaiting_gmeet_slots", ctx)

