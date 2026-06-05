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

    # ── Flask server ──────────────────────────────────────────
    FLASK_PORT: int = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_SECRET: str = os.getenv("FLASK_SECRET", "change_me_in_production")

    # ── Groq AI ──────────────────────────────────────────────
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
    ]

    # ── Database ─────────────────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///notebot.db")
    SQLITE_BUSY_TIMEOUT_MS: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
    WEBHOOK_DEDUPE_TTL_HOURS: int = int(os.getenv("WEBHOOK_DEDUPE_TTL_HOURS", "24"))

    # ── Bot ──────────────────────────────────────────────────
    BOT_TIMEZONE: str = os.getenv("BOT_TIMEZONE", "Asia/Kolkata")

    # ── Public URL (ngrok / production) ──────────────────────
    BASE_URL: str = os.getenv("BASE_URL", "")  # e.g. https://xxx.ngrok-free.dev

    # Placeholder strings that look set but aren't real credentials.
    _PLACEHOLDERS = {
        "your_groq_api_key", "your_long_lived_access_token",
        "your_phone_number_id", "generate_a_random_32_byte_token",
        "generate_a_random_secret", "your_meta_app_secret",
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
        if cls._is_placeholder(cls.GROQ_API_KEY):
            errors.append(
                "GROQ_API_KEY is missing or still set to placeholder — "
                "get a real key from https://console.groq.com/keys"
            )
        return errors


config = Config()
