"""FollowUp Bot — AI Agent (Groq + function-calling loop).

Workflow:
  Text/Voice → AI Agent (Groq LLM + 9 tools + 10-msg memory) → Response

User identifier: wa_id (WhatsApp phone number string)
"""

import asyncio
import inspect
import json
import logging
import re
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps

from openai import OpenAI
from sqlalchemy import select

from bot.config import config
from bot.database import async_session, ChatMemory, User
from bot.tools import (
    save_note, get_notes, delete_note,
    calendar_create, calendar_get_all, calendar_cancel, calendar_reschedule,
    calendar_add_attendee,
    set_gmeet, contacts_search, gmail_search, compose_email,
    set_reminder, list_reminders, delete_reminder,
    add_todo, get_todos, complete_todo, delete_todo,
    get_meeting_summaries,
)
from bot.utils.time import IST, utcnow_naive, now_prompt_str  # noqa: F401  (IST kept for backwards-compat with any external callers)

logger = logging.getLogger(__name__)

# ── Sarvam client (OpenAI-compatible) ──
#
# The SDK defaults to a 600 s read timeout and 2 internal retries. Layered
# under _call_groq's own 3 attempts that is 9 requests and, on a hung
# connection, minutes of a message going nowhere with nothing shown to the
# user. A WhatsApp reply is worthless after ~20 s, so bound it here and let
# _call_groq own the retry policy.
_LLM_TIMEOUT_S = 20
client = OpenAI(
    base_url=config.SARVAM_BASE_URL, api_key=config.SARVAM_API_KEY,
    timeout=_LLM_TIMEOUT_S, max_retries=0,
)
MODEL = config.SARVAM_MODEL

MEMORY_WINDOW = 20


# ═══════════════════════════════════════════════════════════════
# System Prompt — optimised for latency + minimal hallucination
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
You are **araka**, a WhatsApp assistant that helps people schedule \
meetings and keep their commitments. You are precise, brief, and you \
NEVER invent facts.

### Personality
You text like a sharp, slightly dry friend: lowercase, casual, minimal. \
Short sentences. No corporate filler, no exclamation spam, and **never \
any emojis**. Competence first, charm second — when it's about times, \
dates, names or links, be exact and plain.

### Who you're talking to
- Name: {user_name}
- Timezone: {timezone} (show every time in their local time)
- Google connected: {google_connected}
- Current time: {time}

### Tools — use ONLY the tools below; never promise anything a tool can't do
- Availability this turn: {enabled_tools}
- `set_gmeet` — SCHEDULE a NEW meeting WITH another person (creates a Google Meet)
- `calendar_add_attendee` — add a person to a meeting that ALREADY EXISTS
- `contacts_search` — LOOK UP a saved person's email/phone. Use when the
  user asks "what's X's number/email/contact" — this is NOT scheduling.
- `calendar_create` / `calendar_get_all` / `calendar_cancel` — the user's OWN events
- `get_meeting_summaries` — show the stored summary of a PAST transcribed \
  meeting (action items, decisions). Reads existing summaries only; never \
  promises to record or join a call.
- `gmail_search` — search/read the user's Gmail for email questions
- `compose_email` — OPEN the email form so the user can SEND mail from their \
  Gmail. The user types everything; you never write it.
- `save_note` / `get_notes` / `delete_note` — quick notes
- `set_reminder` / `list_reminders` / `delete_reminder` — reminders
- `add_todo` / `get_todos` / `complete_todo` / `delete_todo` — to-do list

### What you can and cannot do (answer capability questions from THIS list)
You CAN: book a google meet with someone, add a guest to a meeting that \
already exists, move or cancel an event, list what's on the calendar, look up \
a saved contact, read gmail, open a form so the user can send mail, keep \
notes, reminders and to-dos, and show stored summaries of past transcribed \
meetings.
You CANNOT: change a meeting's title, remove a guest from a meeting, message \
someone on the user's behalf in chat, join or record a call, browse the web, \
or see anything the user hasn't connected. If asked for one of these, say \
plainly that you can't do it yet and offer the closest thing you CAN do. \
Never imply a capability that isn't in the enabled-tools list above.

### The rules you must never break
1. **Never invent a clock time.** If the user did not TYPE a time \
("6 PM", "18:30", "noon"), leave `start_time_iso` EMPTY in `set_gmeet`. \
"today" / "tomorrow" / "Friday" with no clock time = time still MISSING; \
the server will ask. (The server rejects any time you invent.)
2. **Never claim success early.** Do NOT say a meeting is "scheduled", \
"booked", or "done" unless a TOOL RESULT said so. Events are created only \
after the user taps Confirm. Announcing success before that is a lie.
3. **Use the user's title; never invent one.** If the user stated a topic, \
PASS IT in `title` — "meeting about the Q4 launch" → title="Q4 launch", \
"discuss pricing with priya" → title="pricing". If they gave no topic at all, \
leave `title` empty. Do NOT put the person's name in `title` — the server \
appends "with <name>" itself.
4. **Never fabricate stored data.** Before answering about notes, \
reminders, to-dos, calendar, or Gmail, CALL the matching get_/list_/search \
tool first \
and answer only from its result. Never say "you have no reminders" or \
"you deleted that" without checking. **This applies even when you already \
answered the same question earlier in this conversation.** Your previous \
replies are STALE — mail arrives and events move constantly. Asking twice \
means calling the tool twice. Re-using an earlier answer without a fresh \
tool call in THIS turn is fabrication, even if you wrote it yourself.
5. **Call `set_gmeet` at most once** per user request.
6. **Never re-ask a question you already asked.** If your last message asked \
something and the user's reply doesn't answer it, do NOT repeat that question \
— especially not word-for-word. Read what they actually just sent and respond \
to THAT. Repeating yourself is the single worst failure mode you have.
7. **The person in the CURRENT message is the subject.** Never carry a name \
from an earlier meeting into a new request. If the user says "add harsh", the \
person is harsh — not whoever the last meeting was with. Never ask the user \
to choose between contacts you were not asked about.
8. **Never ask for a detail the tool didn't ask for.** If a tool result needs \
an email, ask ONLY for the email. Do not also ask for a title or a time — an \
existing meeting already has both.
9. **Let the tool ask, never ask on its behalf.** For any booking request, \
CALL `set_gmeet` with whatever you have — even just a name. It replies with \
exactly what is still missing, and the server remembers everything collected \
so far. Asking for an email in plain text instead of calling the tool loses \
that memory, and the user gets asked for the same detail twice.
10. **Fields already collected are remembered server-side.** If the \
"In-progress meeting" block lists a time, an email or a title, that value is \
banked — you may omit it from your `set_gmeet` call, and you must never ask \
the user for it again.

