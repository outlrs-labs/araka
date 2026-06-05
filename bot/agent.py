"""FollowUp Bot — AI Agent (Groq + function-calling loop).

Workflow:
  Text/Voice → AI Agent (Groq LLM + 9 tools + 10-msg memory) → Response

User identifier: wa_id (WhatsApp phone number string)
"""

import json
import logging
import traceback
from datetime import timedelta

from openai import OpenAI
from sqlalchemy import select, delete as sql_delete

from bot.config import config
from bot.database import async_session, ChatMemory, User
from bot.tools import (
    save_note, get_notes, delete_note,
    calendar_create, calendar_get_all,
    set_gmeet, contacts_search,
    set_reminder, list_reminders, delete_reminder,
    add_todo, get_todos, complete_todo, delete_todo,
)
from bot.utils.time import IST, utcnow_naive, now_prompt_str  # noqa: F401  (IST kept for backwards-compat with any external callers)

logger = logging.getLogger(__name__)

# ── Groq client (OpenAI-compatible) ──
client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)
MODEL = config.GROQ_MODEL

MEMORY_WINDOW = 20


# ═══════════════════════════════════════════════════════════════
# System Prompt — optimised for latency + minimal hallucination
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
You are **FollowUp Bot**, a WhatsApp assistant that helps people schedule \
meetings and keep their commitments. You are precise, brief, and you \
NEVER invent facts.

### Who you're talking to
- Name: {user_name}
- Timezone: {timezone} (show every time in their local time)
- Google connected: {google_connected}
- Current time: {time}

### Tools — use ONLY these; never promise anything a tool can't do
- `set_gmeet` — SCHEDULE a meeting WITH another person (creates a Google Meet)
- `contacts_search` — LOOK UP a saved person's email/phone. Use when the
  user asks "what's X's number/email/contact" — this is NOT scheduling.
- `calendar_create` / `calendar_get_all` — the user's OWN events
- `save_note` / `get_notes` / `delete_note` — quick notes
- `set_reminder` / `list_reminders` / `delete_reminder` — reminders
- `add_todo` / `get_todos` / `complete_todo` / `delete_todo` — to-do list

### 🚫 The five rules you must never break
1. **Never invent a clock time.** If the user did not TYPE a time \
("6 PM", "18:30", "noon"), leave `start_time_iso` EMPTY in `set_gmeet`. \
"today" / "tomorrow" / "Friday" with no clock time = time still MISSING; \
the server will ask. (The server rejects any time you invent.)
2. **Never claim success early.** Do NOT say a meeting is "scheduled", \
"booked", or "done" unless a TOOL RESULT said so. Events are created only \
after the user taps ✅ Yes. Announcing success before that is a lie.
3. **Never invent a `title`.** Leave it empty unless the user stated a \
topic (e.g. "quick chat about Q4" → title="Q4"). Do NOT put the person's \
name in `title` — the server builds the final title.
4. **Never fabricate stored data.** Before answering about notes, \
reminders, to-dos, or calendar, CALL the matching get_/list_ tool first \
and answer only from its result. Never say "you have no reminders" or \
"you deleted that" without checking.
5. **Call `set_gmeet` at most once** per user request.

### Time handling
- For "in X minutes" / "after X min" → call `set_reminder` with \
`relative_minutes`. Do NOT compute the timestamp yourself.
- For an explicit clock time → pass the ISO datetime with the user's \
offset. Default meeting duration is **30 minutes**.

### Meetings vs personal events
- WITH another person → `set_gmeet`. After you call it, your turn is \
basically over: the server resolves the contact, checks conflicts, and \
shows a confirmation card. Relay the server's message — don't pre-empt it.
- Just the user, no attendee → `calendar_create`.
- If the user writes `Name{{email@example.com}}`, pass `attendee_name` \
AND `attendee_email`.

### To-do vs Notes
- Actionable lists ("my tasks", "add to my list") → `add_todo` / `get_todos`.
- Free-form memos → `save_note` / `get_notes`.
- If a reminder in `list_reminders` is marked already FIRED, tell the \
user it went off — do NOT create a duplicate.

### Privacy (non-negotiable)
- Do NOT repeat back full phone numbers or email addresses unless the \
user explicitly asked you to confirm one.
- Do not echo internal IDs or raw JSON.

### Style
- Short, warm, WhatsApp-native. Dates like "Mon May 3, 9:00 AM".
- Emojis sparingly: 📅 ✅ 🔗 ⏰.
- If a tool returns "GOOGLE_NOT_CONNECTED" → end your reply with \
SHOW_CONNECT_BUTTON.

