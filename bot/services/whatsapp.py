"""FollowUp Bot — WhatsApp Business Cloud API client.

Wraps the Meta Graph API for:
  - Sending text messages
  - Sending interactive button messages (max 3 buttons, 20-char titles)
  - Sending interactive list messages (for multi-option menus)
  - Downloading media files (voice notes)
  - Marking messages as read
"""

import asyncio
import logging
import re
import requests

from bot.config import config

logger = logging.getLogger(__name__)

# Persistent HTTP session — reuses the TLS connection to graph.facebook.com
# instead of a fresh ~100-300ms handshake on EVERY send. requests.Session is
# thread-safe for this use (all callers hit the same host).
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=8,
))

# ── API base ──────────────────────────────────────────────────
def _base_url() -> str:
    return (
        f"https://graph.facebook.com/{config.WA_API_VERSION}"
        f"/{config.WA_PHONE_NUMBER_ID}"
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════════
# Send helpers
# ═══════════════════════════════════════════════════════════════

def send_message(wa_id: str, text: str) -> bool:
    """Send a plain text WhatsApp message.

    Synchronous, because APScheduler jobs and the reminder scheduler call it
    from plain threads. Async callers should use `send_message_async`.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]},
    }
    return _post(payload)


async def send_message_async(wa_id: str, text: str) -> bool:
    """`send_message` for coroutines — the HTTP round trip runs in a thread.

    The Graph call takes 150-400 ms. Made directly from a coroutine that
    stalled the one event loop serving every user for its whole duration.

    Deliberately dispatches through the module-level `send_message` rather than
    calling `_post` directly, so anything that patches `send_message` (the test
    harness does) still intercepts this path.
    """
    return await asyncio.to_thread(send_message, wa_id, text)


def send_buttons(wa_id: str, body_text: str, buttons: list) -> bool:
    """Send an interactive button message.

    buttons: list of dicts with keys 'id' (str) and 'title' (str, max 20 chars).
    Maximum 3 buttons per message.
    """
    btn_list = [
        {
            "type": "reply",
            "reply": {
                "id": btn["id"][:256],
                "title": btn["title"][:20],
            },
        }
        for btn in buttons[:3]
    ]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {"buttons": btn_list},
        },
    }
    return _post(payload)


async def send_buttons_async(wa_id: str, body_text: str, buttons: list) -> bool:
    """`send_buttons` for coroutines — see `send_message_async`."""
    return await asyncio.to_thread(send_buttons, wa_id, body_text, buttons)


def send_list(wa_id: str, body_text: str, button_label: str, sections: list) -> bool:
    """Send an interactive list message.

    sections: list of dicts:
      {"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}]}
    Row title max 24 chars, description max 72 chars.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text[:1024]},
            "action": {
                "button": button_label[:20],
                "sections": sections,
            },
        },
    }
    return _post(payload)


async def send_list_async(wa_id: str, body_text: str, button_label: str,
                          sections: list) -> bool:
    """`send_list` for coroutines — see `send_message_async`."""
    return await asyncio.to_thread(send_list, wa_id, body_text, button_label, sections)


def send_flow(wa_id: str, body_text: str, flow_id: str, screen: str,
              data: dict, cta: str = "Open", flow_token: str = "",
              flow_action: str = "navigate") -> bool:
    """Send an interactive WhatsApp Flow message (published Flow).

    flow_action="navigate" (static Flow): `data` prefills the first screen,
    so it must contain a value for EVERY key the screen declares.
    flow_action="data_exchange" (endpoint Flow): the endpoint's INIT supplies
    the first screen + data, so no payload is sent here.
    """
    import uuid
    params = {
        "flow_message_version": "3",
        "flow_id": flow_id,
        "flow_token": flow_token or f"{wa_id}:{uuid.uuid4().hex[:12]}",
        "flow_cta": cta[:20],
        "flow_action": flow_action,
    }
    # Draft Flows can be sent to WABA testers before publishing.
    if config.WA_FLOW_DRAFT_MODE:
        params["mode"] = "draft"
    if flow_action == "navigate":
        params["flow_action_payload"] = {"screen": screen, "data": data}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body_text[:1024]},
            "action": {"name": "flow", "parameters": params},
        },
    }
    return _post(payload)


def mark_read(wa_id: str, message_id: str) -> None:
    """Mark an incoming message as read (blue ticks) + show 'typing…'.

    The typing indicator is the cheapest latency win there is: the user sees
    the bot typing within ~200ms while the LLM call (~1-3s) runs. It clears
    automatically when our reply arrives (or after 25s).
    """
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    try:
        _session.post(
            f"{_base_url()}/messages",
            json=payload,
            headers=_headers(),
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"mark_read failed: {e}")


# ═══════════════════════════════════════════════════════════════
# Template Messages (for business-initiated / third-party notifications)
# ═══════════════════════════════════════════════════════════════

def send_template(wa_id: str, template_name: str, language_code: str = "en_US",
                  components: list = None) -> bool:
    """Send a pre-approved template message.

    Required for messaging users who haven't messaged the bot first
    (i.e. third-party notifications). In dev mode, the recipient must
    be in the test phone number list on Meta Developer Portal.

    Args:
        wa_id: Recipient phone number (with country code, no +).
        template_name: The approved template name from WA Business Manager.
        language_code: Template language code (default: en_US).
        components: Optional list of template components for dynamic values.
    """
    template = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "template",
        "template": template,
    }
    return _post(payload)


