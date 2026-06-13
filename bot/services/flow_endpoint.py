"""WhatsApp Flow data-exchange endpoint (encrypted, data_api_version 3.0).

Implements Meta's request/response encryption and the business logic for the
dynamic meeting Flow:

    ping          -> {"data": {"status": "active"}}
    INIT          -> SCHEDULE screen prefilled from the saved gmeet context,
                     with the chosen day's live availability (04:00–12:00)
    data_exchange:
        date_selected -> refreshed availability for the picked date
        review        -> SUMMARY screen with a formatted recap
    (final submit uses the client-side `complete` action, which arrives as an
     nfm_reply on /webhook → handle_gmeet_flow_completion → booking)

Crypto: AES key is RSA-OAEP(SHA-256) encrypted; flow data is AES-128-GCM with
the tag appended; the response reuses the AES key with a bit-inverted IV.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bot.config import config
from bot.utils.time import now_local

logger = logging.getLogger(__name__)

_SLOT_MIN, _SLOT_MAX = 4, 12          # 04:00 .. 12:00 inclusive
_SLOT_STEP_MIN = 30
_private_key_cache = None


# ─── Crypto ──────────────────────────────────────────────────

def _load_private_key():
    global _private_key_cache
    if _private_key_cache is not None:
        return _private_key_cache
    with open(config.FLOW_PRIVATE_KEY_PATH, "rb") as f:
        pem = f.read()
    passphrase = config.FLOW_KEY_PASSPHRASE.encode() if config.FLOW_KEY_PASSPHRASE else None
    _private_key_cache = serialization.load_pem_private_key(pem, password=passphrase)
    return _private_key_cache


def decrypt_request(body: dict):
    """Return (decrypted_dict, aes_key, iv). Raises on tamper/bad key."""
    enc_aes = base64.b64decode(body["encrypted_aes_key"])
    iv = base64.b64decode(body["initial_vector"])
    enc_flow = base64.b64decode(body["encrypted_flow_data"])

    aes_key = _load_private_key().decrypt(
        enc_aes,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    # AES-GCM expects ciphertext||tag, which is exactly enc_flow.
    plaintext = AESGCM(aes_key).decrypt(iv, enc_flow, None)
    return json.loads(plaintext.decode("utf-8")), aes_key, iv


def encrypt_response(response: dict, aes_key: bytes, iv: bytes) -> str:
    """Encrypt the response with the inverted IV; return base64 text."""
    flipped = bytes(b ^ 0xFF for b in iv)
    ct = AESGCM(aes_key).encrypt(flipped, json.dumps(response).encode("utf-8"), None)
    return base64.b64encode(ct).decode("utf-8")


# ─── Helpers ─────────────────────────────────────────────────

def _epoch_ms_utc_midnight(d) -> str:
    return str(int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000))


def _hhmm_label(h: int, m: int) -> str:
    ampm = "AM" if h < 12 else "PM"
    disp_h = h if 1 <= h <= 12 else (12 if h % 12 == 0 else h % 12)
    return f"{disp_h}:{m:02d} {ampm}"


def _grid_slots():
    """All candidate slots between 04:00 and 12:00 as (h, m)."""
    out = []
    h, m = _SLOT_MIN, 0
    while (h, m) <= (_SLOT_MAX, 0):
        out.append((h, m))
        m += _SLOT_STEP_MIN
        if m >= 60:
            m -= 60
            h += 1
    return out


async def _slots_for(wa_id: str, day_local) -> list:
    """Build the 04:00–12:00 dropdown with taken slots disabled."""
    busy = []
    try:
        from bot.database import async_session, User
        from sqlalchemy import select
        from bot.services.calendar import day_busy
        async with async_session() as session:
            u = (await session.execute(select(User).where(User.wa_id == wa_id))).scalar_one_or_none()
        if u and u.google_token_json:
            busy = await day_busy(u, day_local)
    except Exception as e:
        logger.warning(f"flow slots: availability lookup failed ({e}); all slots open")

    now = now_local()
    is_today = day_local.date() == now.date()
    out = []
    for h, m in _grid_slots():
        slot = day_local.replace(hour=h, minute=m, second=0, microsecond=0)
        slot_end = slot + timedelta(minutes=_SLOT_STEP_MIN)
        enabled = True
        if is_today and slot <= now:
            enabled = False
        for bs, be in busy:
            bs_n = bs.replace(tzinfo=None) if bs.tzinfo else bs
            be_n = be.replace(tzinfo=None) if be.tzinfo else be
            s_n = slot.replace(tzinfo=None)
            e_n = slot_end.replace(tzinfo=None)
            if bs_n < e_n and be_n > s_n:
                enabled = False
                break
        out.append({"id": f"{h:02d}:{m:02d}", "title": _hhmm_label(h, m), "enabled": enabled})
    return out


def _first_enabled(slots: list) -> str:
    for s in slots:
        if s.get("enabled", True):
            return s["id"]
    return ""


async def _load_prefill(wa_id: str) -> dict:
    """Read the gmeet context saved when the Flow was sent."""
    try:
        from bot.services.task_service import get_conversation_state
        _, ctx = await get_conversation_state(wa_id)
        return (ctx or {}).get("gmeet_data", {}) or {}
    except Exception:
        return {}


def _day_from_iso(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


# ─── Screen builders ─────────────────────────────────────────

async def _init_screen(wa_id: str) -> dict:
    pre = await _load_prefill(wa_id)
    today = now_local()
    known = _day_from_iso(pre.get("start_time_iso", ""))
    day = known if (known and known.date() >= today.date()) else today
    slots = await _slots_for(wa_id, day)
    time_init = ""
    if known and (_SLOT_MIN <= known.hour <= _SLOT_MAX) and known.minute in (0, 30):
        cand = f"{known.hour:02d}:{known.minute:02d}"
        if any(s["id"] == cand and s.get("enabled", True) for s in slots):
            time_init = cand
    if not time_init:
        time_init = _first_enabled(slots)
    return {"screen": "SCHEDULE", "data": {
        "topic_init": pre.get("title") or "",
        "name_init": pre.get("attendee_name") or "",
        "email_init": pre.get("attendee_email") or "",
        "min_date": _epoch_ms_utc_midnight(today.date()),
        "date_init": _epoch_ms_utc_midnight(day.date()),
        "time": slots,
        "time_init": time_init,
        "is_time_enabled": True,
    }}


async def _date_selected(wa_id: str, data: dict) -> dict:
    raw = str(data.get("date") or "").strip()
    day = now_local()
    try:
        if raw.isdigit():
            d = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date()
            day = now_local().replace(year=d.year, month=d.month, day=d.day)
    except (ValueError, OverflowError):
        pass
    slots = await _slots_for(wa_id, day)
    return {"screen": "SCHEDULE", "data": {
        "time": slots,
        "time_init": _first_enabled(slots),
        "is_time_enabled": True,
    }}


def _review(data: dict) -> dict:
    topic = (data.get("topic") or "Meeting").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    raw_date = str(data.get("date") or "").strip()
    raw_time = str(data.get("time") or "").strip()
    when = ""
    try:
        if raw_date.isdigit():
            d = datetime.fromtimestamp(int(raw_date) / 1000, tz=timezone.utc).date()
            h, m = [int(x) for x in raw_time.split(":")]
            when = datetime(d.year, d.month, d.day, h, m).strftime("%a %b %d, %I:%M %p")
    except (ValueError, TypeError):
        when = f"{raw_date} {raw_time}".strip()
    summary = f"{topic}" + (f" with {name}" if name else "")
    summary += f"\n{when}" if when else ""
    summary += f"\n{email}" if email else ""
    return {"screen": "SUMMARY", "data": {
        "summary": summary,
        "topic": topic, "name": name, "email": email,
        "date": raw_date, "time": raw_time,
    }}


async def build_screen_response(decrypted: dict) -> dict:
    action = decrypted.get("action")
    if action == "ping":
        return {"data": {"status": "active"}}

    token = decrypted.get("flow_token", "") or ""
    wa_id = token.split(":", 1)[0] if ":" in token else token
    data = decrypted.get("data") or {}

    try:
        if action == "INIT":
            return await _init_screen(wa_id)
        if action == "data_exchange":
            trigger = data.get("trigger")
            if trigger == "date_selected":
                return await _date_selected(wa_id, data)
            if trigger == "review":
                return _review(data)
    except Exception as e:
        logger.error(f"flow endpoint business logic error: {e}", exc_info=True)
        # Surface a friendly error on the current screen.
        return {"screen": "SCHEDULE", "data": {
            "error_message": "Something went wrong loading times — try again.",
            "is_time_enabled": True,
        }}

    # Error-notification / unknown action acknowledgement.
    return {"data": {"acknowledged": True}}
