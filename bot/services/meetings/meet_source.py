"""Google Meet REST v2 transcript source.

UNTESTED — see `bot/services/meetings/__init__.py` for the account-tier gate.
The code is written against the documented API shape but has never run against
a real conference, because no araka account can currently produce a transcript.

API path (three calls):

    conferenceRecords.list                        find the finished conference
      filter: space.meeting_code / start_time
    conferenceRecords.transcripts.list            the transcript handle
    conferenceRecords.transcripts.entries.list    speaker + text + timings

Scope: https://www.googleapis.com/auth/meetings.space.created
Narrower than `meetings.space.readonly` and sufficient, because araka creates
every event it books — so every conference it cares about is one it created.

Transcripts appear shortly AFTER a call ends, not during it, which is why this
is a post-hoc poller and not a bot that joins the meeting.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from googleapiclient.discovery import build

from bot.services.google_auth import get_google_creds
from bot.services.meetings.transcript_source import (
    MeetingRef, Participant, TranscriptContext, TranscriptSegment,
    TranscriptUnavailable, tally_participants,
)

logger = logging.getLogger(__name__)

MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"
PROVIDER = "google_meet"

# Google returns entries paginated; keep a ceiling so one very long meeting
# can't pin a worker thread indefinitely.
_MAX_ENTRY_PAGES = 20


def _service(user_db):
    creds = get_google_creds(user_db)
    return build("meet", "v2", credentials=creds) if creds else None


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _fetch_sync(user_db, ref: MeetingRef) -> Optional[TranscriptContext]:
    """Blocking implementation — always invoked through a thread."""
    svc = _service(user_db)
    if not svc:
        raise TranscriptUnavailable("Google not connected")

    conference_id = ref.conference_id
    if not conference_id:
        raise TranscriptUnavailable("no conference id on this meeting")

    transcripts = svc.conferenceRecords().transcripts().list(
        parent=conference_id,
    ).execute().get("transcripts", [])
    if not transcripts:
        # Ambiguous on purpose: could be "not ready yet" or "never recorded".
        # The poller retries for a bounded window, then gives up.
        return None

    transcript_name = transcripts[0].get("name")
    if not transcript_name:
        return None

    entries, page_token, pages = [], None, 0
    while pages < _MAX_ENTRY_PAGES:
        resp = svc.conferenceRecords().transcripts().entries().list(
            parent=transcript_name, pageToken=page_token,
        ).execute()
        entries.extend(resp.get("transcriptEntries", []))
        page_token = resp.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    if not entries:
        return None

    base = _parse_ts(entries[0].get("startTime", ""))
    people: dict[str, Participant] = {}
    segments: list[TranscriptSegment] = []

    for entry in entries:
        # `participant` is a resource name; it is the stable speaker key.
        speaker_id = entry.get("participant") or None
        if speaker_id and speaker_id not in people:
            people[speaker_id] = Participant(
                id=speaker_id,
                display_name=entry.get("participantDisplayName", "") or "",
            )
        start = _parse_ts(entry.get("startTime", ""))
        end = _parse_ts(entry.get("endTime", ""))
        segments.append(TranscriptSegment(
            speaker_id=speaker_id,
            text=(entry.get("text") or "").strip(),
            start_offset_s=(start - base).total_seconds() if start and base else 0.0,
            end_offset_s=(end - base).total_seconds() if end and base else 0.0,
        ))

    participants = tally_participants(segments, people)
    duration = 0
    if segments:
        duration = int(max(s.end_offset_s for s in segments) // 60)

    return TranscriptContext(
        provider=PROVIDER,
        conference_id=conference_id,
        title=ref.title,
        start=ref.start or base,
        duration_minutes=duration,
        participants=participants,
        segments=[s for s in segments if s.text],
    )


class MeetTranscriptSource:
    """TranscriptSource for Google Meet."""

    provider = PROVIDER

    async def fetch(self, user_db, ref: MeetingRef) -> Optional[TranscriptContext]:
        return await asyncio.to_thread(_fetch_sync, user_db, ref)


def meeting_code_from_link(meeting_link: str) -> str:
    """Pull the Meet code out of a hangoutLink.

    araka already stores the full link on every Task it books
    (`https://meet.google.com/abc-defg-hij`), and that trailing code is the
    key the Meet API indexes spaces by — so no Calendar round trip is needed
    to find the conference.
    """
    if not meeting_link:
        return ""
    match = re.search(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})",
                      meeting_link, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _resolve_conference_sync(user_db, meeting_link: str,
                             started_after: Optional[datetime] = None) -> str:
    """meeting link -> conferenceRecord name, or "" if none exists yet."""
    code = meeting_code_from_link(meeting_link)
    if not code:
        return ""
    svc = _service(user_db)
    if not svc:
        raise TranscriptUnavailable("Google not connected")

    space = svc.spaces().get(name=f"spaces/{code}").execute()
    space_name = space.get("name")
    if not space_name:
        return ""

    # A space is reused across recurring meetings, so filter by start time to
    # avoid picking up an older conference on the same link.
    filters = [f'space.name="{space_name}"']
    if started_after:
        filters.append(f'start_time>="{started_after.isoformat()}"')
    resp = svc.conferenceRecords().list(filter=" AND ".join(filters)).execute()
    records = resp.get("conferenceRecords", [])
    if not records:
        return ""
    # Most recent first — the conference that just ended.
    records.sort(key=lambda r: r.get("startTime", ""), reverse=True)
    return records[0].get("name", "")


async def resolve_conference(user_db, meeting_link: str,
                             started_after: Optional[datetime] = None) -> str:
    """Async wrapper — see `_resolve_conference_sync`."""
    return await asyncio.to_thread(
        _resolve_conference_sync, user_db, meeting_link, started_after
    )
