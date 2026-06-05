"""FollowUp Bot — Database models and async session setup.

Models
------
User                    WhatsApp user + Google OAuth token  (wa_id = phone number)
Note                    Quick notes
ChatMemory              Rolling conversation history for the AI agent
Reminder                One-time and recurring reminders
Task                    Commitment with a 6-state lifecycle
TaskConversationState   Per-user inline-flow tracker
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, JSON, ForeignKey, event,
    text as sql_text, inspect as sa_inspect,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

from bot.config import config
from bot.utils.time import utcnow_naive

Base = declarative_base()

_engine_kwargs = {"echo": False, "pool_pre_ping": True}
if config.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": config.SQLITE_BUSY_TIMEOUT_MS / 1000}

engine = create_async_engine(config.DATABASE_URL, **_engine_kwargs)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Single canonical "now" — see bot/utils/time.utcnow_naive for the
# implementation. Kept as a module-level name so existing
# `Column(..., default=_utcnow)` references continue to work.
_utcnow = utcnow_naive


# ═══════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wa_id = Column(String(50), unique=True, nullable=False, index=True)  # WhatsApp phone number
    username = Column(String(100))
    first_name = Column(String(100))
    display_name = Column(String(80))
    timezone = Column(String(50), default="Asia/Kolkata")

    # ── Onboarding (PRD §FR-1) ───────────────────────────────
    onboarding_complete = Column(Boolean, default=False)
    # Tracks which onboarding step we're waiting on:
    #   None | "await_wa_confirm" | "await_name" | "await_tz" | "await_google"
    onboarding_step = Column(String(40), nullable=True)
    whatsapp_confirmed = Column(Boolean, default=False)        # PRD §10
    phone_e164 = Column(String(20), nullable=True)             # validated E.164

    # ── Consent / opt-out (PRD §10 + §12) ────────────────────
    # OPT_IN | OPT_OUT | PENDING
    consent_status = Column(String(10), default="PENDING")
    consent_given_at = Column(DateTime, nullable=True)

    # ── Google ───────────────────────────────────────────────
    google_token_json = Column(Text)
    google_email = Column(String(255), nullable=True)         # PRD §10
    google_sheet_id = Column(String(255))

    is_bot_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(Text, nullable=False)
    detected_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class ChatMemory(Base):
    """Rolling conversation buffer — pruned to 2 h TTL."""
    __tablename__ = "chat_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)   # FK to users.id (PK)
    role = Column(String(20), nullable=False)               # "user" | "model"
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class WebhookMessage(Base):
    """Durable idempotency record for inbound WhatsApp messages."""
    __tablename__ = "webhook_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(255), unique=True, nullable=False, index=True)
    wa_id = Column(String(50), nullable=False, index=True)
    received_at = Column(DateTime, default=_utcnow, index=True)


class Reminder(Base):
    """One-time or recurring daily reminder."""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wa_id = Column(String(50), nullable=False, index=True)  # WhatsApp phone number
    message = Column(Text, nullable=False)
    remind_at = Column(DateTime, nullable=False)
    reminder_type = Column(String(20), default="text")      # text | notes | calendar
    is_recurring = Column(Boolean, default=False)
    recur_time = Column(String(5), nullable=True)           # HH:MM (user tz)
    is_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)


class Task(Base):
    """Commitment with 6-state machine.

    States: draft → scheduled → completed / rescheduled / cancelled / unconfirmed
    """
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wa_id = Column(String(50), nullable=False, index=True)  # WhatsApp phone number

    # Parsed fields
    title = Column(String(255), nullable=False)
    assignee_name = Column(String(100))
    assignee_phone = Column(String(20))
    assignee_id = Column(Integer, nullable=True, index=True)  # FK→users.id (PRD §10)
    scheduled_at = Column(DateTime)                         # UTC
    duration_minutes = Column(Integer, default=30)
    mode = Column(String(20), default="online")             # online | offline
    location_text = Column(String(200))
    meeting_link = Column(Text)
    timezone = Column(String(50), nullable=True)            # IANA tz (PRD §10)

    # Reminder offsets in minutes-before, JSON list. PRD default [1440, 60].
    reminder_offsets_min = Column(JSON, default=lambda: [1440, 60])

    # Per-field NLP confidence (PRD §FR-3) e.g. {"assignee":0.9,"title":0.95}
    confidence_scores = Column(JSON, nullable=True)

    # State machine
    status = Column(String(20), default="draft", index=True)

    # Google Calendar
    external_event_id = Column(String(255))
    conflict_override = Column(Boolean, default=False)
    assignee_unreachable = Column(Boolean, default=False)    # PRD §FR-8 (STOP)

    # Tracking
    rescheduled_to_id = Column(Integer, nullable=True)
    completion_response = Column(String(50))
    completion_check_sent = Column(Boolean, default=False)
    completion_check_sent_at = Column(DateTime, nullable=True)
    reminder_1_sent = Column(DateTime)                      # T-24 h
    reminder_2_sent = Column(DateTime)                      # T-1 h

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class TaskConversationState(Base):
    """Per-user flow tracker for interactive-button interactions."""
    __tablename__ = "task_conversation_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wa_id = Column(String(50), unique=True, nullable=False, index=True)
    flow_state = Column(String(50))
    context_json = Column(Text)
    updated_at = Column(DateTime, default=_utcnow)


class OAuthState(Base):
    """Durable OAuth handshake state (replaces in-memory dicts).

    Process restarts used to lose in-flight Google OAuth handshakes
    because the state→wa_id map lived in a module-level dict. Persisting
    it here means a user mid-auth survives a deploy/restart (PRD §11
    "stateless handler" non-negotiable).
    """
    __tablename__ = "oauth_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    state = Column(String(255), unique=True, nullable=False, index=True)
    wa_id = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)


# ═══════════════════════════════════════════════════════════════
# Bootstrap + lightweight migration
# ═══════════════════════════════════════════════════════════════

# Columns that may be missing on an older SQLite file. Format:
#   table_name: [(column_name, sqlite_ddl_fragment), ...]
# create_all() makes new TABLES but never adds COLUMNS to an existing
# table, so we ALTER-ADD them here. (At 100 users a full Alembic setup
# is overkill; this is the pragmatic equivalent.)
_MIGRATIONS = {
    "users": [
        ("onboarding_step", "VARCHAR(40)"),
        ("whatsapp_confirmed", "BOOLEAN DEFAULT 0"),
        ("phone_e164", "VARCHAR(20)"),
        ("consent_status", "VARCHAR(10) DEFAULT 'PENDING'"),
        ("consent_given_at", "DATETIME"),
        ("google_email", "VARCHAR(255)"),
    ],
    "tasks": [
        ("assignee_id", "INTEGER"),
        ("timezone", "VARCHAR(50)"),
        ("reminder_offsets_min", "JSON DEFAULT '[1440, 60]'"),
        ("confidence_scores", "JSON"),
        ("assignee_unreachable", "BOOLEAN DEFAULT 0"),
    ],
}


def _sync_migrate(conn):
    """Add any missing columns to existing tables (SQLite-safe)."""
    inspector = sa_inspect(conn)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _MIGRATIONS.items():
        if table not in existing_tables:
            continue  # create_all already built it with the full schema
        have = {c["name"] for c in inspector.get_columns(table)}
        for col_name, ddl in columns:
            if col_name not in have:
                conn.execute(sql_text(
                    f"ALTER TABLE {table} ADD COLUMN {col_name} {ddl}"
                ))


async def init_db():
    """Create all tables if they don't exist, then run column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_migrate)
