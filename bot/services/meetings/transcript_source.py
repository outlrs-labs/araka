"""The provider-agnostic transcript contract.

Every provider normalises into `TranscriptContext`, which is the ONLY thing the
summariser ever sees. That is what lets Zoom be added later without touching
the prompt or the summarisation code.

Speaker attribution is first-class here, not an afterthought: the whole point
of the feature is answering "who said what / who owns this action", so
`Participant` carries per-person totals and every `TranscriptSegment` names its
speaker. A provider that cannot attribute a line must say so explicitly with
`speaker_id=None` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Protocol


class TranscriptUnavailable(Exception):
    """No transcript exists for this meeting, and none ever will.

    Raised for the permanent cases — the account tier cannot produce
    transcripts, or transcription was off for the call. Callers should give up
    rather than retry. A transcript that is merely *not ready yet* is a
    different thing: return None from `fetch` so the poller tries again.
    """


@dataclass
class MeetingRef:
    """Enough to find one meeting at a provider."""
    provider: str                        # "google_meet" | "zoom"
    external_event_id: str = ""          # araka's Task.external_event_id
    conference_id: str = ""              # provider conference/meeting id
    title: str = ""
    start: Optional[datetime] = None
    organiser_email: str = ""


@dataclass
class Participant:
    id: str                              # stable within one transcript
    display_name: str = ""
    email: str = ""
    speaking_seconds: float = 0.0
    turns: int = 0


@dataclass
class TranscriptSegment:
    speaker_id: Optional[str]            # None = provider could not attribute
    text: str
    start_offset_s: float = 0.0
    end_offset_s: float = 0.0


@dataclass
class TranscriptContext:
    """The JSON handed to the LLM. Keep it stable — the prompt depends on it."""
    provider: str
    conference_id: str
    title: str
    start: Optional[datetime] = None
    duration_minutes: int = 0
    participants: list[Participant] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def participant_count(self) -> int:
        return len(self.participants)

    def to_json_dict(self) -> dict:
        """Serialise to the exact shape `join_meet_prompt.md` documents."""
        return {
            "meeting": {
                "provider": self.provider,
                "conference_id": self.conference_id,
                "title": self.title,
                "start": self.start.isoformat() if self.start else None,
                "duration_minutes": self.duration_minutes,
            },
            "participants": {
                "count": self.participant_count,
                "people": [asdict(p) for p in self.participants],
            },
            "segments": [asdict(s) for s in self.segments],
        }


def tally_participants(segments: list[TranscriptSegment],
                       people: dict[str, Participant]) -> list[Participant]:
    """Fill in speaking_seconds / turns from the segments.

    Providers give per-line timings but no per-person totals, and those totals
    are what let the summary say who drove the discussion. Computed here so
    every provider reports them identically.
    """
    for seg in segments:
        person = people.get(seg.speaker_id) if seg.speaker_id else None
        if person is None:
            continue
        person.turns += 1
        span = (seg.end_offset_s or 0.0) - (seg.start_offset_s or 0.0)
        if span > 0:
            person.speaking_seconds += span
    for person in people.values():
        person.speaking_seconds = round(person.speaking_seconds, 1)
    return list(people.values())


class TranscriptSource(Protocol):
    """What every provider implements."""

    provider: str

    async def fetch(self, user_db, ref: MeetingRef) -> Optional[TranscriptContext]:
        """Return the transcript, or None if it is not ready yet.

        Raises TranscriptUnavailable when it will never be ready.
        """
        ...
