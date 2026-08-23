"""Zoom transcript source — design + VTT parser, not yet wired.

Zoom differs from Google Meet in three ways that matter, and in nothing else —
which is the point of `TranscriptSource`. Once `fetch` returns a
`TranscriptContext`, `summariser.py` and `join_meet_prompt.md` are reused
verbatim.

1. AUTH is account-level, not per-user.
   Google uses araka's existing user-delegated OAuth. Zoom wants a
   Server-to-Server OAuth app: account_id + client_id + client_secret, exchanged
   for a short-lived token. There is no per-user consent screen, so this does
   NOT go through `bot/services/google_auth.py` — it needs its own credentials
   in config and its own token cache.

   Scopes: `cloud_recording:read:list_recording_files:admin`
           (plus `meeting:read:meeting:admin` to resolve a meeting id)

2. FETCH returns a file, not structured entries.
       GET /v2/meetings/{meetingId}/recordings
         -> recording_files[] where file_type == "TRANSCRIPT"
         -> download_url (append ?access_token=... or send the bearer header)
   The payload is WebVTT, so speaker attribution arrives as a text convention
   ("Name: line") rather than a participant id. `parse_vtt` below normalises
   that into the same segments/participants shape.

3. DELIVERY can be push instead of polling.
   Zoom emits `recording.transcript_completed`. That fits araka's existing
   Flask webhook pattern — including signature verification, which Zoom does
   with an HMAC over the raw body much like Meta's X-Hub-Signature-256. This is
   arguably easier than the Google side, where polling is the only option.

Requires Zoom Pro or above with cloud recording enabled; transcript language
coverage is narrower than Meet's.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import requests

from bot.config import config
from bot.services.meetings.transcript_source import (
    MeetingRef, Participant, TranscriptContext, TranscriptSegment,
    TranscriptUnavailable, tally_participants,
)

logger = logging.getLogger(__name__)

PROVIDER = "zoom"

# 00:00:12.400 --> 00:00:18.100
_TIMING_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
# "Akshay Kumar: we should ship it"
_SPEAKER_RE = re.compile(r"^([^:]{1,60}):\s*(.*)$")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(vtt_text: str, title: str = "",
              conference_id: str = "") -> TranscriptContext:
    """Turn a Zoom WebVTT transcript into a TranscriptContext.

    Zoom identifies speakers by display name inside the cue text, so names are
    the only stable key available — unlike Meet, which returns a participant
    resource id. Names are therefore used as ids here; two attendees sharing a
    display name will merge, which is a real Zoom limitation, not a bug to fix
    downstream.
    """
    people: dict[str, Participant] = {}
    segments: list[TranscriptSegment] = []
    start_s = end_s = 0.0

    for raw_line in (vtt_text or "").splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.isdigit():
            continue

        timing = _TIMING_RE.match(line)
        if timing:
            g = timing.groups()
            start_s, end_s = _seconds(*g[:4]), _seconds(*g[4:])
            continue

        speaker_id, text = None, line
        match = _SPEAKER_RE.match(line)
        if match:
            name = match.group(1).strip()
            text = match.group(2).strip()
            if name:
                speaker_id = name
                people.setdefault(speaker_id, Participant(id=name, display_name=name))
        if not text:
            continue
        segments.append(TranscriptSegment(
            speaker_id=speaker_id, text=text,
            start_offset_s=start_s, end_offset_s=end_s,
        ))

    participants = tally_participants(segments, people)
    duration = int(max((s.end_offset_s for s in segments), default=0) // 60)
    return TranscriptContext(
        provider=PROVIDER, conference_id=conference_id, title=title,
        duration_minutes=duration, participants=participants, segments=segments,
    )


_TOKEN_URL = "https://zoom.us/oauth/token"
_API_BASE = "https://api.zoom.us/v2"
_HTTP_TIMEOUT = 20

# Server-to-Server tokens last an hour. Cached module-level because it is
# account-wide, not per-user — re-minting one per meeting would burn quota for
# nothing. Refreshed early so a token never expires mid-request.
_TOKEN_EARLY_REFRESH_S = 120
_token_cache: dict = {"access_token": "", "expires_at": 0.0}


def meeting_id_from_link(join_url: str) -> str:
    """Pull the numeric meeting id out of a Zoom join URL.

    Zoom links look like https://us02web.zoom.us/j/85512345678?pwd=... — the
    digits after /j/ are the meeting id the API indexes recordings by.
    """
    match = re.search(r"zoom\.us/(?:j|s|w)/(\d{9,12})", join_url or "")
    return match.group(1) if match else ""


def _access_token_sync() -> str:
    """Mint (or reuse) an account-level access token."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    if not config.zoom_configured():
        raise TranscriptUnavailable("Zoom credentials are not configured")

    resp = requests.post(
        _TOKEN_URL,
        params={"grant_type": "account_credentials",
                "account_id": config.ZOOM_ACCOUNT_ID},
        auth=(config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET),
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        # Bad credentials never fix themselves — say so permanently rather
        # than letting the poller retry every 10 minutes forever.
        raise TranscriptUnavailable(
            f"Zoom auth failed ({resp.status_code}): {resp.text[:120]}"
        )
    payload = resp.json()
    token = payload.get("access_token", "")
    if not token:
        raise TranscriptUnavailable("Zoom returned no access token")
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = (
        now + max(60, int(payload.get("expires_in", 3600)) - _TOKEN_EARLY_REFRESH_S)
    )
    return token


def _fetch_sync(ref: MeetingRef) -> Optional[TranscriptContext]:
    """Blocking implementation — always invoked through a thread."""
    meeting_id = ref.conference_id or meeting_id_from_link(ref.external_event_id)
    if not meeting_id:
        raise TranscriptUnavailable("no Zoom meeting id on this meeting")

    token = _access_token_sync()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{_API_BASE}/meetings/{meeting_id}/recordings",
        headers=headers, timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        # No cloud recording for this meeting, and there never will be.
        raise TranscriptUnavailable("no cloud recording for this meeting")
    if resp.status_code in (401, 403):
        raise TranscriptUnavailable(
            f"Zoom denied access ({resp.status_code}) — check the app's "
            "cloud_recording scope"
        )
    if resp.status_code != 200:
        # Transient (429, 5xx): return None so the poller tries again.
        logger.warning(
            "Zoom recordings lookup failed for %s: %s %s",
            meeting_id, resp.status_code, resp.text[:120],
        )
        return None

    data = resp.json()
    files = data.get("recording_files") or []
    transcript = next(
        (f for f in files if (f.get("file_type") or "").upper() == "TRANSCRIPT"),
        None,
    )
    if not transcript:
        # Recording exists but the transcript is still processing — Zoom
        # publishes audio first. Retry later rather than giving up.
        logger.info("Zoom transcript not ready yet for %s", meeting_id)
        return None

    download_url = transcript.get("download_url") or ""
    if not download_url:
        return None

    # The download endpoint takes the same bearer token.
    vtt = requests.get(download_url, headers=headers, timeout=_HTTP_TIMEOUT)
    if vtt.status_code != 200:
        logger.warning(
            "Zoom transcript download failed for %s: %s",
            meeting_id, vtt.status_code,
        )
        return None

    ctx = parse_vtt(
        vtt.text,
        title=ref.title or data.get("topic", "") or "",
        conference_id=str(meeting_id),
    )
    if not ctx.segments:
        return None
    if ref.start:
        ctx.start = ref.start
    # Zoom reports the real meeting length; the VTT only knows its last cue.
    api_duration = data.get("duration")
    if isinstance(api_duration, int) and api_duration > 0:
        ctx.duration_minutes = api_duration
    return ctx


class ZoomTranscriptSource:
    """TranscriptSource for Zoom."""

    provider = PROVIDER

    async def fetch(self, user_db, ref: MeetingRef) -> Optional[TranscriptContext]:
        # user_db is unused: Zoom auth is account-level, not per-user. Kept in
        # the signature so both providers satisfy the same interface.
        return await asyncio.to_thread(_fetch_sync, ref)
