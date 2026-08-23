"""Post-meeting transcripts → per-speaker summaries.

STATUS: complete and wired, but INERT. `config.MEET_TRANSCRIPTS_ENABLED`
defaults to false, so the polling job is never even registered. Flip it on
once the two prerequisites below are real — no code change needed.

Why it ships switched off
─────────────────────────
Google Meet only *generates* a transcript on Workspace Business Standard,
Business Plus, Enterprise Standard, Enterprise Plus or Education Plus — or, on
a personal account, Google One Premium (2 TB) or Workspace Individual. A plain
free @gmail.com never produces one, so there is nothing for any API to fetch
and no way to test this end to end. araka's current accounts are free Gmail.

To switch it on, in order:
  1. Confirm the meeting organiser's account tier (above). Nothing else
     matters if the account cannot produce a transcript.
  2. Enable the Google Meet API on the Cloud project. Requesting a scope
     whose API is off makes the whole consent screen fail with
     `invalid_scope`, which is why step 3 comes after this one.
  3. Set `MEET_TRANSCRIPTS_ENABLED=true` in `.env` and restart. That single
     flag both registers the polling job AND adds the Meet scope to the
     consent request (see `config.google_scopes()`) — deliberately the same
     switch, so the scope can never be asked for before step 2 is done.
  4. Every user reconnects once, to grant the newly-requested scope.

Zoom needs none of the above — its auth is account-level. Set
ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET and it works for any
meeting whose link points at zoom.us, independently of Google entirely.

Shape
─────
    transcript_source   the contract — TranscriptContext, Participant,
                        TranscriptSegment. The ONLY thing the summariser sees,
                        which is what lets Zoom slot in without touching it.
    meet_source         Google Meet REST v2: link -> conference -> entries
    zoom_source         Zoom Cloud Recording + a tested WebVTT parser
    summariser          TranscriptContext -> MeetingSummary, via the LLM
    pipeline            the orchestrator: find -> fetch -> summarise ->
                        store -> deliver. Idempotent by design.
    prompts/join_meet_prompt.md   the summarisation prompt, versioned as text

Data flow
─────────
    Task.meeting_link                    already stored on every booking
      -> meet_source.resolve_conference  meeting code -> conferenceRecord
      -> MeetTranscriptSource.fetch      -> TranscriptContext
      -> summariser.summarise            -> MeetingSummary
      -> meeting_summaries table         one row per meeting = never redone
      -> WhatsApp                        condensed, per-speaker

The `meeting_summaries` row is the idempotency key: written BEFORE delivery,
so a failed send retries the send alone and never re-pays for the LLM call.
"""

from bot.services.meetings.transcript_source import (  # noqa: F401
    MeetingRef, Participant, TranscriptContext, TranscriptSegment,
    TranscriptSource, TranscriptUnavailable,
)
