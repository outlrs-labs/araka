"""A deterministic stand-in for the Groq LLM (`bot.agent._call_groq`).

It mimics a *well-behaved* model under our system prompt: it extracts a
meeting intent and emits a `set_gmeet` tool call, and — crucially — it
only includes `start_time_iso` when the user actually typed a clock time
(otherwise it leaves it empty, exactly as the prompt demands). This lets
the real agent → tool → confirmation pipeline run end-to-end in tests
without any network or API key.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from bot.utils.time import now_local
from bot.utils.intent import has_explicit_clock_time


# ── Fake OpenAI-style response objects ──────────────────────────
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, **_):
        d = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": t.id, "type": "function",
                 "function": {"name": t.function.name, "arguments": t.function.arguments}}
                for t in self.tool_calls
            ]
        return d


class _Choice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, choice):
        self.choices = [choice]


# ── Intent extraction ───────────────────────────────────────────
_NAME_RX = re.compile(r"(?:meet(?:ing)?|with|call)\s+(?:with\s+)?([A-Z][a-z]+)", re.I)
_AMPM_RX = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_H24_RX = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_name(text: str) -> str:
    m = _NAME_RX.search(text)
    if m:
        return m.group(1).strip().title()
    return ""


def _extract_iso(text: str) -> str:
    """Return an ISO datetime ONLY if the user typed an explicit clock time."""
    if not has_explicit_clock_time(text):
        return ""
    hh = mm = None
    m = _AMPM_RX.search(text)
    if m:
        hh = int(m.group(1)) % 12
        if m.group(3).lower() == "pm":
            hh += 12
        mm = int(m.group(2) or 0)
    else:
        m24 = _H24_RX.search(text)
        if m24:
            hh, mm = int(m24.group(1)), int(m24.group(2))
    if hh is None:
        return ""
    base = now_local()
    low = text.lower()
    if "tomorrow" in low:
        base = base + timedelta(days=1)
    elif "today" not in low and base.replace(hour=hh, minute=mm) <= base:
        base = base + timedelta(days=1)  # next occurrence
    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return dt.isoformat()


def make_call_groq():
    """Return a function with the same signature as bot.agent._call_groq."""
    def _fake_call_groq(system_msg, messages, max_retries: int = 3):
        # If a tool result is already in the transcript, finish with text.
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            return _Response(_Choice(
                _Message(content="Here are the details above 👆"), "stop"))

        user_msgs = [m.get("content") or "" for m in messages
                     if isinstance(m, dict) and m.get("role") == "user"]
        last = user_msgs[-1] if user_msgs else ""
        low = last.lower()

        # Pull the attendee from the latest message, else from history — this
        # mirrors a real model carrying the pending attendee across turns.
        name = _extract_name(last)
        if not name:
            for um in reversed(user_msgs[:-1]):
                name = _extract_name(um)
                if name:
                    break

        iso = _extract_iso(last)
        email_m = _EMAIL_RX.search(last)
        meet_intent = any(k in low for k in ("meet", "meeting", "schedule", "call with"))

        # Schedule if there's clear meeting intent, OR the user just supplied a
        # follow-up time / email for an attendee we already know about.
        if name and (meet_intent or iso or email_m):
            args = {"attendee_name": name, "title": ""}
            if iso:
                args["start_time_iso"] = iso
            if email_m:
                args["attendee_email"] = email_m.group(0)
            tc = _ToolCall("call_1", "set_gmeet", json.dumps(args))
            return _Response(_Choice(_Message(tool_calls=[tc]), "tool_calls"))

        return _Response(_Choice(
            _Message(content="Got it! Tell me who to meet and when."), "stop"))

    return _fake_call_groq


def make_parse_datetime():
    """Deterministic replacement for task_service.parse_datetime_from_text."""
    async def _fake_parse_datetime(text, timezone_name=None):
        iso = _extract_iso(text)
        return {"date_iso": iso or None, "confidence": 0.9 if iso else 0.0}
    return _fake_parse_datetime
