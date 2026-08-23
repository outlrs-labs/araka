"""Gmail helpers — read (for email questions) and send (from the Flow form)."""

import base64
import html
import logging
import re
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot.services.google_auth import get_google_creds

from bot.utils.aio import offloaded

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")


class GmailScopeError(Exception):
    """Raised when the stored Google token lacks Gmail permissions."""


def _get_service(user_db):
    """Build a Gmail v1 service for the given user."""
    creds = get_google_creds(user_db)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body_data(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode((data + padding).encode("utf-8")).decode(
            "utf-8", errors="replace",
        )
    except Exception:
        return ""


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    value = re.sub(r"(?s)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<.*?>", " ", value)
    return html.unescape(value)


def _clean_text(value: str) -> str:
    value = _strip_html(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def _extract_body(payload: dict) -> str:
    plain = []
    html_parts = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data", "")
        if not data:
            continue
        text = _decode_body_data(data)
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            html_parts.append(text)
    body = "\n".join(plain).strip() or "\n".join(html_parts).strip()
    return _clean_text(body)


def _format_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.strftime("%a %b %d, %I:%M %p")
    except Exception:
        return value


def _message_summary(message: dict, include_body: bool = False) -> dict:
    payload = message.get("payload") or {}
    out = {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject") or "(no subject)",
        "date": _format_date(_header(payload, "Date")),
        "snippet": _clean_text(message.get("snippet", "")),
    }
    if include_body:
        out["body"] = _extract_body(payload)
    return out


def _is_scope_error(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False
    content = getattr(exc, "content", b"") or b""
    text = content.decode("utf-8", errors="ignore").lower()
    return exc.resp.status in (401, 403) and (
        "insufficient" in text or "scope" in text or "permission" in text
    )


@offloaded
def search_messages(
    user_db,
    query: str = "",
    max_results: int = 5,
    include_body: bool = False,
) -> list[dict]:
    """Search Gmail and return compact message summaries."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    # Ceiling raised from 10: a week-long summary legitimately needs more than
    # ten messages, and the caller already sizes its request by payload type.
    max_results = max(1, min(int(max_results or 5), 40))
    try:
        result = svc.users().messages().list(
            userId="me",
            q=(query or "").strip(),
            maxResults=max_results,
        ).execute()
        refs = result.get("messages") or []
        messages = []
        for ref in refs:
            fmt = "full" if include_body else "metadata"
            params = {"userId": "me", "id": ref["id"], "format": fmt}
            if fmt == "metadata":
                params["metadataHeaders"] = ["From", "To", "Subject", "Date"]
            req = svc.users().messages().get(**params)
            messages.append(_message_summary(req.execute(), include_body=include_body))
        return messages
    except Exception as e:
        if _is_scope_error(e):
            raise GmailScopeError("Missing Gmail readonly permission") from e
        logger.error(f"Gmail search failed: {e}", exc_info=True)
        raise


def _parse_address(header: str) -> tuple[str, str]:
    """Split a From/To header into (display name, address).

    Handles the three shapes Gmail emits:
        "Muskaan Jain (Unstop)" <muskaan@unstop.com>
        Muskaan Jain <muskaan@unstop.com>
        muskaan@unstop.com
    """
    raw = (header or "").strip()
    if not raw:
        return "", ""
    match = re.search(r"<([^<>]+)>", raw)
    if match:
        addr = match.group(1).strip()
        name = raw[:match.start()].strip().strip('"').strip()
    else:
        addr = raw
        name = ""
    if not is_valid_email(addr):
        return "", ""
    if not name:
        # "muskaan.jain@x.com" -> "Muskaan Jain", so the picker shows a
        # person rather than a raw address.
        local = addr.split("@", 1)[0]
        parts = [p for p in re.split(r"[._+-]+", local) if p and not p.isdigit()]
        name = " ".join(p.capitalize() for p in parts)
    return name, addr


@offloaded
def find_people_in_mail(user_db, name: str, max_results: int = 12) -> list[dict]:
    """Find someone by name in Gmail message headers.

    The People API only knows SAVED contacts and "Other contacts" — and Google
    populates Other contacts from people the user has EMAILED, not from people
    who merely emailed them. So a sender the user never replied to is invisible
    to both, even though their address is sitting in the inbox. That is why
    "what's Muskaan Jain's email?" failed seconds after araka had summarised a
    message from her.

    Searches From AND To so it works for both inbound senders and outbound
    recipients, and returns the same {name, emails, phones} shape the contact
    cache uses, so callers can treat it as just another contact source.
    """
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    query = (name or "").strip()
    if not query:
        return []

    try:
        result = svc.users().messages().list(
            userId="me",
            q=f'from:("{query}") OR to:("{query}")',
            maxResults=max(1, min(int(max_results or 12), 25)),
        ).execute()
    except Exception as e:
        if _is_scope_error(e):
            raise GmailScopeError("Missing Gmail readonly permission") from e
        logger.warning(f"Gmail people lookup failed for {query!r}: {e}")
        return []

    refs = result.get("messages") or []
    needle = query.lower()
    by_addr: dict[str, dict] = {}

    for ref in refs:
        try:
            msg = svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "To"],
            ).execute()
        except Exception:
            continue
        payload = msg.get("payload") or {}
        for header in ("From", "To"):
            # A To: header can carry several recipients.
            for chunk in (_header(payload, header) or "").split(","):
                person, addr = _parse_address(chunk)
                if not addr:
                    continue
                # Gmail matches the query loosely across the whole message, so
                # confirm the name really belongs to THIS address before
                # offering it as a resolution.
                haystack = f"{person} {addr}".lower()
                if not all(tok in haystack for tok in needle.split()):
                    continue
                existing = by_addr.get(addr.lower())
                if existing is None or (not existing["name"] and person):
                    by_addr[addr.lower()] = {
                        "name": person or addr, "emails": [addr], "phones": [],
                    }
    return list(by_addr.values())


def is_valid_email(addr: str) -> bool:
    """Light server-side check; the Flow already validates input-type=email."""
    return bool(_EMAIL_RE.match((addr or "").strip()))


@offloaded
def send_email(user_db, to: str, subject: str, body: str) -> dict:
    """Send a plain-text email AS the connected user (gmail.send scope).

    Privacy contract: `subject` and `body` are passed through verbatim from
    the WhatsApp Flow form. No AI/LLM touches them. The message is sent from
    the user's own Gmail address (userId="me" → Gmail fills the From header).

    Returns {"ok": True, "id", "to"} on success.
    Raises GmailScopeError if the token lacks gmail.send; ValueError on bad
    input; Exception on transport/API failure.
    """
    to = (to or "").strip()
    # Strip CR/LF from the single-line headers to prevent header injection and
    # to avoid a confusing serialize error if the user pastes a multi-line
    # value into Subject. (Body keeps its newlines — they're content.)
    subject = re.sub(r"[\r\n]+", " ", (subject or "").strip())
    body = body or ""

    if not is_valid_email(to):
        raise ValueError("invalid recipient email")
    if not body.strip():
        raise ValueError("empty body")

    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    mime = MIMEText(body, "plain", "utf-8")
    mime["to"] = to
    mime["subject"] = subject or "(no subject)"
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    try:
        sent = svc.users().messages().send(
            userId="me", body={"raw": raw},
        ).execute()
        return {"ok": True, "id": sent.get("id", ""), "to": to}
    except Exception as e:
        if _is_scope_error(e):
            raise GmailScopeError("Missing Gmail send permission") from e
        logger.error(f"Gmail send failed: {e}", exc_info=True)
        raise