### Boundaries
- You cannot browse the web, run code, or do arithmetic beyond simple \
date phrasing. You are a scheduling assistant only.
{active_flow_context}\
"""


# ═══════════════════════════════════════════════════════════════
# Tool Declarations (OpenAI function-calling format) — 13 tools
# ═══════════════════════════════════════════════════════════════

TOOLS = [
    {"type": "function", "function": {
        "name": "set_gmeet",
        "description": "Schedule a Google Meet meeting with another person. Handles contact lookup, conflict checking, and event creation.",
        "parameters": {"type": "object", "properties": {
            "attendee_name": {"type": "string", "description": "Name of the person to meet"},
            "attendee_email": {"type": "string", "description": "Email if known (skip contact search)"},
            "title": {"type": "string", "description": "Meeting title"},
            "start_time_iso": {"type": "string", "description": "ISO 8601 start datetime with +05:30. LEAVE EMPTY if the user did not state a clock time."},
            "duration_minutes": {"type": "string", "description": "Duration in minutes (default 30)"},
        }, "required": ["attendee_name"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_create",
        "description": "Create a PERSONAL calendar event (no attendees). For meetings WITH people, use set_gmeet.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "start_time_iso": {"type": "string", "description": "ISO 8601 start datetime with timezone"},
            "duration_minutes": {"type": "string", "description": "Duration in minutes (default 30)"},
            "with_meet": {"type": "string", "description": "Set to 'true' for a Google Meet link"},
        }, "required": ["title", "start_time_iso"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_get_all",
        "description": "Retrieve upcoming Google Calendar events.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "string", "description": "Max events to return (default 10)"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "contacts_search",
        "description": "Look up a saved person's contact details (email, phone) from the user's Google Contacts. Use this when the user ASKS ABOUT a contact, e.g. 'what is Akshay's email/number/contact'. Do NOT use it to schedule — that's set_gmeet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Name to look up"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "save_note",
        "description": "Save a quick note for the user.",
        "parameters": {"type": "object", "properties": {
            "note_text": {"type": "string"},
            "detected_date_iso": {"type": "string"},
        }, "required": ["note_text"]},
    }},
    {"type": "function", "function": {
        "name": "get_notes",
        "description": "Get the user's recent notes.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "string"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "delete_note",
        "description": "Delete a note by its ID.",
        "parameters": {"type": "object", "properties": {
            "note_id": {"type": "string"},
        }, "required": ["note_id"]},
    }},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Set a one-time or recurring daily reminder. For 'in X minutes' use relative_minutes instead of computing the ISO time yourself.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"},
            "relative_minutes": {"type": "string", "description": "Minutes from NOW. Use for 'in 5 minutes'/'after 10 min'. Server computes exact time. PREFERRED over remind_at_iso for relative times."},
            "remind_at_iso": {"type": "string", "description": "ISO 8601 datetime. Use ONLY for absolute times like 'at 5 PM tomorrow'. For relative times use relative_minutes instead."},
            "reminder_type": {"type": "string", "enum": ["text", "notes", "calendar"]},
            "is_recurring": {"type": "string", "description": "Set to 'true' for daily recurring"},
            "recur_time_hhmm": {"type": "string", "description": "Time in HH:MM (24h) for daily recurring"},
        }, "required": ["message"]},
    }},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": "List all active reminders and daily messages.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "delete_reminder",
        "description": "Delete a reminder by its ID.",
        "parameters": {"type": "object", "properties": {
            "reminder_id": {"type": "string"},
        }, "required": ["reminder_id"]},
    }},
    # ── To-Do (Google Sheets) ──
    {"type": "function", "function": {
        "name": "add_todo",
        "description": "Add items to the user's to-do list (stored in Google Sheets). Pass comma-separated items.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "string", "description": "Comma-separated to-do items. E.g. 'Buy groceries, Call mom, Study'"},
        }, "required": ["items"]},
    }},
    {"type": "function", "function": {
        "name": "get_todos",
        "description": "Get the user's to-do list from Google Sheets.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "complete_todo",
        "description": "Mark a to-do item as done or undone by its number.",
        "parameters": {"type": "object", "properties": {
            "item_number": {"type": "string", "description": "The # of the to-do item"},
            "done": {"type": "string", "description": "'true' to mark done, 'false' to reopen (default: true)"},
        }, "required": ["item_number"]},
    }},
    {"type": "function", "function": {
        "name": "delete_todo",
        "description": "Delete a to-do item by its number.",
        "parameters": {"type": "object", "properties": {
            "item_number": {"type": "string", "description": "The # of the to-do item"},
        }, "required": ["item_number"]},
    }},
]


# ═══════════════════════════════════════════════════════════════
# Tool Executor
# ═══════════════════════════════════════════════════════════════

TOOL_MAP = {
    "calendar_create": calendar_create, "calendar_get_all": calendar_get_all,
    "save_note": save_note, "get_notes": get_notes, "delete_note": delete_note,
    "set_gmeet": set_gmeet, "contacts_search": contacts_search,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders, "delete_reminder": delete_reminder,
    "add_todo": add_todo, "get_todos": get_todos,
    "complete_todo": complete_todo, "delete_todo": delete_todo,
}

_INT_FIELDS = {"duration_minutes", "limit", "note_id", "task_id", "reminder_id", "new_duration_minutes", "relative_minutes", "item_number"}
_BOOL_FIELDS = {"with_meet", "is_recurring", "done"}

# Per-request user text (set at the top of process_message). Used by
# _execute_tool to inject user_text into set_gmeet so the anti-
# hallucination guard can validate against the actual user message.
# Stored on a function attribute to remain process-local & sync-safe.
def _set_current_user_text(wa_id: str, text: str) -> None:
    _execute_tool._user_text_by_wa[wa_id] = text  # type: ignore[attr-defined]

def _get_current_user_text(wa_id: str) -> str:
    return _execute_tool._user_text_by_wa.get(wa_id, "")  # type: ignore[attr-defined]

# Cache for set_gmeet results (keyed by wa_id)
_gmeet_result_cache: dict = {}

# Forward-declare the user-text storage so attribute exists at call-time
def _init_tool_storage():
    if not hasattr(_execute_tool, "_user_text_by_wa"):
        _execute_tool._user_text_by_wa = {}  # type: ignore[attr-defined]


def get_last_gmeet_result(wa_id: str):
    """Retrieve and clear cached set_gmeet result for a user."""
    return _gmeet_result_cache.pop(wa_id, None)


async def _execute_tool(wa_id: str, name: str, args: dict) -> str:
    logger.info(f"Tool call: {name}({args})")
    fn = TOOL_MAP.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        for k, v in list(args.items()):
            if k in _INT_FIELDS and isinstance(v, str):
                try:
                    args[k] = int(v)
                except ValueError:
                    pass
            if k in _BOOL_FIELDS and isinstance(v, str):
                args[k] = v.lower() in ("true", "1", "yes")
            if isinstance(v, dict) and k in v:
                args[k] = v[k]

        # Inject the raw user message into set_gmeet for the anti-
        # hallucination guard (PRD §FR-3 + Procedural Integrity).
        # The LLM never sees `user_text` — it's a server-side cross-check.
        if name == "set_gmeet" and "user_text" not in args:
            args["user_text"] = _get_current_user_text(wa_id)
        # Force-clear `_confirmed` — only the deterministic callback
        # handler is allowed to set it. If the LLM ever tries, drop it.
        args.pop("_confirmed", None)

        result = await fn(wa_id, **args)

        if name == "set_gmeet":
            try:
                parsed = json.loads(result)
                if parsed.get("action") in (
                    "pick_contact", "no_contact", "need_email",
                    "missing_time", "conflict", "created",
                    "awaiting_confirmation",
                ):
                    _gmeet_result_cache[wa_id] = parsed
            except (json.JSONDecodeError, TypeError):
                pass

        return result
    except Exception as e:
        logger.error(f"Tool error ({name}): {e}")
        return f"Tool error: {e}"


# ═══════════════════════════════════════════════════════════════
# Agent — Main Processing Loop
# ═══════════════════════════════════════════════════════════════

async def process_message(wa_id: str, text: str) -> str:
    """Process a user message through the AI agent.

    Flow: Load memory → Build prompt → Call Groq → Handle tool calls → Return.
    """
    # Stash raw user text for tool-side anti-hallucination guards
    _init_tool_storage()
    _set_current_user_text(wa_id, text)

    # 1. Get DB user to resolve user.id for ChatMemory
    async with async_session() as session:
        u_result = await session.execute(select(User).where(User.wa_id == wa_id))
        db_user = u_result.scalar_one_or_none()
        if not db_user:
            return "User not found. Please restart the bot."
        db_user_id = db_user.id  # Integer PK for ChatMemory
        google_connected = "Yes" if db_user.google_token_json else "No"
        user_name = db_user.display_name or db_user.first_name or "there"
        user_tz = db_user.timezone or "Asia/Kolkata"

    # 2. Load memory
    async with async_session() as session:
        result = await session.execute(
            select(ChatMemory).where(ChatMemory.user_id == db_user_id)
            .order_by(ChatMemory.created_at.desc()).limit(MEMORY_WINDOW)
        )
        records = list(reversed(result.scalars().all()))

        messages = []
        for r in records:
            messages.append({
                "role": "assistant" if r.role == "model" else r.role,
                "content": r.text,
            })

        messages.append({"role": "user", "content": text})
        session.add(ChatMemory(user_id=db_user_id, role="user", text=text))

        # Prune memory older than 2 hours
        cutoff = utcnow_naive() - timedelta(hours=2)
        await session.execute(
            sql_delete(ChatMemory).where(
                ChatMemory.user_id == db_user_id,
                ChatMemory.created_at < cutoff,
            )
        )
        await session.commit()

    # 3. Build system prompt
    now_ist = now_prompt_str()
    active_flow_context = ""

    # Inject live state so LLM never has to guess
    try:
        from bot.database import Note, Reminder
        async with async_session() as session:
            from sqlalchemy import func
            notes_count = (await session.execute(
                select(func.count()).where(Note.user_id == db_user_id)
            )).scalar() or 0
            reminders_count = (await session.execute(
                select(func.count()).where(
                    Reminder.wa_id == wa_id, Reminder.is_sent == False
                )
            )).scalar() or 0
        active_flow_context += (
            f"\n### Live state (from DB — trust this, not memory)"
            f"\n- Notes saved: {notes_count}"
            f"\n- Active reminders: {reminders_count}"
        )
    except Exception as e:
        logger.warning(f"State injection failed: {e}")

    try:
        from bot.services.task_service import get_conversation_state
        flow_state, flow_ctx = await get_conversation_state(wa_id)
        if flow_state and flow_state != "awaiting_gmeet_confirmation":
            gd = flow_ctx.get("gmeet_data", {})
            td = flow_ctx.get("task_data", {})
            pending_attendee = gd.get("attendee_name", "") or td.get("assignee_name", "")
            pending_email = gd.get("attendee_email", "")
            if pending_attendee:
                active_flow_context += (
                    f"\n### In-progress meeting (continue it)\n"
                    f"The user is part-way through scheduling a meeting with "
                    f"**{pending_attendee}**"
                    + (f" ({pending_email})" if pending_email else "")
                    + ". If their new message supplies the missing piece "
                    "(a time, or an email), call `set_gmeet` again with this "
                    "attendee plus the new detail. If they've changed topic, "
                    "just help with the new request instead."
                )
    except Exception as e:
        logger.error(f"Flow context load failed: {e}")

    system_msg = {
        "role": "system",
        "content": SYSTEM_PROMPT.format(
            time=now_ist,
            active_flow_context=active_flow_context,
            google_connected=google_connected,
            user_name=user_name,
            timezone=user_tz,
        ),
    }

    try:
        response = _call_groq(system_msg, messages)

        # 4. Tool-call loop (max 5 iterations)
        for _ in range(5):
            choice = response.choices[0]
            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                break

            dumped = choice.message.model_dump(exclude_unset=True)
            dumped.pop("annotations", None)
            dumped.pop("audio", None)
            messages.append(dumped)

            for tc in choice.message.tool_calls:
                try:
                    fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    fn_args = {}

                result_str = await _execute_tool(wa_id, tc.function.name, fn_args)
                result_content = str(result_str)
                if len(result_content) > 2000:
                    result_content = result_content[:2000] + "\n...(truncated)"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_content,
                })

            response = _call_groq(system_msg, messages)

        # 5. Extract final response
        final = response.choices[0].message.content or "Done! ✅"

        # 6. Save to memory (trim to reduce storage)
        final_trimmed = final[:800] if len(final) > 800 else final
        async with async_session() as session:
            session.add(ChatMemory(user_id=db_user_id, role="model", text=final_trimmed))
            await session.commit()

        return final

    except Exception as e:
        logger.error(f"Agent error: {e}\n{traceback.format_exc()}")
        error_str = str(e).lower()
        if "rate_limit" in error_str or "429" in error_str:
            return "⏳ I'm being rate-limited right now. Please try again in a minute."
        if "model output" in error_str and "empty" in error_str:
            return "🔄 The AI returned an empty response. Please rephrase and try again."
        if "invalid_api_key" in error_str or ("401" in error_str and "api key" in error_str):
            return "⚙️ The bot isn't fully configured yet. Ask the admin to check the GROQ_API_KEY in .env."
        if "connection" in error_str or "timeout" in error_str:
            return "📡 Having trouble reaching the AI right now. Please try again in a moment."
        return "Sorry, I ran into an issue. Please try again in a moment."


def _call_groq(system_msg: dict, messages: list, max_retries: int = 3):
    """Call Groq with retry logic."""
    import time
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[system_msg] + messages,
                tools=TOOLS,
                temperature=0.1,
                max_tokens=1024,
            )
            return response
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            if (("model output" in error_msg and "empty" in error_msg) or
                    "tool_use_failed" in error_msg or "tool call validation" in error_msg):
                logger.warning(f"Groq retryable error (attempt {attempt + 1}/{max_retries}): {str(e)[:100]}")
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    raise last_error
