"""FollowUp Bot — Main Flask Webhook Server.

Handles incoming WhatsApp Cloud API webhooks.
Routes text, voice, and interactive messages.
Runs APScheduler background jobs.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import timedelta
from html import escape as html_escape

from flask import Flask, request, jsonify, redirect, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import config
from bot.database import (
    init_db, async_session, User, Task, Reminder, WebhookMessage, ChatMemory,
)
from bot.utils.time import utcnow_naive
from bot.agent import process_message, get_last_agent_action_result
from bot.services.onboarding import (
    ensure_user, start_onboarding, handle_onboarding_message,
)
from bot.services.google_auth import (
    handle_connect, handle_auth_code, handle_disconnect,
    handle_oauth_callback, is_google_connected,
)
from bot.services.whatsapp import (
    send_message, send_message_async, send_buttons, mark_read,
)
from bot.services.transcribe import transcribe_voice
from bot.handlers.callback_handler import (
    handle_interactive_reply,
    send_connect_button, send_gmeet_contact_picker,
    send_gmeet_email_request, send_gmeet_time_request,
    send_gmeet_conflict_buttons, send_gmeet_confirm_buttons,
    handle_gmeet_text_reply,
    send_gmeet_flow, handle_gmeet_flow_completion,
    handle_flow_completion,
    send_calendar_reschedule_confirm_buttons,
    send_add_attendee_confirm_buttons, send_add_attendee_contact_picker,
    send_add_attendee_event_picker, send_add_attendee_email_request,
)
from bot.services.task_service import (
    clear_conversation_state, get_conversation_state,
)
from bot.services import reminder_scheduler
from sqlalchemy import delete as sql_delete, select, update as sql_update
from sqlalchemy.exc import IntegrityError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Fail-open guard: without signature enforcement, ANYONE who finds the
# webhook URL can forge messages as any user. Loud warning, every boot.
if not (config.WA_APP_SECRET and config.REQUIRE_WA_SIGNATURE):
    logger.warning(
        "SECURITY: webhook signature verification is NOT enforced "
        "(set WA_APP_SECRET and REQUIRE_WA_SIGNATURE=true in .env). "
        "Forged webhooks will be accepted."
    )

# Static HTML lives in <project>/web
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _render_page(filename: str, status: int = 200, **subs):
    """Read an HTML file from web/ and do simple {{KEY}} substitution."""
    path = os.path.join(WEB_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return ("<h1>FollowUp Bot</h1><p>Service running.</p>", 200,
                {"Content-Type": "text/html; charset=utf-8"})
    for k, v in subs.items():
        # Escape — substituted values (e.g. OAuth ?error=) are untrusted text,
        # not HTML. Prevents reflected XSS on the error page.
        html = html.replace("{{" + k + "}}", html_escape(str(v)))
    return (html, status, {"Content-Type": "text/html; charset=utf-8"})


# Global asyncio loop for running async code from sync Flask routes
_loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=start_background_loop, args=(_loop,), daemon=True)
_loop_thread.start()

def run_async(coro):
    """Run an async coroutine thread-safely in the background loop."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


def _verify_webhook_signature(raw_body: bytes) -> bool:
    """Verify Meta's X-Hub-Signature-256 when configured."""
    if not config.WA_APP_SECRET:
        return not config.REQUIRE_WA_SIGNATURE

    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        config.WA_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _record_message_once(msg_id: str, wa_id: str) -> bool:
    """Persist message idempotency and return False for duplicates."""
    cutoff = utcnow_naive() - timedelta(hours=config.WEBHOOK_DEDUPE_TTL_HOURS)
    async with async_session() as session:
        try:
            session.add(WebhookMessage(message_id=msg_id, wa_id=wa_id))
            await session.execute(
                sql_delete(WebhookMessage).where(WebhookMessage.received_at < cutoff)
            )
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False

# ═══════════════════════════════════════════════════════════════
# Flask Webhook Routes
# ═══════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home_page():
    return _render_page("index.html")


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "FollowUp Bot WhatsApp Server"}), 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta Webhook Verification."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == config.WA_VERIFY_TOKEN:
            logger.info("Webhook verified successfully.")
            return challenge, 200
        else:
            return "Forbidden", 403
    return "OK", 200

