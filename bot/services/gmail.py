"""Gmail read helpers for answering email questions."""

import base64
import html
import logging
import re
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot.services.google_auth import get_google_creds

logger = logging.getLogger(__name__)


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


async def search_messages(
    user_db,
    query: str = "",
    max_results: int = 5,
    include_body: bool = False,
) -> list[dict]:
    """Search Gmail and return compact message summaries."""
    svc = _get_service(user_db)
    if not svc:
        raise Exception("Google not connected")

    max_results = max(1, min(int(max_results or 5), 10))
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
