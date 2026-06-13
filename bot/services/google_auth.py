"""FollowUp Bot — Google OAuth2 connect / disconnect.

Active flow (no own domain needed — `_get_redirect_uri()` is localhost):
1. User taps 'Connect Google' button
2. Bot sends a clickable Google auth URL (redirect_uri = http://localhost)
3. User authorises on their phone browser; Google redirects to
   http://localhost/?code=... (the page won't load — that's expected)
4. User copies that URL from the address bar and pastes it back into chat;
   `handle_auth_code()` extracts the code and exchanges it
5. Bot sends a WhatsApp confirmation — done!

The `/auth/callback` Flask route + `handle_oauth_callback()` implement an
optional domain-based auto-exchange. They are INACTIVE while the redirect
URI is localhost; to enable them on a hosted domain later, point
`_get_redirect_uri()` at `{BASE_URL}/auth/callback`.
"""

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select, delete as sql_delete

from bot.database import async_session, User, OAuthState
from bot.config import config
from bot.services.whatsapp import send_message, send_buttons
from bot.utils.time import utcnow_naive

logger = logging.getLogger(__name__)
_TOKEN_PREFIX = "enc:v1:"

# OAuth handshake state is now DB-backed (OAuthState table) so that an
# in-flight "Connect Google" survives a process restart. State rows
# older than this are considered abandoned and swept.
_OAUTH_STATE_TTL = timedelta(minutes=30)


# ─── Durable OAuth-state helpers (replace old in-memory dicts) ─

async def _save_oauth_state(state: str, wa_id: str) -> None:
    """Persist a state→wa_id mapping; clear this user's stale rows first."""
    cutoff = utcnow_naive() - _OAUTH_STATE_TTL
    async with async_session() as session:
        await session.execute(
            sql_delete(OAuthState).where(
                (OAuthState.wa_id == wa_id) | (OAuthState.created_at < cutoff)
            )
        )
        session.add(OAuthState(state=state, wa_id=wa_id))
        await session.commit()


async def _pop_oauth_state(state: str) -> Optional[str]:
    """Return the wa_id for a state token and delete the row (one-shot)."""
    async with async_session() as session:
        row = (await session.execute(
            select(OAuthState).where(OAuthState.state == state)
        )).scalar_one_or_none()
        if not row:
            return None
        wa_id = row.wa_id
        await session.delete(row)
        await session.commit()
        return wa_id


async def _has_pending_oauth(wa_id: str) -> bool:
    """True if this user has an unconsumed OAuth handshake in flight."""
    async with async_session() as session:
        row = (await session.execute(
            select(OAuthState.id).where(OAuthState.wa_id == wa_id).limit(1)
        )).scalar_one_or_none()
        return row is not None


async def _clear_oauth_states(wa_id: str) -> None:
    async with async_session() as session:
        await session.execute(
            sql_delete(OAuthState).where(OAuthState.wa_id == wa_id)
        )
        await session.commit()


# ─── Helpers ─────────────────────────────────────────────────

def _get_redirect_uri() -> str:
    """Return the OAuth redirect URI. Uses localhost for installed app credentials."""
    return "http://localhost"


def _build_flow() -> Optional[Flow]:
    """Build a fresh OAuth Flow from the credentials file.

    Built fresh on every connect AND every callback so we never depend on
    a Flow object surviving in process memory — that's what makes the
    handshake restart-safe.
    """
    cred_path = _get_installed_credentials_path()
    if not cred_path:
        return None
    return Flow.from_client_secrets_file(
        str(cred_path),
        scopes=config.GOOGLE_SCOPES,
        redirect_uri=_get_redirect_uri(),
    )


def _extract_google_email(creds) -> Optional[str]:
    """Best-effort: read the authenticated user's email (PRD §10).

    Non-fatal — if the granted scopes don't permit it, returns None and
    leaves users.google_email blank. Never raises into the OAuth flow.
    """
    try:
        from googleapiclient.discovery import build
        service = build("people", "v1", credentials=creds, cache_discovery=False)
        profile = service.people().get(
            resourceName="people/me", personFields="emailAddresses"
        ).execute()
        emails = profile.get("emailAddresses") or []
        return emails[0].get("value") if emails else None
    except Exception as e:
        logger.debug(f"Could not read google_email (scope?): {e}")
        return None


def _get_installed_credentials_path() -> Optional[Path]:
    """Return a path to 'installed'-type credentials.

    Auto-converts 'web' → 'installed' format if needed.
    """
    cred_path = Path(config.GOOGLE_CREDENTIALS_FILE)
    if not cred_path.exists():
        return None

    with open(cred_path) as f:
        data = json.load(f)

    if "installed" in data:
        return cred_path

    if "web" in data:
        web = data["web"]
        installed = {
            "installed": {
                "client_id": web["client_id"],
                "client_secret": web["client_secret"],
                "auth_uri": web.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
                "token_uri": web.get("token_uri", "https://oauth2.googleapis.com/token"),
                "redirect_uris": [_get_redirect_uri()],
            }
        }
        converted = cred_path.parent / "_credentials_desktop.json"
        with open(converted, "w") as f:
            json.dump(installed, f, indent=2)
        return converted

    return None