@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    """Google OAuth2 callback — exchanges code for tokens automatically."""
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        logger.warning(f"OAuth callback error: {error}")
        return _render_page("oauth_error.html", status=400, ERROR=error)

    if not code or not state:
        return _render_page("oauth_error.html", status=400,
                            ERROR="Missing parameters — please reconnect from WhatsApp.")

    try:
        wa_id = run_async(handle_oauth_callback(code, state))
        if wa_id:
            return _render_page("oauth_success.html")
        return _render_page("oauth_error.html", status=400,
                            ERROR="The authorization link may have expired.")
    except Exception as e:
        logger.error(f"Auth callback error: {e}", exc_info=True)
        return _render_page("oauth_error.html", status=500, ERROR=str(e))

@app.route("/flow", methods=["POST"])
def flow_data_exchange():
    """Encrypted WhatsApp Flow data-exchange endpoint (dynamic meeting form)."""
    from bot.services import flow_endpoint
    body = request.get_json(silent=True) or {}
    if not all(k in body for k in ("encrypted_flow_data", "encrypted_aes_key", "initial_vector")):
        return "Bad Request", 400
    try:
        decrypted, aes_key, iv = flow_endpoint.decrypt_request(body)
    except Exception as e:
        logger.warning(f"Flow endpoint decrypt failed: {e}")
        return "", 421   # Meta retries / surfaces a refresh-key error
    try:
        resp = run_async(flow_endpoint.build_screen_response(decrypted))
        encrypted = flow_endpoint.encrypt_response(resp, aes_key, iv)
        return Response(encrypted, mimetype="text/plain")
    except Exception as e:
        logger.error(f"Flow endpoint error: {e}", exc_info=True)
        return "", 500


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Handle incoming WhatsApp messages."""
    raw_body = request.get_data()
    if not _verify_webhook_signature(raw_body):
        logger.warning("Rejected webhook with invalid Meta signature.")
        return "Forbidden", 403

    body = request.get_json(silent=True)
    if not body:
        return "Bad Request", 400

    try:
        # Check if this is a WhatsApp message event
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        # Mark read and process
                        for msg in value["messages"]:
                            wa_id = msg.get("from")
                            msg_id = msg.get("id")
                            if wa_id and msg_id:
                                # Durable dedup: skip already-processed messages
                                if not run_async(_record_message_once(msg_id, wa_id)):
                                    logger.info(f"Skipping duplicate msg {msg_id}")
                                    continue
                                mark_read(wa_id, msg_id)
                                # Fire-and-forget the processing so webhook responds quickly
                                asyncio.run_coroutine_threadsafe(
                                    _process_update(wa_id, msg), _loop
                                )
    except Exception as e:
        logger.error(f"Error handling webhook: {e}", exc_info=True)

    return "OK", 200

# ═══════════════════════════════════════════════════════════════
# Message Router
# ═══════════════════════════════════════════════════════════════

# ── Per-user rate limit (P0 abuse guard) ───────────────────────
# Every message costs an LLM call + Google API quota. A hostile or broken
# client spamming the webhook must not be able to drain either. Sliding
# window, in-memory: fine for gunicorn -w1 + single background loop.
_RATE_LIMIT_MAX = 20          # messages allowed …
_RATE_LIMIT_WINDOW = 60.0     # … per this many seconds
_rate_buckets: dict = {}      # wa_id -> deque[monotonic timestamps]
_rate_notified: dict = {}     # wa_id -> when we last told them to slow down


def _rate_limited(wa_id: str) -> bool:
    """True if this user is over the limit (runs on the single event loop)."""
    now = time.monotonic()
    q = _rate_buckets.setdefault(wa_id, deque())
    while q and now - q[0] > _RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= _RATE_LIMIT_MAX:
        # Tell them once per window, then drop silently.
        if now - _rate_notified.get(wa_id, 0.0) > _RATE_LIMIT_WINDOW:
            _rate_notified[wa_id] = now
            send_message(wa_id, "whoa, that's a lot of messages — give me a minute, then try again.")
        return True
    q.append(now)
    return False


async def _process_update(wa_id: str, msg: dict):
    """Process a single incoming message from WhatsApp."""
    msg_type = msg.get("type")

    if _rate_limited(wa_id):
        logger.warning(f"Rate limit: dropping message from {wa_id}")
        return

    # 1. Get-or-create the user.
    user, is_new = await ensure_user(wa_id)

    # 2. Route by message type
    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()

        # STOP / START are honoured at ANY time — even for a brand-new
        # number that only ever received a template (FR-8 + §12).
        if await _maybe_handle_optout(wa_id, text):
            return
        if await _maybe_handle_optin(wa_id, text):
            return
        if await _maybe_handle_data_deletion(wa_id, text):
            return

        # Brand-new user → kick off onboarding (FR-1).
        if is_new:
            await start_onboarding(wa_id)
            return

        # Onboarding gate — until complete, all text feeds the FR-1 machine.
        if not user.onboarding_complete:
            if await handle_onboarding_message(wa_id, text):
                return
            # Fell through (e.g. step got cleared) → restart onboarding.
            await start_onboarding(wa_id)
            return

        await _handle_text(wa_id, text)

    elif msg_type == "audio":
        if is_new:
            await start_onboarding(wa_id)
            return
        if not user.onboarding_complete:
            await send_message_async(wa_id, "let's finish the quick setup above first — "
                                "tap a button or type your name.")
            return
        audio_id = msg.get("audio", {}).get("id")
        if audio_id:
            await _handle_audio(wa_id, audio_id)

    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        int_type = interactive.get("type")
        
        reply_id = None
        reply_title = ""
        
        if int_type == "button_reply":
            reply_id = interactive.get("button_reply", {}).get("id")
            reply_title = interactive.get("button_reply", {}).get("title")
        elif int_type == "list_reply":
            reply_id = interactive.get("list_reply", {}).get("id")
            reply_title = interactive.get("list_reply", {}).get("title")
        elif int_type == "nfm_reply":
            # WhatsApp Flow form submission (meeting form OR email form).
            response_json = interactive.get("nfm_reply", {}).get("response_json", "")
            if response_json:
                await handle_flow_completion(wa_id, response_json)
            return

        if reply_id:
            await handle_interactive_reply(wa_id, reply_id, reply_title)

    elif msg_type == "button":
        # Template quick-reply buttons arrive as type="button" webhooks.
        button = msg.get("button", {})
        reply_id = button.get("payload") or button.get("text", "")
        reply_title = button.get("text", "")
        if reply_id:
            await handle_interactive_reply(wa_id, reply_id, reply_title)

# ═══════════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════════

_OPTOUT_WORDS = {
    "stop", "unsubscribe", "opt out", "optout", "opt-out",
    "stop reminders", "no more reminders",
}


async def _maybe_handle_optout(wa_id: str, text: str) -> bool:
    """Handle STOP / opt-out (PRD §FR-8 + §12). Returns True if handled.

    Sets the user's consent to OPT_OUT and flags every task where they are
    the assignee as `assignee_unreachable=True`, so reminders stop going to
    them. Their own data is untouched (use 'delete my data' for that).
    """
    t = (text or "").strip().lower()
    if t not in _OPTOUT_WORDS:
        return False

    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            u.consent_status = "OPT_OUT"
            # Mark tasks where this user is the assignee as unreachable.
            await session.execute(
                sql_update(Task)
                .where(Task.assignee_id == u.id)
                .values(assignee_unreachable=True)
            )
        # Also catch tasks that only stored the phone string (pre-User era).
        await session.execute(
            sql_update(Task)
            .where(Task.assignee_phone == wa_id)
            .values(assignee_unreachable=True)
        )
        await session.commit()

    await send_message_async(
        wa_id,
        "done — you won't get any more meeting reminders from me. "
        "reply *START* anytime to turn them back on.",
    )
    logger.info(f"User {wa_id} opted out (consent=OPT_OUT)")
    return True


async def _maybe_handle_optin(wa_id: str, text: str) -> bool:
    """Re-enable reminders after a STOP (PRD §FR-8). Returns True if handled."""
    if (text or "").strip().lower() not in ("start", "resume", "opt in", "optin"):
        return False
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if not u or u.consent_status != "OPT_OUT":
            return False
        u.consent_status = "OPT_IN"
        await session.execute(
            sql_update(Task)
            .where(Task.assignee_id == u.id)
            .values(assignee_unreachable=False)
        )
        await session.commit()
    await send_message_async(wa_id, "reminders are back on. welcome back.")
    return True


_DELETE_WORDS = {"delete my data", "delete my account", "forget me", "erase my data"}


async def _maybe_handle_data_deletion(wa_id: str, text: str) -> bool:
    """Right-to-erasure (PRD §12). Wipes the user's stored data. Returns True if handled."""
    if (text or "").strip().lower() not in _DELETE_WORDS:
        return False

    from bot.database import (
        Note, TaskConversationState, OAuthState, ContactCache,
    )
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            await session.execute(sql_delete(Note).where(Note.user_id == u.id))
            await session.execute(sql_delete(ChatMemory).where(ChatMemory.user_id == u.id))
            await session.execute(sql_delete(ContactCache).where(ContactCache.user_id == u.id))
        await session.execute(sql_delete(Task).where(Task.wa_id == wa_id))
        await session.execute(sql_delete(Reminder).where(Reminder.wa_id == wa_id))
        await session.execute(sql_delete(TaskConversationState).where(TaskConversationState.wa_id == wa_id))
        await session.execute(sql_delete(OAuthState).where(OAuthState.wa_id == wa_id))
        if u:
            await session.delete(u)
        await session.commit()

    await send_message_async(
        wa_id,
        "done — i've deleted your data (meetings, reminders, notes and "
        "your google link). message me again any time to start fresh.",
    )
    logger.info(f"Data deletion completed for {wa_id}")
    return True


