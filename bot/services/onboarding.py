"""First-run onboarding state machine (PRD §FR-1 + §A.1).

Canonical flow — at most one question per turn, never re-asked:

    (new user)
        │  start_onboarding()
        ▼
    await_wa_confirm ──tap "Yes"──► await_name
        │                              │  user types name (≤80)
        │                              ▼
        │                          await_tz ──tap "Yes, IST"──► DONE + offer Google
        │                              │  (or types an IANA tz)
        └── tap "Different number" ── (re-confirm; v1 uses the WA number)

`User.onboarding_step` holds the current state; `User.onboarding_complete`
gates the rest of the bot. Google OAuth is *offered* at the end but is
not a hard block — the bonus features (notes, reminders, to-do) work
without it, and `set_gmeet`/calendar tools already prompt to connect when
needed.

Privacy notice (PRD §12) is surfaced on the very first turn.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import pytz
from sqlalchemy import select

from bot.database import async_session, User
from bot.services.whatsapp import send_message, send_buttons
from bot.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

STEP_WA   = "await_wa_confirm"
STEP_NAME = "await_name"
STEP_TZ   = "await_tz"

_MAX_NAME = 80

# Privacy notice lives in a published Google Doc — no domain of our own needed.
PRIVACY_URL = (
    "https://docs.google.com/document/d/e/"
    "2PACX-1vTxvJzZceBjtOtKOFKzBw_yZYKlCO2qYdQpf5kBCVUeVHjY9VOwm8IJ0HwRDcDVYMCgLTIe90vjMWA6/pub"
)


def _privacy_line() -> str:
    return f"🔒 We only store what you ask us to schedule. Privacy: {PRIVACY_URL}"


def _pretty_number(wa_id: str) -> str:
    return f"+{wa_id}" if wa_id and not wa_id.startswith("+") else (wa_id or "")


# ═══════════════════════════════════════════════════════════════
# Status helpers
# ═══════════════════════════════════════════════════════════════

async def is_onboarded(wa_id: str) -> bool:
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        return bool(u and u.onboarding_complete)


async def ensure_user(wa_id: str) -> Tuple[User, bool]:
    """Get-or-create the user. Returns (user, is_new)."""
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            return u, False
        u = User(wa_id=wa_id, onboarding_step=STEP_WA, consent_status="PENDING")
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u, True


# ═══════════════════════════════════════════════════════════════
# Step 0 — greet + confirm WhatsApp number
# ═══════════════════════════════════════════════════════════════

async def start_onboarding(wa_id: str) -> None:
    """Send the welcome + privacy notice + WhatsApp-number confirmation."""
    await _set_step(wa_id, STEP_WA)
    send_message(
        wa_id,
        "👋 Hi! I'm *FollowUp Bot* — I help you schedule meetings and never "
        "drop a commitment.\n\n" + _privacy_line(),
    )
    send_buttons(
        wa_id,
        f"First, is *{_pretty_number(wa_id)}* the best number to reach you on?",
        [
            {"id": "onboard_wa_yes",   "title": "✅ Yes, that's me"},
            {"id": "onboard_wa_other", "title": "📱 Different no."},
        ],
    )


# ═══════════════════════════════════════════════════════════════
# Button handlers (routed from callback_handler)
# ═══════════════════════════════════════════════════════════════

async def confirm_whatsapp(wa_id: str) -> None:
    """User confirmed their WhatsApp number → ask for display name."""
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            u.whatsapp_confirmed = True
            u.phone_e164 = _pretty_number(wa_id)
            # Using the bot is the creator's opt-in (PRD §12).
            u.consent_status = "OPT_IN"
            u.consent_given_at = utcnow_naive()
            u.onboarding_step = STEP_NAME
            await session.commit()
    send_message(
        wa_id,
        "Great! What should I call you? (Just your first name is fine.)",
    )


async def reject_whatsapp(wa_id: str) -> None:
    """v1 always uses the WhatsApp number the user is messaging from."""
    send_buttons(
        wa_id,
        "For v1, I work on the WhatsApp number you're messaging me from — "
        "so let's use this one. Sound good?",
        [
            {"id": "onboard_wa_yes",   "title": "✅ Use this number"},
            {"id": "onboard_wa_other", "title": "❔ Ask me later"},
        ],
    )


async def confirm_timezone(wa_id: str) -> None:
    await _set_timezone_and_finish(wa_id, "Asia/Kolkata")


async def change_timezone_prompt(wa_id: str) -> None:
    send_message(
        wa_id,
        "🌐 No problem — type your timezone in IANA form, e.g. "
        "*America/New_York*, *Europe/London*, or *Asia/Dubai*.",
    )


# ═══════════════════════════════════════════════════════════════
# Text handler — drives await_name / await_tz
# ═══════════════════════════════════════════════════════════════

async def handle_onboarding_message(wa_id: str, text: str) -> bool:
    """Handle a typed message while onboarding is in progress.

    Returns True if the message was consumed by onboarding (the LLM and
    all other flows must NOT see it).
    """
    text = (text or "").strip()

    # Let users read the privacy notice at any onboarding step.
    if text.lower() == "privacy":
        send_message(
            wa_id,
            "🔒 *Privacy*\n\n"
            "• I store only the meetings/reminders you ask me to create.\n"
            "• I don't keep a transcript of our chat.\n"
            "• Your Google token is stored encrypted and used only for "
            "your calendar.\n"
            "• Reply *delete my data* anytime to wipe everything."
            f"\n\nFull notice: {PRIVACY_URL}",
        )
        return True

    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        step = u.onboarding_step if u else None

    if step == STEP_WA:
        send_message(wa_id, "Please tap one of the buttons above to continue 👆")
        return True

    if step == STEP_NAME:
        name = text[:_MAX_NAME].strip()
        if len(name) < 1:
            send_message(wa_id, "I didn't catch a name — what should I call you?")
            return True
        async with async_session() as session:
            u = (await session.execute(
                select(User).where(User.wa_id == wa_id)
            )).scalar_one_or_none()
            if u:
                u.display_name = name
                u.first_name = name.split()[0]
                u.onboarding_step = STEP_TZ
                await session.commit()
        send_buttons(
            wa_id,
            f"Nice to meet you, {name}! 🎉\n\n"
            "Last step — I'll use *India Standard Time (IST)* for your "
            "reminders. Is that right?",
            [
                {"id": "onboard_tz_yes",    "title": "✅ Yes, use IST"},
                {"id": "onboard_tz_change", "title": "🌐 Change"},
            ],
        )
        return True

    if step == STEP_TZ:
        tz_name = _coerce_timezone(text)
        if not tz_name:
            send_message(
                wa_id,
                "Hmm, I don't recognise that timezone. Try an IANA name like "
                "*Asia/Kolkata* or *America/New_York* — or tap *Yes, use IST*.",
            )
            return True
        await _set_timezone_and_finish(wa_id, tz_name)
        return True

    # Not in an onboarding step → not handled here.
    return False


# ═══════════════════════════════════════════════════════════════
# Finish + Google offer
# ═══════════════════════════════════════════════════════════════

async def _set_timezone_and_finish(wa_id: str, tz_name: str) -> None:
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            u.timezone = tz_name
            u.onboarding_step = None
            u.onboarding_complete = True
            if not u.whatsapp_confirmed:
                u.whatsapp_confirmed = True
            await session.commit()

    nice_tz = "IST" if tz_name == "Asia/Kolkata" else tz_name
    send_message(wa_id, f"✅ All set! Timezone: *{nice_tz}*.")
    # Offer Google as the final (optional) step.
    send_buttons(
        wa_id,
        "🔗 Connect your Google account to unlock *Calendar, Contacts & "
        "Meet*. (You can also just start with notes & reminders.)",
        [
            {"id": "connect_google",    "title": "🔗 Connect Google"},
            {"id": "show_capabilities", "title": "ℹ️ What can I do?"},
        ],
    )


async def on_google_connected(wa_id: str) -> None:
    """Hook fired by google_auth after a successful connect.

    Ensures onboarding flags are coherent if a user connected Google very
    early. Safe to call multiple times.
    """
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if not u:
            return
        changed = False
        if not u.onboarding_complete:
            u.onboarding_complete = True
            u.onboarding_step = None
            changed = True
        if u.consent_status == "PENDING":
            u.consent_status = "OPT_IN"
            u.consent_given_at = utcnow_naive()
            changed = True
        if changed:
            await session.commit()


# ═══════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════

async def _set_step(wa_id: str, step: Optional[str]) -> None:
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            u.onboarding_step = step
            await session.commit()


def _coerce_timezone(text: str) -> Optional[str]:
    """Return a valid IANA tz name from user text, or None."""
    candidate = (text or "").strip()
    low = candidate.lower()
    aliases = {
        "ist": "Asia/Kolkata", "india": "Asia/Kolkata",
        "kolkata": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
        "est": "America/New_York", "pst": "America/Los_Angeles",
        "gmt": "Etc/GMT", "utc": "UTC", "uk": "Europe/London",
        "london": "Europe/London", "dubai": "Asia/Dubai",
    }
    if low in aliases:
        return aliases[low]
    try:
        pytz.timezone(candidate)   # raises if invalid
        return candidate
    except Exception:
        return None