def _get_token_cipher():
    """Return a Fernet cipher when GOOGLE_TOKEN_ENCRYPTION_KEY is a valid key.

    Returns None (plaintext mode) when:
      - the key is not set in .env, OR
      - the key is a placeholder / invalid format

    A warning is logged in plaintext mode so you know to fix it, but the
    OAuth flow is never crashed over a missing/bad key — the user would
    otherwise be permanently locked out of connecting Google.
    """
    key = (config.GOOGLE_TOKEN_ENCRYPTION_KEY or "").strip()
    if not key:
        logger.warning(
            "GOOGLE_TOKEN_ENCRYPTION_KEY is not set — Google tokens stored in "
            "plaintext. Run `python setup/generate_secrets.py` to generate one."
        )
        return None
    try:
        from cryptography.fernet import Fernet
        cipher = Fernet(key.encode())
        # Force-validate the key by doing a tiny round-trip (Fernet.__init__
        # doesn't fully validate until the first operation on some versions).
        cipher.decrypt(cipher.encrypt(b"test"))
        return cipher
    except ImportError as exc:
        raise RuntimeError(
            "GOOGLE_TOKEN_ENCRYPTION_KEY is set but cryptography is not installed. "
            "Run: pip install cryptography"
        ) from exc
    except Exception:
        logger.warning(
            "GOOGLE_TOKEN_ENCRYPTION_KEY in .env is not a valid Fernet key "
            "(looks like a placeholder). Storing Google tokens in plaintext. "
            "Fix: run `python setup/generate_secrets.py` and paste the generated "
            "GOOGLE_TOKEN_ENCRYPTION_KEY value into your .env file."
        )
        return None


def _encrypt_token_json(token_json: str) -> str:
    cipher = _get_token_cipher()
    if not cipher or token_json.startswith(_TOKEN_PREFIX):
        return token_json
    return _TOKEN_PREFIX + cipher.encrypt(token_json.encode("utf-8")).decode("utf-8")


def _decrypt_token_json(stored_token: str) -> str:
    if not stored_token:
        return ""
    if not stored_token.startswith(_TOKEN_PREFIX):
        return stored_token
    cipher = _get_token_cipher()
    if not cipher:
        raise RuntimeError("Encrypted Google token found but GOOGLE_TOKEN_ENCRYPTION_KEY is not configured.")
    ciphertext = stored_token[len(_TOKEN_PREFIX):]
    return cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def _missing_google_scopes(stored_token: str) -> list[str]:
    """Return required Google scopes that are absent from a stored token."""
    if not stored_token:
        return list(config.GOOGLE_SCOPES)
    try:
        token_data = json.loads(_decrypt_token_json(stored_token))
        raw_scope = token_data.get("scope") or token_data.get("scopes") or ""
        if isinstance(raw_scope, str):
            granted = set(raw_scope.split())
        else:
            granted = set(raw_scope)
        return [scope for scope in config.GOOGLE_SCOPES if scope not in granted]
    except Exception as e:
        logger.debug(f"Could not inspect Google token scopes: {e}")
        return []


# ─── Connection status check ────────────────────────────────

async def is_google_connected(wa_id: str) -> bool:
    """Check if a user has a valid Google token."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        db_user = result.scalar_one_or_none()
        return bool(db_user and db_user.google_token_json)


# ─── connect ─────────────────────────────────────────────────

async def handle_connect(wa_id: str, display_name: str = ""):
    """Handle 'connect' command — send Google auth link via WhatsApp."""
    flow = _build_flow()
    if not flow:
        send_message(
            wa_id,
            "google credentials file not found. "
            "place credentials.json in the bot directory.",
        )
        return

    # Already connected? Offer status unless new scopes were added.
    missing_scopes = []
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        db_user = result.scalar_one_or_none()
        if db_user and db_user.google_token_json:
            missing_scopes = _missing_google_scopes(db_user.google_token_json)
        if db_user and db_user.google_token_json and not missing_scopes:
            send_buttons(wa_id, "your google account is already connected.", [
                {"id": "disconnect_google", "title": "Disconnect"},
                {"id": "show_capabilities", "title": "What can I do?"},
            ])
            return

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    # Persist the handshake durably (survives restart).
    await _save_oauth_state(state, wa_id)

    if missing_scopes:
        lead = "*tap to update google permissions:*"
    else:
        lead = "*tap to connect google:*"

    send_message(
        wa_id,
        f"{lead}\n\n"
        f"{auth_url}\n\n"
        f"_after allowing access, copy the url from your browser and paste it here._",
    )


# ─── Server-side callback (called by Flask route) ────────────

async def handle_oauth_callback(code: str, state: str) -> Optional[str]:
    """Exchange auth code from callback URL. Returns wa_id on success, None on failure."""
    wa_id = await _pop_oauth_state(state)
    if not wa_id:
        logger.warning(f"OAuth callback with unknown/expired state: {state[:20]}...")
        return None

    flow = _build_flow()
    if not flow:
        logger.warning("OAuth callback but credentials file is missing")
        return None

    try:
        flow.fetch_token(code=code)
        token_json = flow.credentials.to_json()
        google_email = _extract_google_email(flow.credentials)

        await _persist_token(wa_id, token_json, google_email)
        await _clear_oauth_states(wa_id)

        send_message(
            wa_id,
            "*google connected.* calendar, contacts, gmail and meet are ready.",
        )
        logger.info(f"Google connected via callback for wa_id={wa_id}")
        await _notify_onboarding_google_connected(wa_id)
        return wa_id

    except Exception as e:
        logger.error(f"OAuth callback exchange error: {e}", exc_info=True)
        send_message(wa_id, f"connection failed: {e}\n\ntry connecting again.")
        return None


async def _persist_token(wa_id: str, token_json: str, google_email: Optional[str]) -> None:
    """Store the (encrypted) Google token + email on the user row."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            db_user = User(wa_id=wa_id)
            session.add(db_user)
        db_user.google_token_json = _encrypt_token_json(token_json)
        if google_email:
            db_user.google_email = google_email
        await session.commit()