async def _handle_text(wa_id: str, text: str):
    """Handle plain text messages (commands or natural language)."""
    text_lower = text.lower().strip()
    # "/connect" == "connect" — users type slash-commands out of habit; a
    # missed command falls to the LLM, which then hallucinates fake buttons.
    cmd = text_lower.lstrip("/!").strip()

    # Commands
    if cmd == "connect":
        await handle_connect(wa_id)
        return
    if cmd == "disconnect":
        await handle_disconnect(wa_id)
        return
    if cmd == "cancel":
        await clear_conversation_state(wa_id)
        await send_message_async(wa_id, "cancelled.")
        return
    if cmd in ("refresh contacts", "refresh my contacts", "sync contacts"):
        # Manual cache invalidation — wipe + re-warm from Google.
        from bot.services.contacts import clear_contact_cache, bootstrap_contacts
        user, _ = await ensure_user(wa_id)
        await clear_contact_cache(user.id)
        n = 0
        try:
            n = await bootstrap_contacts(wa_id)
        except Exception as e:
            logger.warning(f"Contact re-sync failed for {wa_id}: {e}")
        await send_message_async(
            wa_id,
            f"contacts refreshed — {n} synced from google." if n
            else "contact cache cleared — i'll re-fetch from google as needed.",
        )
        return
        
    # Check for OAuth redirect URL paste
    if await handle_auth_code(wa_id, text):
        return

    # Deterministic Google Meet continuations (contact choice, Gmail, new time)
    flow_state, flow_ctx = await get_conversation_state(wa_id)
    if await handle_gmeet_text_reply(wa_id, text, flow_state, flow_ctx):
        return

    # Tier-0 router — strict-pattern turns ("remind me in 20 min to X",
    # "my reminders") are handled deterministically: no LLM call, no
    # hallucination surface, near-zero latency. Anything fuzzy falls through.
    from bot.services.fast_path import try_fast_path
    fast_reply = await try_fast_path(wa_id, text)
    if fast_reply:
        await send_message_async(wa_id, fast_reply)
        return

    # Send to AI Agent
    try:
        response_text = await process_message(wa_id, text)
        
        # A structured tool result outranks the model's prose. Every
        # state-changing calendar tool routes through here so its own
        # deterministic UI is shown; letting one fall through to free text is
        # what turns a clear tool result into an invented question.
        cached = get_last_agent_action_result(wa_id)
        if cached:
            tool = cached.get("tool")
            data = cached.get("data") or {}
            action = data.get("action")
            msg = data.get("message", response_text)

            if tool == "set_gmeet":
                if action == "awaiting_confirmation":
                    # Confirmation gate: bot shows proposed details with Yes/Edit/Cancel.
                    # The event is NOT created until the user taps Yes.
                    await send_gmeet_confirm_buttons(wa_id, data)
                    return
                elif action == "conflict":
                    await send_gmeet_conflict_buttons(wa_id, data)
                    return
                elif action == "created":
                    await send_message_async(wa_id, msg)
                    return
                elif action == "pick_contact":
                    await send_gmeet_contact_picker(wa_id, data)
                    return
                elif action in ("need_email", "no_contact"):
                    # Attendee email missing → native form, prefilled with
                    # everything already known. Full-info requests never get
                    # here — they keep the classic conflict-check + confirm.
                    if config.WA_GMEET_FLOW_ID:
                        await send_gmeet_flow(wa_id, data)
                    else:
                        await send_gmeet_email_request(wa_id, data)
                    return
                elif action == "missing_time":
                    await send_gmeet_time_request(wa_id, data)
                    return
                elif action == "error":
                    await send_message_async(wa_id, msg)
                    return

            elif tool == "calendar_reschedule":
                # Previously cached and then dropped on the floor, so the
                # confirm buttons never appeared and the tap always reported
                # "expired".
                if action == "awaiting_calendar_reschedule_confirmation":
                    await send_calendar_reschedule_confirm_buttons(wa_id, data)
                    return
                await send_message_async(wa_id, msg)
                return

            elif tool == "calendar_add_attendee":
                if action == "awaiting_add_attendee_confirmation":
                    await send_add_attendee_confirm_buttons(wa_id, data)
                    return
                elif action == "pick_contact_for_add":
                    await send_add_attendee_contact_picker(wa_id, data)
                    return
                elif action == "choose_event_for_add":
                    await send_add_attendee_event_picker(wa_id, data)
                    return
                elif action in ("need_attendee_email", "need_attendee"):
                    await send_add_attendee_email_request(wa_id, data)
                    return
                await send_message_async(wa_id, msg)
                return

            elif tool == "get_meeting_summaries":
                # Pull path for post-meeting summaries. The card renderer is
                # deterministic — the model never narrates the summary.
                from bot.handlers.callback_handler import (
                    send_meeting_summary_card, send_meeting_summary_picker,
                )
                if action == "meeting_summary":
                    await send_meeting_summary_card(wa_id, data)
                    return
                if action == "pick_summary":
                    await send_meeting_summary_picker(wa_id, data)
                    return
                await send_message_async(wa_id, msg)
                return

        # SHOW_CONNECT_BUTTON — safety fallback for Google connection prompt
        if "SHOW_CONNECT_BUTTON" in response_text:
            connected = await is_google_connected(wa_id)
            if connected:
                await send_message_async(
                    wa_id,
                    "your google account is connected. "
                    "try asking again — i'll access your calendar now.",
                )
            else:
                text_clean = response_text.replace("SHOW_CONNECT_BUTTON", "").strip()
                await send_connect_button(wa_id, text_clean or "connect your google account to get started.")
            return
            
        await send_message_async(wa_id, response_text)
        
    except Exception as e:
        logger.error(f"Agent processing error: {e}", exc_info=True)
        await send_message_async(wa_id, "sorry, i ran into an issue processing that.")

