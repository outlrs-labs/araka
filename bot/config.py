"""FollowUp Bot — Configuration (loaded from .env)."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Config:
    """Central configuration — all values sourced from environment."""

    # ── WhatsApp Business Cloud API ──────────────────────────
    WA_PHONE_NUMBER_ID: str = os.getenv("WA_PHONE_NUMBER_ID", "")
    WA_ACCESS_TOKEN: str = os.getenv("WA_ACCESS_TOKEN", "")
    WA_VERIFY_TOKEN: str = os.getenv("WA_VERIFY_TOKEN", "followup_verify_token")
    WA_API_VERSION: str = os.getenv("WA_API_VERSION", "v19.0")
    WA_APP_SECRET: str = os.getenv("WA_APP_SECRET", "")
    REQUIRE_WA_SIGNATURE: bool = os.getenv("REQUIRE_WA_SIGNATURE", "false").lower() in (
        "1", "true", "yes", "on",
    )
    WA_MEETING_TEMPLATE_NAME: str = os.getenv("WA_MEETING_TEMPLATE_NAME", "gmeet_confirmation")
    WA_MEETING_TEMPLATE_LANGUAGE: str = os.getenv("WA_MEETING_TEMPLATE_LANGUAGE", "en_US")
    # Approved utility template used to deliver a reminder when the user is
    # OUTSIDE the 24-hour customer-service window (free-form would be rejected).
    # One body variable {{1}} = the reminder text. Empty = no fallback (a
    # reminder for a user who's been quiet >24h will silently fail to deliver).
    WA_REMINDER_TEMPLATE_NAME: str = os.getenv("WA_REMINDER_TEMPLATE_NAME", "")
    WA_REMINDER_TEMPLATE_LANGUAGE: str = os.getenv("WA_REMINDER_TEMPLATE_LANGUAGE", "en_US")

    # Published WhatsApp Flow used to collect meeting details when the
    # attendee email is missing. Empty = legacy text prompts.
    WA_GMEET_FLOW_ID: str = os.getenv("WA_GMEET_FLOW_ID", "")
    # Dynamic (endpoint-backed) Flow: live availability + server INIT prefill.
    # Requires FLOW_PRIVATE_KEY_PATH + the public key uploaded to Meta.
    WA_GMEET_FLOW_DYNAMIC: bool = os.getenv("WA_GMEET_FLOW_DYNAMIC", "false").lower() in (
        "1", "true", "yes", "on",
    )
    # RSA private key (PEM) for decrypting Flow data-exchange requests.
    FLOW_PRIVATE_KEY_PATH: str = os.getenv("FLOW_PRIVATE_KEY_PATH", "flow_private.pem")
    FLOW_KEY_PASSPHRASE: str = os.getenv("FLOW_KEY_PASSPHRASE", "")
    # Send the Flow in DRAFT mode — works for WABA testers before the Flow is
    # published. Set false once the Flow is published (needs business verification).
    WA_FLOW_DRAFT_MODE: bool = os.getenv("WA_FLOW_DRAFT_MODE", "false").lower() in (
        "1", "true", "yes", "on",
    )

    # Published WhatsApp Flow that collects To / Subject / Body so the user can
    # send an email straight from chat. The content is typed by the user inside
    # the form and goes Flow -> Gmail; the AI never sees or edits it. Empty =
    # the email feature is disabled (the bot will NOT collect a body in chat).
    WA_EMAIL_FLOW_ID: str = os.getenv("WA_EMAIL_FLOW_ID", "")

    # ── Flask server ──────────────────────────────────────────
    FLASK_PORT: int = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_SECRET: str = os.getenv("FLASK_SECRET", "change_me_in_production")

    # ── Sarvam AI (chat / agent brain — OpenAI-compatible) ───
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")
    SARVAM_MODEL: str = os.getenv("SARVAM_MODEL", "sarvam-105b")
    SARVAM_BASE_URL: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai/v1")

    # ── Groq (voice transcription only — Whisper) ────────────
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    # ── Google OAuth ─────────────────────────────────────────
    GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    GOOGLE_TOKEN_ENCRYPTION_KEY: str = os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY", "")
    GOOGLE_SCOPES: list = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/contacts.readonly",
        # Gmail-derived contacts ("Other contacts") — people the user has
        # emailed but never saved. Without this, `otherContacts` returns
        # ACCESS_TOKEN_SCOPE_INSUFFICIENT and "meet with Priyanshu" cannot
        # resolve an address unless that person is a SAVED contact, which
        # forces the user to type the email by hand every time.
        "https://www.googleapis.com/auth/contacts.other.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        # Send-only — lets the user send mail from the WhatsApp Flow form.
        # The body never passes through the AI; gmail.send cannot read mail.
        "https://www.googleapis.com/auth/gmail.send",
    ]

    # Requested ONLY when MEET_TRANSCRIPTS_ENABLED is true — see
    # google_scopes() below. Asking for a scope whose API is not enabled on
    # the Cloud project fails the ENTIRE consent screen with invalid_scope,
    # which would break Google sign-in for every user, not just transcripts.
    # Tying it to the same flag makes that mis-ordering impossible.
    GOOGLE_SCOPE_MEET_TRANSCRIPTS: str = (
        "https://www.googleapis.com/auth/meetings.space.created"
    )

    def google_scopes(self) -> list:
        """The scopes to actually request, given what is switched on.

        An instance method, not a classmethod: `config` is an INSTANCE, so a
        classmethod here would read the class attribute and silently ignore
        any per-instance override — including the ones tests and runtime
        toggles rely on.
        """
        scopes = list(self.GOOGLE_SCOPES)
        if self.MEET_TRANSCRIPTS_ENABLED:
            scopes.append(self.GOOGLE_SCOPE_MEET_TRANSCRIPTS)
        return scopes

    # Master switch for the post-meeting transcript → summary pipeline.
    # Off by default: the feature is complete but inert until BOTH the Meet
    # API is enabled AND an eligible Workspace/One tier exists. Turning this
    # on without the scope above just logs a skip — it cannot half-run.
    MEET_TRANSCRIPTS_ENABLED: bool = (
        os.getenv("MEET_TRANSCRIPTS_ENABLED", "false").lower() == "true"
    )
    # ── Zoom ──────────────────────────────────────────────────
    # Server-to-Server OAuth, which is ACCOUNT-level: unlike Google there is
    # no per-user consent screen, so these live here rather than going through
    # google_auth.py. Transcripts need Zoom Pro or above with cloud recording
    # enabled — the credentials alone are not sufficient.
    ZOOM_ACCOUNT_ID: str = os.getenv("ZOOM_ACCOUNT_ID", "")
    ZOOM_CLIENT_ID: str = os.getenv("ZOOM_CLIENT_ID", "")
    ZOOM_CLIENT_SECRET: str = os.getenv("ZOOM_CLIENT_SECRET", "")

    def zoom_configured(self) -> bool:
        # Instance method for the same reason as google_scopes().
        return bool(self.ZOOM_ACCOUNT_ID and self.ZOOM_CLIENT_ID
                    and self.ZOOM_CLIENT_SECRET)

    # How long after a meeting ends to keep looking for its transcript.
    # Google publishes them "shortly after" the call, but not instantly.
    MEET_TRANSCRIPT_LOOKBACK_HOURS: int = int(
        os.getenv("MEET_TRANSCRIPT_LOOKBACK_HOURS", "12")
    )

    # ── Database ─────────────────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///notebot.db")
    SQLITE_BUSY_TIMEOUT_MS: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
    WEBHOOK_DEDUPE_TTL_HOURS: int = int(os.getenv("WEBHOOK_DEDUPE_TTL_HOURS", "24"))

    # ── Bot ──────────────────────────────────────────────────
    BOT_TIMEZONE: str = os.getenv("BOT_TIMEZONE", "Asia/Kolkata")
    # Operator phone (wa_id, country code, no +). If set, the reminder
    # heartbeat pings this number when reminders are stuck undelivered.
    ADMIN_WA_ID: str = os.getenv("ADMIN_WA_ID", "")

    # ── Public URL (ngrok / production) ──────────────────────
    BASE_URL: str = os.getenv("BASE_URL", "")  # e.g. https://xxx.ngrok-free.dev

    # Placeholder strings that look set but aren't real credentials.
    _PLACEHOLDERS = {
        "your_groq_api_key", "your_long_lived_access_token",
        "your_phone_number_id", "generate_a_random_32_byte_token",
        "generate_a_random_secret", "your_meta_app_secret",
        "your_sarvam_api_key",
    }

    @classmethod
    def _is_placeholder(cls, value: str) -> bool:
        return not value or value.strip().lower() in cls._PLACEHOLDERS

    @classmethod
    def validate(cls) -> list:
        """Return a list of missing/placeholder-config error strings (empty = OK)."""
        errors = []
        if cls._is_placeholder(cls.WA_PHONE_NUMBER_ID):
            errors.append("WA_PHONE_NUMBER_ID is missing or still set to placeholder")
        if cls._is_placeholder(cls.WA_ACCESS_TOKEN):
            errors.append("WA_ACCESS_TOKEN is missing or still set to placeholder")
        if not cls.WA_VERIFY_TOKEN:
            errors.append("WA_VERIFY_TOKEN is missing")
        if cls.REQUIRE_WA_SIGNATURE and not cls.WA_APP_SECRET:
            errors.append("WA_APP_SECRET is required when REQUIRE_WA_SIGNATURE=true")
        if cls._is_placeholder(cls.SARVAM_API_KEY):
            errors.append(
                "SARVAM_API_KEY is missing or still set to placeholder — "
                "get a key from https://dashboard.sarvam.ai"
            )
        # GROQ is used ONLY for voice transcription now — optional. Voice notes
        # degrade gracefully when it's absent, so it's not a hard requirement.
        return errors


config = Config()