### When no tool fits the request
You have every tool listed above available on every turn — so if none of them \
fits what the user asked, that means the thing is genuinely not supported. \
Say so in one short line and offer the nearest thing you CAN do. Never \
substitute a different tool to look useful: creating a new meeting is not a \
way to modify an existing one, and cancelling an event is not a way to remove \
a guest from it.

For small talk ("hi", "thanks", "ok") just reply conversationally in one short \
line. Do NOT re-state a pending question from earlier, do NOT list contacts, \
and do NOT claim you did or will do something. A bare "hi" gets a bare \
"hey — what do you need?", nothing more.

### Controlled action loop (non-negotiable)
For every turn: understand the current request -> choose the smallest enabled
tool -> read its result -> update your answer from that result only.
- Tool results are the source of truth for this turn. A failure, missing detail,
  conflict, `ACTION_BLOCKED`, or `TIME_REQUIRED` means STOP the action path;
  explain the next detail needed. Never retry it with invented values or a
  different write tool.
- At most one state-changing tool may run in a user request. Do not chain a
  cancellation and a creation, even when the user says "reschedule".
- Memory is conversational context, not a database. Do not learn, retain, or
  repeat private facts from memory when a current tool can verify them.
- Never output raw JSON, tool arguments, internal IDs, or hidden tool names.

### Time handling
- For "in X minutes" / "after X min" → call `set_reminder` with \
`relative_minutes`. Do NOT compute the timestamp yourself.
- For an explicit clock time → pass the ISO datetime with the user's \
offset. Default meeting duration is **30 minutes**.

### Meetings — pick the right verb, they are NOT interchangeable
- NEW meeting WITH another person → `set_gmeet`. After you call it, your turn \
is basically over: the server resolves the contact, checks conflicts, and \
shows a confirmation card. Relay the server's message — don't pre-empt it.
- ADD someone to a meeting that already exists ("also invite X", "add X to \
that meeting too", "can we add X in this meet as well") → \
`calendar_add_attendee`. **Never** use `set_gmeet` for this — the event \
already exists, so do NOT ask for a title or a start time, and do NOT ask \
which contact the ORIGINAL meeting was with. Pass only the NEW person.
- Just the user, no attendee → `calendar_create`.
- To cancel/delete a meeting or event → call `calendar_cancel`. Put the \
person/title in `query`; pass `start_time_iso` only if the user typed a clear \
date/time. For "cancel everything / all my meetings / both" pass \
`cancel_all=true` (keep the word "today" in `query` to limit it to today). \
Relay the tool's result verbatim-ish: if it returns `CHOOSE_EVENT` or \
`MULTIPLE_CALENDAR_MATCHES`, show that list and ask which; only say an event \
is cancelled when it returns `CALENDAR_EVENT(S)_CANCELLED`. Never invent a \
"type the exact title and time" instruction or an "all" option the tool \
didn't offer.
- To MOVE/reschedule an existing event (e.g. "change it to 10pm", "push to \
  Friday") use `calendar_reschedule` when it is enabled. It proposes an update \
  and waits for a deterministic confirmation. Never cancel and recreate an event.
- If the user writes `Name{{email@example.com}}`, pass `attendee_name` \
AND `attendee_email`.

### Gmail — reading
- For "latest emails", search with an empty query.
- For "email from X" or "about Y", use Gmail search syntax when useful \
(`from:`, `subject:`, keywords).
- Use `include_body=true` only when the user asks what an email says, wants \
a summary, or asks a question that cannot be answered from snippets.

### Summarising several emails — GROUP THEM
When asked to summarise a batch ("summarise my emails today / this week"), \
do NOT return one flat numbered list. Sort every message into these buckets \
and print only the buckets that have something in them:

*Needs you* — a real person writing to this user specifically and wanting \
something: a reply, a decision, a meeting, a document. This bucket goes FIRST \
and never gets truncated.
*Promotional* — marketing, newsletters, sales, offers, product announcements, \
anything with an unsubscribe footer. One line for the whole bucket when there \
are several ("6 promos — Tata CLiQ, Myntra, …"), not one line each.
*Cold outreach* — someone the user has no relationship with pitching a job, \
an event, a competition, a service, or a partnership. Mass-sent but from a \
named human. Recruiter and campus-hiring blasts belong here, not in Needs you.
*Other* — receipts, OTPs, statements, notifications, automated system mail. \
Collapse to a count plus the senders.

Rules for this format:
- Judge by CONTENT, not by whether the sender has a personal name. A named \
person sending a mass campaign is *Cold outreach*, not *Needs you*.
- Under *Needs you*, say what each sender actually wants in one short line, \
and name the action if there is one ("wants a call Thursday").
- Never invent an action item that is not in the mail.
- If a bucket is empty, omit the heading entirely — do not print "none".

### Sending email (compose_email)
- When the user wants to SEND / write / compose / email someone, call \
`compose_email`. That opens a form where the USER types the recipient, \
subject and body and sends it from their own Gmail.
- You must NEVER write the email for them, NEVER draft a subject or body, and \
NEVER ask for the body in chat. The form collects everything — for privacy, \
the content never goes through you.
- Pass `to` only if the user typed a literal address (e.g. "email \
john@x.com"). Otherwise leave it empty. Do not invent a recipient.
- After the tool returns `EMAIL_FORM_OPENED`, just say the form is open (e.g. \
"opened the email form — fill it in and hit send"). If it returns \
`EMAIL_FLOW_NOT_CONFIGURED` or `EMAIL_FLOW_SEND_FAILED`, relay that briefly \
and do NOT offer to send it yourself.

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
- Short, lowercase, WhatsApp-native. Dates like "Mon May 3, 9:00 AM".
- Never use emojis. Plain text only.
- If a tool returns "GOOGLE_NOT_CONNECTED" or "GMAIL_SCOPE_MISSING" → end your reply with \
SHOW_CONNECT_BUTTON.