async def _handle_audio(wa_id: str, audio_id: str):
    """Download audio, transcribe, and process as text."""
    transcript = await transcribe_voice(audio_id)

    if not transcript:
        await send_message_async(wa_id, "sorry, i couldn't understand that audio.")
        return

    await _handle_text(wa_id, transcript)

# ═══════════════════════════════════════════════════════════════
# Background Jobs
# ═══════════════════════════════════════════════════════════════

def job_prune_chat_memory():
    """Prune chat memory past its 2h TTL — moved OFF the message hot path.

    process_message enforces the TTL on read (WHERE created_at >= cutoff),
    so this job is purely storage hygiene and can run coarsely.
    """
    try:
        async def _prune():
            cutoff = utcnow_naive() - timedelta(hours=2)
            async with async_session() as session:
                await session.execute(
                    sql_delete(ChatMemory).where(ChatMemory.created_at < cutoff)
                )
                await session.commit()
        run_async(_prune())
    except Exception as e:
        logger.error(f"Chat memory prune error: {e}")


def job_meeting_transcripts():
    """Fetch + summarise transcripts for meetings that just ended.

    Polls rather than subscribes: Google publishes a transcript some minutes
    after a call ends, and the Workspace Events API would add a Pub/Sub
    dependency for latency nobody is waiting on. Self-limiting — a meeting
    with a summary row is never looked at again.
    """
    try:
        from bot.services.meetings import pipeline
        run_async(pipeline.run_once())
    except Exception as e:
        logger.error(f"Meeting transcript job error: {e}")


