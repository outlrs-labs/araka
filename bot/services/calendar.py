"""FollowUp Bot — Google Calendar CRUD + conflict detection + slot finder."""

import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from bot.services.google_auth import get_google_creds
from bot.config import config

from bot.utils.aio import offloaded

logger = logging.getLogger(__name__)


def _get_service(user_db):
    """Build a Calendar v3 service for the given user."""
    creds = get_google_creds(user_db)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


# ═══════════════════════════════════════════════════════════════
# CRUD
# ═══════════════════════════════════════════════════════════════


# Signature stamped into every event araka creates — which covers every Meet
# event, since a Meet link can only come from this function. Applied here
# rather than at the call sites so no future caller can forget it.
ARAKA_EVENT_TAG = "Booked by araka"


def _with_araka_tag(description: str = None) -> str:
    """Append the araka signature, without duplicating it on re-writes."""
    text = (description or "").strip()
    if not text:
        return ARAKA_EVENT_TAG
    if ARAKA_EVENT_TAG.lower() in text.lower():
        return text
    return f"{text}\n\n{ARAKA_EVENT_TAG}"


@offloaded
def create_event(
    user_db, title: str, event_dt: datetime,
    duration_minutes: int = 60, description: str = None,
    meet_link: bool = False, attendees: list = None,
) -> dict:
    """Create a calendar event. Returns {id, link, meet}."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    tz = config.BOT_TIMEZONE
    # Enforce IST if naive datetime reaches this layer
    if event_dt.tzinfo is None:
        import pytz
        event_dt = pytz.timezone(tz).localize(event_dt)
    end_dt = event_dt + timedelta(minutes=duration_minutes)

    # Use full ISO 8601 with timezone offset for reliable time handling
    start_iso = event_dt.isoformat()
    end_iso = end_dt.isoformat()

    body = {
        "summary": title,
        "description": _with_araka_tag(description),
        "start": {"dateTime": start_iso, "timeZone": tz},
        "end": {"dateTime": end_iso, "timeZone": tz},
    }

    if attendees:
        body["attendees"] = [{"email": e.strip()} for e in attendees]

    conf_version = 0
    if meet_link:
        # requestId MUST be alphanumeric + hyphens only (no dots!)
        import uuid
        req_id = f"followup-{uuid.uuid4().hex[:12]}"
        body["conferenceData"] = {
            "createRequest": {
                "requestId": req_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        conf_version = 1

    event = svc.events().insert(
        calendarId="primary", body=body,
        conferenceDataVersion=conf_version,
        sendUpdates="all" if attendees else "none",
    ).execute()

    return {
        "id": event["id"],
        "link": event.get("htmlLink", ""),
        "meet": event.get("hangoutLink", ""),
    }


@offloaded
def get_all_events(user_db, max_results: int = 10) -> list:
    """List upcoming calendar events."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    now = datetime.now(timezone.utc).isoformat()
    result = svc.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


@offloaded
def delete_event(user_db, event_id: str):
    """Delete a calendar event by ID."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")
    svc.events().delete(calendarId="primary", eventId=event_id).execute()
    return True


@offloaded
def day_busy(user_db, day_local: datetime) -> list:
    """Return [(start, end)] busy intervals for the local calendar day."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")
    import pytz
    tz = pytz.timezone(config.BOT_TIMEZONE)
    start = day_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if start.tzinfo is None:
        start = tz.localize(start)
    end = start + timedelta(days=1)
    res = svc.events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime",
    ).execute()
    busy = []
    for ev in res.get("items", []):
        es = ev.get("start", {}).get("dateTime", "")
        ee = ev.get("end", {}).get("dateTime", "")
        if es and ee:
            try:
                busy.append((datetime.fromisoformat(es), datetime.fromisoformat(ee)))
            except (ValueError, TypeError):
                continue
    return busy


