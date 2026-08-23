"""TranscriptContext -> MeetingSummary, via the LLM.

Provider-agnostic by construction: this module only ever sees a
`TranscriptContext`, so it works identically for Meet and Zoom.

Deliberately NOT routed through `bot.agent.process_message`. That path is
tuned for short WhatsApp replies (`max_tokens=512`) and carries the whole
19-tool schema on every call — neither is right for summarising 40 minutes of
transcript, and the token cap would truncate the answer mid-JSON.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

from bot.config import config
from bot.services.meetings.transcript_source import TranscriptContext

logger = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "join_meet_prompt.md")

# A summary is not a chat reply: it needs room, and nobody is watching a typing
# indicator while it runs.
_SUMMARY_MAX_TOKENS = 2048
_SUMMARY_TIMEOUT_S = 90

# Guard against one enormous meeting blowing the context window.
_MAX_SEGMENTS = 1200


@dataclass
class ActionItem:
    task: str
    owner: Optional[str] = None
    due: Optional[str] = None


@dataclass
class Decision:
    decision: str
    decided_by: Optional[str] = None


@dataclass
class MeetingSummary:
    headline: str = ""
    key_takeaways: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    per_person: list[dict] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)

    def to_whatsapp(self) -> str:
        """Render for a WhatsApp message — plain text, no markdown tables."""
        lines: list[str] = []
        if self.headline:
            lines.append(self.headline)
        if self.key_takeaways:
            lines.append("\n*Takeaways*")
            lines += [f"• {t}" for t in self.key_takeaways]
        if self.decisions:
            lines.append("\n*Decisions*")
            for d in self.decisions:
                who = f" — {d.decided_by}" if d.decided_by else ""
                lines.append(f"• {d.decision}{who}")
        if self.action_items:
            lines.append("\n*Action items*")
            for a in self.action_items:
                who = a.owner or "unassigned"
                due = f" ({a.due})" if a.due else ""
                lines.append(f"• {a.task} — {who}{due}")
        if self.per_person:
            lines.append("\n*Who said what*")
            for p in self.per_person:
                lines.append(f"• {p.get('name', '?')}: {p.get('summary', '')}")
        return "\n".join(lines).strip()


def load_prompt() -> str:
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def build_payload(ctx: TranscriptContext) -> dict:
    """The JSON the prompt consumes, with a ceiling on segment count."""
    payload = ctx.to_json_dict()
    segments = payload.get("segments", [])
    if len(segments) > _MAX_SEGMENTS:
        # Keep the head and tail: openings set context, endings carry the
        # decisions and action items. Dropping the middle is the least-bad cut.
        half = _MAX_SEGMENTS // 2
        payload["segments"] = segments[:half] + segments[-half:]
        payload["meeting"]["truncated"] = True
        logger.warning(
            "Transcript truncated for summarisation: %d -> %d segments",
            len(segments), len(payload["segments"]),
        )
    return payload


def _parse(raw: str) -> MeetingSummary:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    return MeetingSummary(
        headline=data.get("headline", "") or "",
        key_takeaways=list(data.get("key_takeaways") or []),
        decisions=[
            Decision(decision=d.get("decision", ""), decided_by=d.get("decided_by"))
            for d in (data.get("decisions") or []) if d.get("decision")
        ],
        action_items=[
            ActionItem(task=a.get("task", ""), owner=a.get("owner"), due=a.get("due"))
            for a in (data.get("action_items") or []) if a.get("task")
        ],
        per_person=list(data.get("per_person") or []),
        unclear=list(data.get("unclear") or []),
    )


def summarise_sync(ctx: TranscriptContext) -> Optional[MeetingSummary]:
    """Blocking. Call via a thread — see `summarise`."""
    if not ctx.segments:
        return None
    prompt = load_prompt().replace(
        "{transcript_json}", json.dumps(build_payload(ctx), ensure_ascii=False)
    )
    client = OpenAI(
        base_url=config.SARVAM_BASE_URL, api_key=config.SARVAM_API_KEY,
        timeout=_SUMMARY_TIMEOUT_S, max_retries=1,
    )
    try:
        resp = client.chat.completions.create(
            model=config.SARVAM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
        return _parse(resp.choices[0].message.content or "")
    except json.JSONDecodeError as e:
        # A malformed summary is dropped, never shown half-parsed.
        logger.error("Summary JSON parse failed for %s: %s", ctx.conference_id, e)
        return None
    except Exception as e:
        logger.error("Summarisation failed for %s: %s", ctx.conference_id, e)
        return None


async def summarise(ctx: TranscriptContext) -> Optional[MeetingSummary]:
    """Summarise off the event loop."""
    import asyncio
    return await asyncio.to_thread(summarise_sync, ctx)