def job_reminders_reconcile():
    """Defense-in-depth sweep for general reminders.

    The precise DateTrigger jobs in reminder_scheduler are the primary
    fire path. This sweep catches:
      - Rows inserted directly into SQLite without scheduling
      - Reminders whose scheduler job was lost during a crashed restart
      - Past-due rows whose DateTrigger misfire_grace_time expired

    It re-uses the same atomic-claim _fire_reminder_async, so it is safe
    to overlap with normal firing — concurrent fires are de-duped by the
    `UPDATE … WHERE is_sent=False` race-resolver.
    """
    try:
        async def _sweep():
            now_utc = utcnow_naive()
            async with async_session() as session:
                result = await session.execute(
                    select(Reminder).where(
                        Reminder.is_sent == False,
                        Reminder.remind_at <= now_utc,
                    )
                )
                due = list(result.scalars().all())
                ids = [r.id for r in due]
            if ids:
                logger.warning(
                    f"Reconciliation found {len(ids)} overdue reminder(s): {ids}"
                )
            for rid in ids:
                await reminder_scheduler._fire_reminder_async(rid)
        run_async(_sweep())
    except Exception as e:
        logger.error(f"Reminder reconciliation error: {e}", exc_info=True)

# ═══════════════════════════════════════════════════════════════
# App Startup
# ═══════════════════════════════════════════════════════════════