def send_reminder_notification(wa_id: str, message: str) -> bool:
    """Deliver a reminder via the approved utility template.

    Used as a fallback when a free-form reminder is rejected because the user
    is outside the 24-hour customer-service window. The template has a single
    body variable {{1}} = the reminder text.

    Returns False (no-op) if WA_REMINDER_TEMPLATE_NAME isn't configured.
    """
    template_name = config.WA_REMINDER_TEMPLATE_NAME
    if not template_name:
        return False
    clean_phone = re.sub(r"\D", "", wa_id or "")
    if not clean_phone:
        return False
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": (message or "").strip() or "You have a reminder."}],
    }]
    ok = send_template(
        clean_phone, template_name,
        language_code=config.WA_REMINDER_TEMPLATE_LANGUAGE,
        components=components,
    )
    if ok:
        logger.info(f"Reminder delivered to {clean_phone} via template={template_name!r}")
    else:
        logger.warning(f"Reminder template send failed for {clean_phone} (template={template_name!r})")
    return ok


def send_meeting_notification(attendee_phone: str, booker_name: str,
                              meeting_title: str, meeting_time: str,
                              meet_link: str = "") -> bool:
    """Notify a third-party attendee about a Google Meet booking.

    Uses the approved WhatsApp utility template configured by
    WA_MEETING_TEMPLATE_NAME / WA_MEETING_TEMPLATE_LANGUAGE.

    Args:
        attendee_phone: Attendee's phone (with country code, no +/spaces).
        booker_name: Name of the person who booked the meeting.
        meeting_title: Title of the meeting.
        meeting_time: Formatted time string (e.g. "Thu May 15, 3:00 PM IST").
        meet_link: Google Meet link (optional).

    Returns:
        True if sent, False on failure.
    """
    clean_phone = re.sub(r"\D", "", attendee_phone or "")
    if not clean_phone:
        logger.warning("send_meeting_notification: empty phone number")
        return False

    # Approved template body parameters must match this exact order:
    #   {{user_name}}, {{meeting_topic}}, {{meeting_date}}, {{meeting_link}}
    add_calendar_payload = f"meeting_add_calendar|{meet_link}" if meet_link else "meeting_add_calendar"
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": booker_name or "Someone"},
                {"type": "text", "text": meeting_title or "Meeting"},
                {"type": "text", "text": meeting_time or "TBD"},
                {"type": "text", "text": meet_link or "No link"},
            ],
        },
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "0",
            "parameters": [{"type": "payload", "payload": add_calendar_payload}],
        },
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "1",
            "parameters": [{"type": "payload", "payload": "meeting_decline"}],
        },
    ]

    template_name = config.WA_MEETING_TEMPLATE_NAME
    language_code = config.WA_MEETING_TEMPLATE_LANGUAGE
    success = send_template(clean_phone, template_name, language_code=language_code,
                            components=components)
    if success:
        logger.info(
            f"Meeting notification sent to {clean_phone} via "
            f"template={template_name!r} lang={language_code!r}"
        )
        return True

    logger.warning(
        f"Meeting template notification failed for {clean_phone}. "
        f"template={template_name!r}, lang={language_code!r}. "
        "Confirm the approved template name/language and, in Meta dev mode, "
        "add this phone number as a WhatsApp API test recipient."
    )
    return False


# ═══════════════════════════════════════════════════════════════
# Media
# ═══════════════════════════════════════════════════════════════

def download_media(media_id: str) -> bytes:
    """Download a WhatsApp media file and return raw bytes, or empty bytes on error."""
    try:
        # Step 1: Get the temporary download URL
        url_resp = _session.get(
            f"https://graph.facebook.com/{config.WA_API_VERSION}/{media_id}",
            headers={"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"},
            timeout=15,
        )
        if not url_resp.ok:
            logger.error(f"Media URL fetch failed: {url_resp.status_code} {url_resp.text}")
            return b""
        media_url = url_resp.json().get("url", "")
        if not media_url:
            return b""

        # Step 2: Download the actual file
        file_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"},
            timeout=30,
        )
        if not file_resp.ok:
            logger.error(f"Media download failed: {file_resp.status_code}")
            return b""
        return file_resp.content
    except Exception as e:
        logger.error(f"download_media error: {e}")
        return b""


# ═══════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════

def _post(payload: dict) -> bool:
    """POST a message payload to the WhatsApp messages endpoint."""
    try:
        resp = _session.post(
            f"{_base_url()}/messages",
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        if not resp.ok:
            logger.error(f"WA API error {resp.status_code}: {resp.text[:300]}")
            return False
        try:
            data = resp.json()
            msg_id = (data.get("messages") or [{}])[0].get("id", "")
            if msg_id:
                logger.info(f"WA API sent message id={msg_id}")
            else:
                logger.info(f"WA API success: {resp.text[:300]}")
        except Exception:
            logger.info(f"WA API success: {resp.text[:300]}")
        return True
    except Exception as e:
        logger.error(f"WA send error: {e}")
        return False