@offloaded
def update_event(
    user_db, event_id: str,
    title: str = None, start_iso: str = None, duration_minutes: int = None,
) -> str:
    """Update an existing calendar event. Returns the HTML link."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    event = svc.events().get(calendarId="primary", eventId=event_id).execute()

    if title:
        event["summary"] = title
    if start_iso:
        dt = datetime.fromisoformat(start_iso)
        # Enforce IST if naive
        if dt.tzinfo is None:
            import pytz
            dt = pytz.timezone(config.BOT_TIMEZONE).localize(dt)
        dur = duration_minutes or 60
        tz = config.BOT_TIMEZONE
        end_dt = dt + timedelta(minutes=dur)
        event["start"] = {"dateTime": dt.isoformat(), "timeZone": tz}
        event["end"] = {"dateTime": end_dt.isoformat(), "timeZone": tz}

    updated = svc.events().update(
        calendarId="primary", eventId=event_id, body=event,
    ).execute()
    return updated.get("htmlLink", "")


# ═══════════════════════════════════════════════════════════════
# Conflict detection
# ═══════════════════════════════════════════════════════════════


@offloaded
def add_attendees(user_db, event_id: str, emails: list) -> dict:
    """Add guest(s) to an existing event without disturbing anything else.

    Uses `patch` rather than `update` so a title/time edit made elsewhere
    isn't clobbered, and MERGES into the existing attendee list instead of
    replacing it — replacing would silently uninvite everyone already on the
    event. `sendUpdates="all"` makes Google email the new guests.

    Returns {"summary", "start", "added": [...], "already": [...]}.
    """
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    event = svc.events().get(calendarId="primary", eventId=event_id).execute()
    existing = event.get("attendees", []) or []
    existing_emails = {
        (a.get("email") or "").strip().lower() for a in existing if a.get("email")
    }

    added, already = [], []
    for raw in emails:
        addr = (raw or "").strip()
        if not addr:
            continue
        if addr.lower() in existing_emails:
            already.append(addr)
            continue
        existing.append({"email": addr})
        existing_emails.add(addr.lower())
        added.append(addr)

    if added:
        svc.events().patch(
            calendarId="primary", eventId=event_id,
            body={"attendees": existing},
            sendUpdates="all",
        ).execute()

    return {
        "summary": event.get("summary", "Untitled"),
        "start": (event.get("start") or {}).get("dateTime")
                 or (event.get("start") or {}).get("date") or "",
        "meet_link": event.get("hangoutLink", "") or "",
        "added": added,
        "already": already,
    }


@offloaded
def find_conflicts(
    user_db, proposed_start: datetime, duration_minutes: int = 30,
) -> list:
    """Return a list of events that overlap [proposed_start, proposed_start + duration].

    Boundary-exclusive: adjacent events (end == start) are NOT conflicts.
    """
    import pytz

    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    tz_obj = pytz.timezone(config.BOT_TIMEZONE)

    # Ensure proposed_start is timezone-aware (IST)
    if proposed_start.tzinfo is None:
        proposed_start = tz_obj.localize(proposed_start)

    proposed_end = proposed_start + timedelta(minutes=duration_minutes)

    # Widen query window to catch edge overlaps
    q_start = proposed_start - timedelta(hours=2)
    q_end = proposed_end + timedelta(hours=2)

    # CRITICAL: Use .isoformat() to preserve timezone offset.
    # strftime("%Y-%m-%dT%H:%M:%S") strips the offset, causing Google
    # Calendar API to interpret the times as UTC — shifting the query
    # window by 5.5 hours and missing all events at the proposed time.
    logger.info(
        f"Conflict check: proposed {proposed_start.isoformat()} – "
        f"{proposed_end.isoformat()}, query window {q_start.isoformat()} – "
        f"{q_end.isoformat()}"
    )

    result = svc.events().list(
        calendarId="primary",
        timeMin=q_start.isoformat(),
        timeMax=q_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events_found = result.get("items", [])
    logger.info(f"Conflict check found {len(events_found)} events in window")

    conflicts = []
    for ev in events_found:
        es = ev["start"].get("dateTime", ev["start"].get("date", ""))
        ee = ev["end"].get("dateTime", ev["end"].get("date", ""))
        if not es or not ee:
            continue
        try:
            ev_start = datetime.fromisoformat(es)
            ev_end = datetime.fromisoformat(ee)
        except (ValueError, TypeError):
            continue

        # Normalize both sides to the same timezone for reliable comparison
        if ev_start.tzinfo and proposed_end.tzinfo:
            ev_start = ev_start.astimezone(tz_obj)
            ev_end = ev_end.astimezone(tz_obj)
            p_end = proposed_end.astimezone(tz_obj)
            p_start = proposed_start.astimezone(tz_obj)
        else:
            p_end = proposed_end
            p_start = proposed_start

        if ev_start < p_end and ev_end > p_start:
            logger.info(f"Conflict: '{ev.get('summary', 'Untitled')}' {es} – {ee}")
            conflicts.append({
                "id": ev["id"],
                "summary": ev.get("summary", "Untitled"),
                "start": es,
                "end": ee,
            })

    return conflicts


# ═══════════════════════════════════════════════════════════════
# Slot finder
# ═══════════════════════════════════════════════════════════════


@offloaded
def find_free_slots(
    user_db, around_time: datetime,
    duration_minutes: int = 30, count: int = 3,
) -> list:
    """Find available 30-min-boundary slots near *around_time*."""
    import pytz

    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    tz_obj = pytz.timezone(config.BOT_TIMEZONE)

    # Ensure around_time is timezone-aware (IST)
    if around_time.tzinfo is None:
        around_time = tz_obj.localize(around_time)

    search_end = around_time + timedelta(hours=6)

    result = svc.events().list(
        calendarId="primary",
        timeMin=around_time.isoformat(),
        timeMax=search_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    busy = []
    for ev in result.get("items", []):
        es = ev["start"].get("dateTime", "")
        ee = ev["end"].get("dateTime", "")
        if es and ee:
            try:
                busy.append((datetime.fromisoformat(es), datetime.fromisoformat(ee)))
            except (ValueError, TypeError):
                continue
    busy.sort(key=lambda x: x[0])

    # Scan on 30-min boundaries
    candidate = around_time.replace(second=0, microsecond=0)
    if candidate.minute % 30 != 0:
        candidate = candidate.replace(
            minute=(candidate.minute // 30 + 1) * 30 if candidate.minute < 30 else 0,
        )
        if candidate.minute == 0:
            candidate += timedelta(hours=1)

    slots = []
    while len(slots) < count and candidate < search_end:
        candidate_end = candidate + timedelta(minutes=duration_minutes)
        if all(not (bs < candidate_end and be > candidate) for bs, be in busy):
            slots.append(candidate)
        candidate += timedelta(minutes=30)

    return slots