def start_jobs():
    scheduler = BackgroundScheduler(timezone="UTC")
    # User-requested reminders use DateTrigger jobs via reminder_scheduler for
    # ±1s precision. This is the defense-in-depth reconciliation only.
    scheduler.add_job(job_reminders_reconcile, IntervalTrigger(minutes=5))
    # Chat-memory TTL cleanup (removed from the per-message hot path).
    scheduler.add_job(job_prune_chat_memory, IntervalTrigger(minutes=30))
    # Post-meeting transcripts. Registered ONLY when enabled, so a dormant
    # feature costs nothing on a 1-vCPU box.
    if config.MEET_TRANSCRIPTS_ENABLED:
        scheduler.add_job(job_meeting_transcripts, IntervalTrigger(minutes=10))
        logger.info("Meeting transcript polling enabled (every 10 min)")
    scheduler.start()
    # Wire the same scheduler instance for one-shot reminder jobs and
    # rehydrate any pending reminders from the DB.
    reminder_scheduler.init(scheduler, _loop)
    reloaded = run_async(reminder_scheduler.reload_pending())
    logger.info(f"Background jobs started; {reloaded} reminder(s) rehydrated.")

if __name__ == "__main__":
    # Validate config
    errors = config.validate()
    if errors:
        logger.error("Configuration errors:")
        for e in errors:
            logger.error(f" - {e}")
        sys.exit(1)

    # Auto-detect ngrok URL if BASE_URL not set
    if not config.BASE_URL:
        try:
            import requests as _req
            resp = _req.get("http://localhost:4040/api/tunnels", timeout=3)
            tunnels = resp.json().get("tunnels", [])
            for t in tunnels:
                if t.get("public_url", "").startswith("https://"):
                    config.BASE_URL = t["public_url"]
                    break
            if config.BASE_URL:
                logger.info(f"Auto-detected ngrok URL: {config.BASE_URL}")
            else:
                logger.warning("ngrok running but no HTTPS tunnel found. OAuth will use localhost fallback.")
        except Exception:
            logger.warning("ngrok not detected. OAuth will use localhost redirect (user must paste URL).")

    # Init DB schema
    run_async(init_db())
    logger.info("Database initialized.")
    
    # Start APScheduler
    start_jobs()
    
    logger.info(f"Starting webhook server on port {config.FLASK_PORT}")
    if config.BASE_URL:
        logger.info(f"OAuth callback: {config.BASE_URL}/auth/callback")
    app.run(host="0.0.0.0", port=config.FLASK_PORT, debug=False)