### Boundaries
- You cannot browse the web, run code, or do arithmetic beyond simple \
date phrasing. You are a scheduling assistant only.
{active_flow_context}\
"""


# ═══════════════════════════════════════════════════════════════
# Tool Declarations (OpenAI function-calling format)
# ═══════════════════════════════════════════════════════════════

TOOLS = [
    {"type": "function", "function": {
        "name": "set_gmeet",
        "description": "Schedule a Google Meet meeting with another person. Handles contact lookup, conflict checking, and event creation.",
        "parameters": {"type": "object", "properties": {
            "attendee_name": {"type": "string", "description": "Name of the person to meet"},
            "attendee_email": {"type": "string", "description": "Email if known (skip contact search)"},
            "title": {"type": "string", "description": "The meeting TOPIC if the user gave one, e.g. 'discuss pricing with priya' -> 'pricing', 'q4 planning call' -> 'q4 planning'. Pass the user's own words. Omit the attendee's name (the server appends 'with <name>'). Leave empty ONLY if no topic was stated."},
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
        "name": "calendar_cancel",
        "description": "Cancel/delete upcoming Google Calendar event(s) and remove them from the calendar. A clear single match is deleted; a vague request lists events to choose from; cancel_all deletes everything (or just today's). Use for 'cancel my 4pm meeting', 'delete the Priya event', 'cancel all my meetings today'.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Title, attendee, topic, or keywords. Include the word 'today' to scope cancel_all to today."},
            "start_time_iso": {"type": "string", "description": "ISO datetime only if the user explicitly gave a clear date/time."},
            "cancel_all": {"type": "string", "description": "Set 'true' to cancel ALL upcoming events (or just today's if query says 'today')."},
            "limit": {"type": "string", "description": "Max upcoming events to inspect (default 50)."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "calendar_reschedule",
        "description": "Propose moving one existing calendar event to a new explicit date/time. It finds the event, checks conflicts, and shows a confirmation before updating it. Never use calendar_cancel plus calendar_create for a reschedule.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The existing event's title, attendee, or topic. Never leave blank."},
            "new_start_time_iso": {"type": "string", "description": "New ISO datetime with timezone. Use only when the user typed a clear clock time."},
            "duration_minutes": {"type": "string", "description": "Optional new duration; default keeps the event's current duration."},
        }, "required": ["query", "new_start_time_iso"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_add_attendee",
        "description": "Add another person to an EXISTING calendar event or meeting. Use for 'also invite X', 'add X to that meeting too', 'can you add X to this meet as well'. This does NOT create a new meeting and does NOT need a title or a start time — the event already has both. Never use set_gmeet to add someone to a meeting that already exists.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Which existing event: its title, topic, or an attendee already on it. Leave empty if the user just said 'this meeting' and there is only one."},
            "attendee_name": {"type": "string", "description": "Name of the person to ADD. This is the new guest, never the person already on the event."},
            "attendee_email": {"type": "string", "description": "The new guest's email, only if the user typed a literal address."},
            "start_time_iso": {"type": "string", "description": "ISO datetime of the EXISTING event, only if the user typed a clear date/time to identify it."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_meeting_summaries",
        "description": "Show the stored AI summary of a PAST meeting that araka booked and that produced a transcript (Google Meet or Zoom): headline, takeaways, decisions and action items with owners. Use for 'what were the action items from the pricing call', 'recap of yesterday's meeting', 'summary of my last meeting'. This reads existing post-meeting summaries only — it does NOT join, record, or summarise a live call.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Title, topic, person or time words to find the right past meeting. Empty means the most recent one."},
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
        "name": "gmail_search",
        "description": "Search or read the user's Gmail. Use this before answering any question about emails, inbox, senders, subjects, or email contents.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query. Empty means latest emails. Examples: 'from:priya', 'subject:invoice', 'newer_than:7d project'."},
            "limit": {"type": "string", "description": "Max messages. Omit for a sensible default. For a DAY, WEEK or date-range summary pass 30 — a mailbox holds far more than a handful, and a summary built from 3 messages is wrong."},
            "include_body": {"type": "string", "description": "'true' ONLY when the user asks what a specific email says. Leave false for day/week summaries — bodies cap the result at 5 messages, and sender plus subject is enough to summarise."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "compose_email",
        "description": "Open the email form so the USER can write and send an email from their Gmail. Use when the user wants to send/write/compose/email someone. This only OPENS a form — the user types the recipient, subject and body themselves and it sends from their Gmail. You must NOT write the email, NOT ask for the subject or body in chat, and NOT invent any content. This is for SENDING new mail; use gmail_search to read existing mail.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Optional recipient email to prefill — ONLY if the user typed a literal address (e.g. 'email john@x.com'). Leave empty otherwise. Never invent it."},
        }, "required": []},
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
    "calendar_cancel": calendar_cancel, "calendar_reschedule": calendar_reschedule,
    "calendar_add_attendee": calendar_add_attendee,
    "get_meeting_summaries": get_meeting_summaries,
    "save_note": save_note, "get_notes": get_notes, "delete_note": delete_note,
    "set_gmeet": set_gmeet, "contacts_search": contacts_search,
    "gmail_search": gmail_search, "compose_email": compose_email,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders, "delete_reminder": delete_reminder,
    "add_todo": add_todo, "get_todos": get_todos,
    "complete_todo": complete_todo, "delete_todo": delete_todo,
}

_INT_FIELDS = {"duration_minutes", "limit", "note_id", "task_id", "reminder_id", "new_duration_minutes", "relative_minutes", "item_number"}
_BOOL_FIELDS = {"with_meet", "is_recurring", "done", "include_body", "cancel_all"}

_WRITE_TOOLS = {
    "set_gmeet", "calendar_create", "calendar_cancel", "calendar_reschedule",
    "calendar_add_attendee",
    "compose_email", "save_note", "delete_note", "set_reminder",
    "delete_reminder", "add_todo", "complete_todo", "delete_todo",
}
_TIME_GUARDED_TOOLS = {
    "set_gmeet", "calendar_create", "calendar_cancel", "calendar_reschedule",
    "calendar_add_attendee", "set_reminder",
}
_MAX_TOOL_CALLS_PER_REQUEST = 4


@dataclass
class _ToolRequestState:
    """Task-local guard state; never shared between overlapping webhooks."""

    user_text: str
    allowed_tool_names: set[str] = field(default_factory=set)
    tool_calls: int = 0
    write_tool_name: str = ""


_tool_request_state: ContextVar[_ToolRequestState | None] = ContextVar(
    "tool_request_state", default=None,
)
_agent_locks: dict[str, asyncio.Lock] = {}


def _get_agent_lock(wa_id: str) -> asyncio.Lock:
    """Return the event-loop-local lock that serializes one user's agent turns."""
    lock = _agent_locks.get(wa_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_locks[wa_id] = lock
    return lock


def _serialize_user_agent(fn):
    @wraps(fn)
    async def wrapped(wa_id: str, text: str) -> str:
        async with _get_agent_lock(wa_id):
            return await fn(wa_id, text)
    return wrapped


def _request_tool_scope(fn):
    @wraps(fn)
    async def wrapped(wa_id: str, text: str) -> str:
        token = _tool_request_state.set(_ToolRequestState(user_text=text))
        try:
            return await fn(wa_id, text)
        finally:
            _tool_request_state.reset(token)
    return wrapped


def _current_tool_state() -> _ToolRequestState | None:
    return _tool_request_state.get()


# Removing an attendee is NOT a supported action, but "remove priya from the
# meeting" contains every keyword of a cancellation. `calendar_cancel` deletes
# immediately with no confirmation gate, so a misread there destroys the entire
# event to satisfy a request to drop one guest. This is the one case where
# withholding a tool earns its keep: a narrow, explicit DENY of a destructive
# action — not a guess at what the user meant. The lookahead keeps the genuine
# "remove the meeting from my calendar" on the cancel path.
_REMOVE_ATTENDEE_RE = re.compile(
    r"\b(?:remove|drop|uninvite|kick)\b\s+"
    r"(?!(?:the|this|that|my)\s+(?:meet|meeting|gmeet|call|event|appointment)\b)"
    r"[^.]{0,40}\bfrom\b[^.]{0,40}\b(?:meet|meeting|gmeet|call|event)\b"
)


def _select_tools_for_text(text: str) -> list[dict]:
    """Give the model the whole toolset and let it choose.

    This used to regex the message and hand the model one or two tools. That
    was never a safety mechanism — it was a second, far worse intent classifier
    running in front of the one we actually pay for, deciding what the model
    was even allowed to consider. It failed constantly, because natural
    language does not fit a keyword list:

        "Add yharsh499@gmail.com also as attendee" -> gmail_search
            (the literal substring "gmail" appears inside the address)
        "add akshay as attendee"                   -> no tools at all
        "can you also add rahul"                   -> no tools at all
        "add rahul also"                           -> no tools at all

    A model handed the wrong tool, or none, cannot answer correctly — so it
    improvises, and that improvisation is every hallucination we have chased:
    asking for a title and time an existing meeting already had, re-asking
    which contact to invite, repeating its own question. Picking a tool from a
    described set is the one job an LLM is reliably good at, and this function
    was taking it away.

    Nothing that actually protects the user lived here. Every real guard is
    downstream and still applies to each call: the execution-time allow-list,
    one-write-per-request, the tool-call cap, the anti-hallucination time
    check, `_confirmed` stripping, and the confirmation gates. `fast_path.py`
    still short-circuits anchored patterns BEFORE the model is consulted —
    that is a genuine latency win with zero hallucination surface, because it
    never asks the model at all.
    """
    lower = (text or "").lower()
    blocked: set[str] = set()
    if _REMOVE_ATTENDEE_RE.search(lower):
        blocked.add("calendar_cancel")
    return [t for t in TOOLS if t["function"]["name"] not in blocked]

# The callback/UI layer runs immediately after process_message returns. This
# cache carries only a structured action result across that boundary; the
# per-user agent lock prevents a second agent turn from replacing it first.
_agent_action_cache: dict[str, dict] = {}

# Cached tool signatures: tool name -> set of valid kwarg names.
_TOOL_PARAMS: dict = {}

# The model sometimes PARAPHRASES the SHOW_CONNECT_BUTTON marker instead of
# emitting it exactly ("show connect button", "[Connect Google Calendar]").
# main.py matches the exact marker only — so a paraphrase leaks to the user
# as gibberish text. Canonicalize any variant back to the exact marker.
_MARKER_PARAPHRASE_RE = re.compile(
    r"show[\s_-]*connect[\s_-]*button"      # show connect button / show_connect_button
    r"|\[\s*connect google[^\]\n]*\]",       # [Connect Google Calendar]
    re.IGNORECASE,
)

def get_last_agent_action_result(wa_id: str):
    """Retrieve and clear a structured action result for the UI layer."""
    return _agent_action_cache.pop(wa_id, None)


def get_last_gmeet_result(wa_id: str):
    """Compatibility shim for callers that only understand Google Meet."""
    cached = get_last_agent_action_result(wa_id)
    if not cached or cached.get("tool") != "set_gmeet":
        return None
    return cached.get("data")


def _requested_contact_fields(user_text: str) -> str:
    """Only reveal the specific contact detail the user asked to see."""
    lower = (user_text or "").lower()
    wants_email = "email" in lower
    wants_phone = "phone" in lower or "number" in lower
    fields = []
    if wants_email:
        fields.append("email")
    if wants_phone:
        fields.append("phone")
    return ",".join(fields)


async def _execute_tool(wa_id: str, name: str, args: dict) -> str:
    logger.info("Tool call: %s(%s)", name, ", ".join(sorted(str(k) for k in args)))
    fn = TOOL_MAP.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        state = _current_tool_state()
        if state:
            if name not in state.allowed_tool_names:
                return "ACTION_BLOCKED: that tool is not available for this request. Ask for the next detail instead."
            if state.tool_calls >= _MAX_TOOL_CALLS_PER_REQUEST:
                return "ACTION_BLOCKED: the tool-call limit for this request was reached. Stop and ask the user to continue."
            if name in _WRITE_TOOLS and state.write_tool_name:
                return (
                    "ACTION_BLOCKED: one state-changing action already ran this request "
                    f"({state.write_tool_name}). Do not run another action."
                )
            state.tool_calls += 1
            if name in _WRITE_TOOLS:
                state.write_tool_name = name

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

        # Raw user text never reaches the model as a tool parameter. It is
        # passed server-side to every time-sensitive tool so it can reject an
        # ISO timestamp invented by the model.
        user_text = state.user_text if state else ""
        if name in _TIME_GUARDED_TOOLS and "user_text" not in args:
            args["user_text"] = user_text
        if name == "contacts_search" and "requested_fields" not in args:
            args["requested_fields"] = _requested_contact_fields(user_text)
        # Force-clear `_confirmed` — only the deterministic callback
        # handler is allowed to set it. If the LLM ever tries, drop it.
        args.pop("_confirmed", None)

        # Drop junk args some models emit (empty key, hallucinated params).
        # Keep only the tool's real parameters so a bad arg can't crash it.
        # Signature reflection is cached — it's identical for every call.
        valid = _TOOL_PARAMS.get(name)
        if valid is None:
            valid = set(inspect.signature(fn).parameters) - {"wa_id"}
            _TOOL_PARAMS[name] = valid
        args = {k: v for k, v in args.items()
                if isinstance(k, str) and k in valid}

        result = await fn(wa_id, **args)

        if name in {"set_gmeet", "calendar_reschedule", "calendar_add_attendee",
                    "get_meeting_summaries"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("action"):
                    _agent_action_cache[wa_id] = {"tool": name, "data": parsed}
            except (json.JSONDecodeError, TypeError):
                pass

        return result
    except Exception as e:
        logger.error(f"Tool error ({name}): {e}")
        return f"Tool error: {e}"


# ═══════════════════════════════════════════════════════════════
# Tool-call-leak recovery
#
# Sarvam (and similar models) sometimes emit a tool call as RAW JSON in the
# message content instead of using the function-calling interface. That JSON
# then (a) gets shown to the user, who sees gibberish, (b) means the action
# never actually runs, and (c) gets saved to memory, so the model sees its own
# leaked JSON next turn and mimics it — a self-reinforcing loop. These helpers
# detect that leak and reply cleanly. A raw model response is never authority
# to run an action because the missing tool name/confirmation is ambiguous.
# ═══════════════════════════════════════════════════════════════

def _looks_like_tool_json(text: str) -> bool:
    """True if `text` is a bare JSON object (a leaked tool-arg dict)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:].strip()
    if len(t) < 2 or t[0] != "{" or t[-1] != "}":
        return False
    try:
        obj = json.loads(t)
    except Exception:
        return False
    return isinstance(obj, dict) and len(obj) > 0


# Pseudo-XML tool markup. Llama-4 / Hermes / Qwen-style models emit their tool
# call into the *content* channel when structured tool calling degrades, e.g.
#   <tool_call>calendar_create<arg_key>title</arg_key><arg_value>x</arg_value>
# `_looks_like_tool_json` only matches a bare `{...}` body, so this shape used
# to reach the user verbatim AND get stored in memory — where the model read it
# back and mimicked it. Detection is deliberately broad: anything that smells
# like tool markup must never be shown.
_TOOL_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_response|function(?:\s*=[^>]*)?|arg_key|arg_value|parameter)"
    r"\s*/?\s*>|<\|\s*python_tag\s*\|>",
    re.IGNORECASE,
)
_ARG_PAIR_RE = re.compile(
    r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
    re.IGNORECASE | re.DOTALL,
)
_LEAKED_TOOL_NAME_RE = re.compile(
    r"<tool_call>\s*([A-Za-z_][A-Za-z0-9_]*)|<function\s*=\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def _looks_like_tool_leak(text: str) -> bool:
    """True for any shape of leaked tool call — JSON body or pseudo-XML markup."""
    return _looks_like_tool_json(text) or bool(_TOOL_MARKUP_RE.search(text or ""))


def _first_url(text: str) -> str:
    """First URL in a tool result, stripped of trailing punctuation."""
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:") if m else ""


def _parse_leaked_tool_markup(text: str):
    """Recover `(tool_name, args)` from leaked pseudo-XML markup, else None.

    Strict on purpose. The reply is only recoverable when the tool name is
    explicit and every *required* parameter is present: `max_tokens` routinely
    truncates these leaks mid-call, and half a `calendar_create` is worse than
    asking the user to repeat themselves.
    """
    if not text:
        return None

    # Variant: <tool_call>{"name": "x", "arguments": {...}}</tool_call> — here the
    # blob carries the name too, so parse it before resolving the tool.
    blob = None
    if "{" in text and "}" in text:
        try:
            blob = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except Exception:
            blob = None
        if not isinstance(blob, dict):
            blob = None

    m = _LEAKED_TOOL_NAME_RE.search(text)
    name = (m.group(1) or m.group(2) or "").strip() if m else ""
    if not name and blob:
        candidate = blob.get("name") or blob.get("function")
        name = candidate.strip() if isinstance(candidate, str) else ""
    if not name:
        return None
    schema = next((t["function"] for t in TOOLS if t["function"]["name"] == name), None)
    if not schema or name not in TOOL_MAP:
        return None

    args = {k: v for k, v in _ARG_PAIR_RE.findall(text) if k}
    if not args and blob:
        inner = blob.get("arguments") or blob.get("parameters")
        if isinstance(inner, dict):
            args = inner
        elif not {"name", "function", "arguments", "parameters"} & set(blob):
            args = blob

    params = schema.get("parameters") or {}
    allowed = set((params.get("properties") or {}).keys())
    args = {k: v for k, v in args.items() if k in allowed}
    if not set(params.get("required") or []) <= set(args):
        return None  # truncated or incomplete — never guess the missing half
    return name, args


def _match_tool_by_args(args: dict):
    """Map a leaked arg-dict to the tool it was meant for.

    High-confidence only: every provided key must be a valid parameter of the
    tool. When several tools qualify, prefer the most specific (smallest param
    set), e.g. {"message","remind_at_iso"} -> set_reminder.
    """
    keys = {k for k in args if isinstance(k, str) and k}
    if not keys:
        return None
    candidates = []
    for t in TOOLS:
        fn = t["function"]
        params = set((fn.get("parameters") or {}).get("properties", {}).keys())
        if params and keys <= params:
            candidates.append((len(params), fn["name"]))
    candidates.sort()
    return candidates[0][1] if candidates else None


async def _recover_leaked_tool_json(wa_id: str, raw: str) -> str:
    """Reject leaked tool JSON without turning model text into a side effect."""
    fallback = "sorry, that didn't go through cleanly — say it once more?"
    try:
        t = raw.strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t[:4].lower() == "json":
                t = t[4:].strip()
        args = json.loads(t)
    except Exception:
        return fallback
    if not isinstance(args, dict):
        return fallback
    args = {k: v for k, v in args.items() if isinstance(k, str)}
    tool_name = _match_tool_by_args(args)
    if not tool_name:
        return fallback
    logger.warning("Rejected leaked tool JSON for wa_id=%s; inferred=%s", wa_id, tool_name)
    return fallback


# ═══════════════════════════════════════════════════════════════
# WhatsApp markup
#
# WhatsApp is NOT markdown. It understands exactly *bold*, _italic_,
# ~strike~ and ```mono``` — nothing else. The model writes standard markdown
# regardless of instructions, and `**bold**` is the visible failure: WhatsApp
# renders the INNER `*bold*` and leaves the outer asterisks on screen, so the
# user sees bold text wrapped in stray `*` characters. Headings, bullets and
# links leak through as literal syntax the same way.
#
# Normalising here rather than in the prompt because it is deterministic, and
# a formatting rule the model has to remember is a formatting rule it will
# eventually forget.
# ═══════════════════════════════════════════════════════════════

_MD_FENCE_RE = re.compile(r"```[a-zA-Z]*\n?")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.M)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def to_whatsapp_markup(text: str) -> str:
    """Rewrite markdown the model emitted into what WhatsApp actually renders."""
    if not text:
        return text
    out = _MD_FENCE_RE.sub("```", text)
    # [label](url) -> "label: url". WhatsApp shows the raw syntax otherwise,
    # and the URL is the useful half.
    out = _MD_LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", out)
    out = _MD_HEADING_RE.sub(r"*\1*", out)
    out = _MD_BOLD_RE.sub(r"*\1*", out)
    out = _MD_BOLD_UNDERSCORE_RE.sub(r"*\1*", out)
    # "- item" and "* item" both render literally; • reads as a list.
    out = _MD_BULLET_RE.sub(r"\1• ", out)
    # Strip edge BLANK LINES only — a plain .strip() also eats the leading
    # spaces on the first line, flattening a nested bullet into a top-level one.
    return _BLANK_RUN_RE.sub("\n\n", out).strip("\n")


# Per-tool context budgets. 2000 chars fits a calendar or contact result, but
# truncates a month of Gmail metadata mid-list — and a summary built from a
# silently truncated list is wrong without looking wrong.
_TOOL_RESULT_BUDGET = {
    # Measured: 30 real messages render to ~7.8k chars (header + trimmed
    # snippet). Sized above that so a week's list is never cut off mid-way —
    # a summary that silently drops its last few messages looks complete and
    # is wrong.
    "gmail_search": 9000,
    "calendar_get_all": 3000,
}


def _compact_tool_result(result: str, max_chars: int = 2000) -> str:
    """Keep model context bounded without silently cutting evidence mid-field."""
    if len(result) <= max_chars:
        return result
    lines = [line for line in result.splitlines() if line.strip()]
    kept: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if kept else 0)
        if used + extra > max_chars - 180:
            break
        kept.append(line)
        used += extra
    summary = (
        f"TOOL_RESULT_PARTIAL: showing {len(kept)} of {len(lines)} structured lines. "
        "Do not infer anything about omitted results; use a narrower query if needed."
    )
    return "\n".join([summary, *kept])


# ═══════════════════════════════════════════════════════════════
# Agent — Main Processing Loop
# ═══════════════════════════════════════════════════════════════

@_serialize_user_agent
@_request_tool_scope
async def process_message(wa_id: str, text: str) -> str:
    """Process a user message through the AI agent.

    Flow: Load memory → Build prompt → Call Groq → Handle tool calls → Return.
    """
    # 1. ONE DB round-trip for the whole pre-LLM read path: user + memory +
    #    live-state counts. (Was 3 sequential sessions; the 2h prune DELETE
    #    moved to a background job — the TTL is enforced on read instead.)
    from bot.database import Note, Reminder
    from sqlalchemy import func

    cutoff = utcnow_naive() - timedelta(hours=2)
    async with async_session() as session:
        db_user = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if not db_user:
            return "User not found. Please restart the bot."
        db_user_id = db_user.id  # Integer PK for ChatMemory
        google_connected = "Yes" if db_user.google_token_json else "No"
        user_name = db_user.display_name or db_user.first_name or "there"
        user_tz = db_user.timezone or "Asia/Kolkata"

        result = await session.execute(
            select(ChatMemory).where(
                ChatMemory.user_id == db_user_id,
                ChatMemory.created_at >= cutoff,   # 2h TTL, filtered on read
            ).order_by(ChatMemory.created_at.desc()).limit(MEMORY_WINDOW)
        )
        records = list(reversed(result.scalars().all()))

        messages = []
        for r in records:
            role = "assistant" if r.role == "model" else r.role
            # Skip a previously-leaked assistant turn (JSON body or pseudo-XML
            # markup) — replaying it teaches the model to leak again, which is
            # how one bad turn becomes every turn.
            if role == "assistant" and _looks_like_tool_leak(r.text):
                continue
            messages.append({"role": role, "content": r.text})

        messages.append({"role": "user", "content": text})
        session.add(ChatMemory(user_id=db_user_id, role="user", text=text))

        # Live state so the LLM never has to guess.
        notes_count = (await session.execute(
            select(func.count()).where(Note.user_id == db_user_id)
        )).scalar() or 0
        reminders_count = (await session.execute(
            select(func.count()).where(
                Reminder.wa_id == wa_id, Reminder.is_sent == False
            )
        )).scalar() or 0
        await session.commit()

    # 2. Build system prompt and restrict the model to the tools relevant to
    # this request. The executor independently enforces this allow-list.
    selected_tools = _select_tools_for_text(text)
    state = _current_tool_state()
    if state:
        state.allowed_tool_names = {
            tool["function"]["name"] for tool in selected_tools
        }
    # The model gets every tool now, so listing all 19 names here would just
    # duplicate the tool definitions it already receives. Only call out the
    # rare case where one was deliberately withheld.
    _all_names = {tool["function"]["name"] for tool in TOOLS}
    _withheld = sorted(_all_names - {t["function"]["name"] for t in selected_tools})
    enabled_tools = (
        "all tools listed below"
        if not _withheld else
        "all tools listed below EXCEPT " + ", ".join(_withheld)
        + " (not applicable to this request — do not mention it)"
    )
    now_ist = now_prompt_str()
    active_flow_context = (
        f"\n### Live state (from DB — trust this, not memory)"
        f"\n- Notes saved: {notes_count}"
        f"\n- Active reminders: {reminders_count}"
    )

    try:
        from bot.services.task_service import get_conversation_state
        flow_state, flow_ctx = await get_conversation_state(wa_id)
        if flow_state and flow_state != "awaiting_gmeet_confirmation":
            gd = flow_ctx.get("gmeet_data", {})
            td = flow_ctx.get("task_data", {})
            pending_attendee = gd.get("attendee_name", "") or td.get("assignee_name", "")
            pending_email = gd.get("attendee_email", "")
            pending_time = gd.get("start_time_iso", "")
            pending_title = gd.get("title", "")
            if pending_attendee or pending_time:
                # Listing what is ALREADY collected is the point: without the
                # time here, the model re-asked for a time the user had given
                # two turns earlier.
                known = []
                if pending_attendee:
                    known.append(f"attendee: {pending_attendee}")
                if pending_email:
                    known.append(f"email: {pending_email}")
                if pending_time:
                    known.append(f"time: {pending_time}")
                if pending_title:
                    known.append(f"title: {pending_title}")
                active_flow_context += (
                    "\n### In-progress meeting (continue it)\n"
                    "The user is part-way through scheduling a meeting. "
                    "Already collected — do NOT ask for any of these again:\n"
                    + "\n".join(f"- {k}" for k in known)
                    + "\nIf their new message supplies a missing piece, call "
                    "`set_gmeet` again. You may omit fields listed above; the "
                    "server remembers them. If they've changed topic, just help "
                    "with the new request instead."
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
            enabled_tools=enabled_tools,
        ),
    }

    try:
        # Offload the blocking Sarvam call to a worker thread so the shared
        # background event loop stays free — lets other users' messages be
        # processed concurrently instead of serializing behind this ~0.7s call.
        response = await asyncio.to_thread(_call_groq, system_msg, messages)

        # 4. Tool-call loop (max 5 iterations)
        tool_executed = False
        needs_connect = False  # set when a tool says Google isn't linked
        # Kept so an empty completion can still surface what the tool produced
        # (e.g. a Meet link) instead of collapsing to a bare "done.".
        last_tool_result = ""
        last_tool_name = ""
        for _ in range(5):
            choice = response.choices[0]
            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                break

            tool_executed = True
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
                if ("GOOGLE_NOT_CONNECTED" in result_content
                        or "GMAIL_SCOPE_MISSING" in result_content):
                    needs_connect = True
                result_content = _compact_tool_result(
                    result_content,
                    max_chars=_TOOL_RESULT_BUDGET.get(tc.function.name, 2000),
                )
                last_tool_result = result_content
                last_tool_name = tc.function.name

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_content,
                })

            response = await asyncio.to_thread(_call_groq, system_msg, messages)

        # 5. Extract final response
        #
        # Sarvam occasionally returns EMPTY content (most often when no tool is
        # enabled for the turn). Defaulting that to "done." asserts that
        # something happened — so "can you remove X from the meeting?" was
        # answered "done.", claiming a removal the bot cannot even perform.
        # An empty completion is a non-answer, never a success.
        # A bare "done." also throws away whatever the tool returned. When the
        # tool produced a link (Meet URL, event link), that link is the entire
        # point of the turn — "done." forces the user to ask "where is it?".
        final = (response.choices[0].message.content or "").strip()
        if not final:
            if tool_executed and last_tool_name and last_tool_name not in _WRITE_TOOLS:
                # A READ tool returned data and the model then produced
                # nothing. "done." would be a false success claim — the user
                # asked a question and nothing was done. Say what actually
                # happened so they can retry, rather than pretending.
                logger.warning(
                    "Empty completion after read tool %s (%d chars of result)",
                    last_tool_name, len(last_tool_result),
                )
                final = ("i pulled that up but couldn't summarise it — "
                         "ask me again, or narrow it down a bit?")
            elif tool_executed:
                link = _first_url(last_tool_result)
                final = f"done — {link}" if link else "done."
            else:
                final = "sorry, i didn't catch that — say it once more?"

        # Guard: the model put its tool call in the message body instead of the
        # function interface — as raw JSON, or as pseudo-XML markup
        # ("<tool_call>calendar_create<arg_key>title</arg_key>..."). Neither is
        # ever shown or stored.
        if _looks_like_tool_leak(final):
            recovered = _parse_leaked_tool_markup(final)
            if recovered:
                # Tool name is explicit and every required arg is present, so
                # the intent is unambiguous. Route it through _execute_tool so
                # all the usual guardrails still apply (allow-list, call cap,
                # one-write-per-request), then let the model narrate the real
                # result — that's what surfaces the Meet link the user asked for.
                name, args = recovered
                logger.warning("Recovered leaked tool markup for wa_id=%s: %s", wa_id, name)
                result_str = _compact_tool_result(str(await _execute_tool(wa_id, name, args)))
                if ("GOOGLE_NOT_CONNECTED" in result_str
                        or "GMAIL_SCOPE_MISSING" in result_str):
                    needs_connect = True
                if result_str.startswith("ACTION_BLOCKED"):
                    final = "sorry, that didn't go through cleanly — say it once more?"
                else:
                    tool_executed = True
                    messages.append({"role": "assistant", "content": f"(ran {name})"})
                    messages.append({"role": "user", "content": (
                        f"TOOL_RESULT for {name}: {result_str}\n"
                        "Reply to the user in one short line. Never mention tools."
                    )})
                    response = await asyncio.to_thread(_call_groq, system_msg, messages)
                    final = (response.choices[0].message.content or "").strip()
                    if not final or _looks_like_tool_leak(final):
                        link = _first_url(result_str)
                        final = f"done — {link}" if link else "done."
            elif tool_executed:
                # It already acted this turn — a trailing echo is just noise.
                final = "done."
            else:
                # It never actually called the tool — reply clean, don't guess.
                final = await _recover_leaked_tool_json(wa_id, final)

        # Canonicalize connect-button marker paraphrases (see regex above),
        # and force the marker server-side whenever a tool reported Google
        # as not connected — deterministic, never trusts the model for it.
        if _MARKER_PARAPHRASE_RE.search(final):
            final = _MARKER_PARAPHRASE_RE.sub("", final).strip()
            if not final:
                final = "connect your google account to get started."
            if "SHOW_CONNECT_BUTTON" not in final:
                final += " SHOW_CONNECT_BUTTON"
        if needs_connect and "SHOW_CONNECT_BUTTON" not in final:
            final += " SHOW_CONNECT_BUTTON"

        final = to_whatsapp_markup(final)

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
            return "i'm being rate-limited right now. try again in a minute."
        if "model output" in error_str and "empty" in error_str:
            return "that came back empty. rephrase and try again?"
        if "invalid_api_key" in error_str or ("401" in error_str and "api key" in error_str):
            return "i'm not fully configured yet. ask the admin to check the SARVAM_API_KEY in .env."
        if "connection" in error_str or "timeout" in error_str:
            return "having trouble reaching the AI right now. try again in a moment."
        return "sorry, i ran into an issue. try again in a moment."


# Sarvam occasionally 503s with "model_overloaded" — a transient provider-side
# blip, not a bug in this code. `client = OpenAI(..., max_retries=0)` was set
# deliberately so a HUNG connection fails in ~20s instead of pinning a message
# for minutes, but that same setting means a one-off 503 now surfaces
# immediately as an apology instead of quietly recovering. This retries that
# ONE specific case, once, with a short fixed backoff — separate from the
# empty-completion/tool_use_failed retries below, which are cheap local
# hiccups and get their own multi-attempt budget. Hammering an overloaded
# upstream three times back-to-back would make a real outage worse, not
# better, so this stays capped at a single extra attempt.
_OVERLOAD_RETRY_BACKOFF_S = 1.5


def _is_provider_overload(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "model_overloaded" in msg or "503" in msg
        or "service unavailable" in msg or "internal server error" in msg
    )


# A normal WhatsApp reply is a line or two, and a small cap bounds worst-case
# latency and cost. But a grouped summary of a week's inbox does not fit in
# 512 tokens: the model either stops mid-list (leaving a summary that looks
# finished and is not) or returns nothing at all. Sized by the turn instead.
_MAX_TOKENS_DEFAULT = 512
_MAX_TOKENS_LONG_ANSWER = 1600
# A tool result bigger than this means the user asked for a listing, so the
# answer needs room to render it.
_LONG_ANSWER_RESULT_CHARS = 2500


def _max_tokens_for(messages: list) -> int:
    """Give listing answers room; keep everything else short."""
    for msg in messages:
        if msg.get("role") == "tool" and len(msg.get("content") or "") > _LONG_ANSWER_RESULT_CHARS:
            return _MAX_TOKENS_LONG_ANSWER
    return _MAX_TOKENS_DEFAULT


def _call_groq(system_msg: dict, messages: list, max_retries: int = 3):
    """Call Groq with retry logic."""
    import time
    last_error = None
    overload_retried = False
    for attempt in range(max_retries):
        try:
            state = _current_tool_state()
            tool_definitions = state and [
                tool for tool in TOOLS
                if tool["function"]["name"] in state.allowed_tool_names
            ]
            request_kwargs = {
                "model": MODEL,
                "messages": [system_msg] + messages,
                "temperature": 0.1,
                "max_tokens": _max_tokens_for(messages),
            }
            if tool_definitions:
                request_kwargs["tools"] = tool_definitions
            response = client.chat.completions.create(**request_kwargs)
            # Sarvam sometimes returns a 200 with NOTHING in it — no content and
            # no tool call — most often on turns where no tool is enabled. That
            # isn't an error, so the except-branch below never sees it, and the
            # caller is left with an empty reply to show the user. Retry it.
            choice = response.choices[0]
            if (not (choice.message.content or "").strip()
                    and not choice.message.tool_calls
                    and attempt < max_retries - 1):
                logger.warning(
                    "Empty completion (attempt %d/%d); retrying",
                    attempt + 1, max_retries,
                )
                time.sleep(0.3 * (attempt + 1))
                continue
            return response
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            if (("model output" in error_msg and "empty" in error_msg) or
                    "tool_use_failed" in error_msg or "tool call validation" in error_msg):
                logger.warning(f"Groq retryable error (attempt {attempt + 1}/{max_retries}): {str(e)[:100]}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if not overload_retried and _is_provider_overload(e):
                overload_retried = True
                logger.warning(f"Sarvam overloaded, one retry: {str(e)[:100]}")
                time.sleep(_OVERLOAD_RETRY_BACKOFF_S)
                continue
            raise
    raise last_error
