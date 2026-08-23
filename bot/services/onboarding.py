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
from bot.services.whatsapp import (
    send_message, send_message_async, send_buttons, send_buttons_async,
)
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
# Full Terms & Conditions URL (set once you publish Document/TERMS_AND_CONDITIONS.md).
# Empty = show only the short in-chat summary.
TERMS_URL = ""


def _privacy_line() -> str:
    return f"we only store what you ask us to schedule. privacy: {PRIVACY_URL}"


def _consent_buttons() -> list:
    return [
        {"id": "show_terms",     "title": "Terms"},
        {"id": "show_privacy",   "title": "Privacy"},
        {"id": "connect_google", "title": "Connect Google"},
    ]


def send_consent_screen(wa_id: str) -> None:
    """Transparent consent step: explain access, link Terms + Privacy, then connect."""
    send_buttons(
        wa_id,
        "one last thing, so we're fully upfront:\n\n"
        "connecting google lets me use your *calendar, meet and contacts*, "
        "read gmail only when you ask about an email, and *send an email when "
        "you fill in the email form yourself* (i never read or write what you "
        "type). i touch only what you ask me to, never sell your data, and you "
        "can say *delete my data* anytime.\n\n"
        "by tapping *Connect Google* you agree to araka's terms & privacy "
        "policy — tap to read them first.",
        _consent_buttons(),
    )


async def send_terms(wa_id: str) -> None:
    """Short Terms summary + full link, then re-offer the consent buttons."""
    body = (
        "*terms & conditions (short version)*\n\n"
        "- araka is a scheduling assistant — not a general chatbot or "
        "professional advice.\n"
        "- a meeting is created only after you tap *Confirm*. review the "
        "details yourself; we're not liable for mistaken or missed meetings.\n"
        "- only add other people's contact details if you have their "
        "permission.\n"
        "- use araka lawfully — no spam, harassment, or unlawful content.\n"
        "- provided as-is, free for now."
    )
    if TERMS_URL:
        body += f"\n\nfull terms: {TERMS_URL}"
    await send_message_async(wa_id, body)
    await send_buttons_async(wa_id, "all good? you can read the privacy notice too, or connect.",
                 _consent_buttons())


async def send_privacy(wa_id: str) -> None:
    """Short Privacy summary + full link, then re-offer the consent buttons."""
    await send_message_async(
        wa_id,
        "*privacy (short version)*\n\n"
        "- i store only what you ask me to schedule (meetings, reminders, "
        "notes) plus your name and timezone.\n"
        "- google data (calendar, contacts, gmail) is used only to help you, "
        "*never sold and never used to train ai*.\n"
        "- contact names/emails you look up are kept in an encrypted cache "
        "for speed (7 days); say *refresh contacts* to re-sync or *delete my "
        "data* to wipe it.\n"
        "- i can *send* an email only from the form you fill in yourself — the "
        "subject and message go straight to gmail, i never read or change them, "
        "and i don't store a copy.\n"
        "- i message people you book with via an approved template; they can "
        "reply *STOP*.\n"
        "- say *delete my data* anytime to erase everything; *disconnect* to "
        "unlink google."
        f"\n\nfull notice: {PRIVACY_URL}",
    )
    await send_buttons_async(wa_id, "all good? you can read the terms too, or connect.",
                 _consent_buttons())


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
    await send_message_async(
        wa_id,
        "look who went official\n\n"
        "meta makes me say this before we start, so here: i'm an automated "
        "AI agent. now that the boring part is over: i'm araka, and i live "
        "right here in your texts now, way faster, and i can drop buttons "
        "and lists whenever we need them\n\n"
        "this is where all your reminders and notifications are gonna land "
        "from now on. say \"unsubscribe\" anytime if it gets too much\n\n"
        "what's good?\n\n" + _privacy_line(),
    )
    await send_buttons_async(
        wa_id,
        f"quick check — is *{_pretty_number(wa_id)}* the right number for you?",
        [
            {"id": "onboard_wa_yes",   "title": "Yes, that's me"},
            {"id": "onboard_wa_other", "title": "Different number"},
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
    await send_message_async(
        wa_id,
        "noted. what should i call you? first name is fine.",
    )


async def reject_whatsapp(wa_id: str) -> None:
    """v1 always uses the WhatsApp number the user is messaging from."""
    await send_buttons_async(
        wa_id,
        "for now i work on the number you're texting me from — let's use "
        "this one. sound good?",
        [
            {"id": "onboard_wa_yes",   "title": "Use this number"},
            {"id": "onboard_wa_other", "title": "Ask me later"},
        ],
    )


async def confirm_timezone(wa_id: str) -> None:
    await _set_timezone_and_finish(wa_id, "Asia/Kolkata")


async def change_timezone_prompt(wa_id: str) -> None:
    await send_message_async(
        wa_id,
        "no problem — type your timezone in IANA form, e.g. "
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
        await send_message_async(
            wa_id,
            "*privacy*\n\n"
            "- i store only the meetings and reminders you ask me to create\n"
            "- i don't keep a transcript of our chat\n"
            "- your google token is stored encrypted and used only for "
            "your calendar\n"
            "- reply *delete my data* anytime to wipe everything"
            f"\n\nfull notice: {PRIVACY_URL}",
        )
        return True

    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        step = u.onboarding_step if u else None

    if step == STEP_WA:
        await send_message_async(wa_id, "tap one of the buttons above to continue")
        return True

    if step == STEP_NAME:
        name = text[:_MAX_NAME].strip()
        if len(name) < 1:
            await send_message_async(wa_id, "didn't catch a name — what should i call you?")
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
        await send_buttons_async(
            wa_id,
            f"nice to meet you, {name}.\n\n"
            "last step — i'll use *India Standard Time (IST)* for your "
            "reminders. is that right?",
            [
                {"id": "onboard_tz_yes",    "title": "Yes, use IST"},
                {"id": "onboard_tz_change", "title": "Change"},
            ],
        )
        return True

    if step == STEP_TZ:
        tz_name = _coerce_timezone(text)
        if not tz_name:
            await send_message_async(
                wa_id,
                "i don't recognise that timezone. try an IANA name like "
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
    await send_message_async(wa_id, f"all set. timezone: *{nice_tz}*.")
    # Transparent consent step: Terms / Privacy / Connect Google.
    send_consent_screen(wa_id)


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