async def _notify_onboarding_google_connected(wa_id: str) -> None:
    """Let the onboarding state machine finalise once Google is linked."""
    try:
        from bot.services.onboarding import on_google_connected
        await on_google_connected(wa_id)
    except Exception as e:
        logger.debug(f"onboarding google-connected hook skipped: {e}")


# ─── Auth-code interceptor (localhost fallback) ──────────────

async def handle_auth_code(wa_id: str, text: str) -> bool:
    """Intercept pasted OAuth redirect URLs (localhost fallback).

    Returns True if the message was handled (auth code), False otherwise.
    Rebuilds the Flow fresh from credentials, so this works even after a
    process restart that cleared in-memory state.
    """
    if not await _has_pending_oauth(wa_id):
        return False
    if not ("code=" in text and ("localhost" in text or "127.0.0.1" in text)):
        return False

    flow = _build_flow()
    if not flow:
        send_message(wa_id, "google credentials file not found.")
        return True

    try:
        parsed = urlparse(text.strip())
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            send_message(wa_id, "couldn't find the code in that url. try connecting again.")
            return True

        flow.fetch_token(code=code)
        token_json = flow.credentials.to_json()
        google_email = _extract_google_email(flow.credentials)

        await _persist_token(wa_id, token_json, google_email)
        await _clear_oauth_states(wa_id)

        send_message(
            wa_id,
            "*google connected.* calendar, contacts, gmail and meet are ready.",
        )
        logger.info(f"Google connected via paste for wa_id={wa_id}")
        await _notify_onboarding_google_connected(wa_id)

    except Exception as e:
        logger.error(f"OAuth exchange error: {e}", exc_info=True)
        send_message(wa_id, f"connection failed: {e}\n\ntry connecting again.")

    return True


# ─── disconnect ──────────────────────────────────────────────

async def handle_disconnect(wa_id: str):
    """Handle 'disconnect' — unlink Google account."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.wa_id == wa_id))
        db_user = result.scalar_one_or_none()
        if not db_user or not db_user.google_token_json:
            send_message(wa_id, "no google account is linked.")
            return
        db_user.google_token_json = None
        db_user.google_email = None
        await session.commit()

    await _clear_oauth_states(wa_id)
    send_message(wa_id, "google account disconnected.")


# ─── Credential loader ───────────────────────────────────────

def get_google_creds(user_db) -> Optional[Credentials]:
    """Load and auto-refresh Google credentials for a user."""
    if not user_db or not user_db.google_token_json:
        return None
    try:
        token_data = json.loads(_decrypt_token_json(user_db.google_token_json))
        creds = Credentials.from_authorized_user_info(token_data)
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            _save_refreshed_token(user_db.wa_id, creds.to_json())
        return creds
    except Exception as e:
        logger.error(f"Credential error: {e}")
        return None


def _save_refreshed_token(wa_id: str, token_json: str):
    """Save refreshed Google token back to the database (sync)."""
    try:
        import asyncio
        from bot.database import async_session as _async_session

        async def _save():
            async with _async_session() as session:
                result = await session.execute(select(User).where(User.wa_id == wa_id))
                db_user = result.scalar_one_or_none()
                if db_user:
                    db_user.google_token_json = _encrypt_token_json(token_json)
                    await session.commit()
                    logger.debug(f"Refreshed token saved for wa_id={wa_id}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_save())
        except RuntimeError:
            asyncio.run(_save())
    except Exception as e:
        logger.warning(f"Could not save refreshed token: {e}")
