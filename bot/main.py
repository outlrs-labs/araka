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
from datetime import timedelta
from html import escape as html_escape

from flask import Flask, request, jsonify, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_result

from bot.config import config
from bot.database import init_db, async_session, User, Task, Reminder, WebhookMessage
from bot.utils.time import utcnow_naive
from bot.agent import process_message, get_last_gmeet_result
from bot.services.onboarding import (
    ensure_user, start_onboarding, handle_onboarding_message,
)
from bot.services.google_auth import (
    handle_connect, handle_auth_code, handle_disconnect,
    handle_oauth_callback, is_google_connected,
)
from bot.services.whatsapp import send_message, send_buttons, mark_read
from bot.services.transcribe import transcribe_voice
from bot.handlers.callback_handler import (
    handle_interactive_reply,
    send_completion_buttons,
    send_connect_button, send_gmeet_contact_picker,
    send_gmeet_email_request, send_gmeet_time_request,
    send_gmeet_conflict_buttons, send_gmeet_confirm_buttons,
    handle_gmeet_text_reply,
)
from bot.services.task_service import (
    check_draft_timeouts,
    get_tasks_needing_completion_check, mark_completion_check_sent,
    check_unconfirmed_timeouts,
    get_tasks_needing_reminders, mark_reminder_sent,
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

async def _process_update(wa_id: str, msg: dict):
    """Process a single incoming message from WhatsApp."""
    msg_type = msg.get("type")

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
            send_message(wa_id, "Let's finish the quick setup above first 🙂 "
                                "Tap a button or type your name.")
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

    send_message(
        wa_id,
        "🔕 Done — you won't get any more meeting reminders from me. "
        "Reply *START* anytime to turn them back on.",
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
    send_message(wa_id, "🔔 Reminders are back on. Welcome back!")
    return True


_DELETE_WORDS = {"delete my data", "delete my account", "forget me", "erase my data"}


async def _maybe_handle_data_deletion(wa_id: str, text: str) -> bool:
    """Right-to-erasure (PRD §12). Wipes the user's stored data. Returns True if handled."""
    if (text or "").strip().lower() not in _DELETE_WORDS:
        return False

    from bot.database import (
        Note, ChatMemory, TaskConversationState, OAuthState,
    )
    async with async_session() as session:
        u = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
        if u:
            await session.execute(sql_delete(Note).where(Note.user_id == u.id))
            await session.execute(sql_delete(ChatMemory).where(ChatMemory.user_id == u.id))
        await session.execute(sql_delete(Task).where(Task.wa_id == wa_id))
        await session.execute(sql_delete(Reminder).where(Reminder.wa_id == wa_id))
        await session.execute(sql_delete(TaskConversationState).where(TaskConversationState.wa_id == wa_id))
        await session.execute(sql_delete(OAuthState).where(OAuthState.wa_id == wa_id))
        if u:
            await session.delete(u)
        await session.commit()

    send_message(
        wa_id,
        "🗑️ Done — I've deleted your data (meetings, reminders, notes and "
        "your Google link). Message me again any time to start fresh.",
    )
    logger.info(f"Data deletion completed for {wa_id}")
    return True


async def _handle_text(wa_id: str, text: str):
    """Handle plain text messages (commands or natural language)."""
    text_lower = text.lower()
    
    # Commands
    if text_lower == "connect":
        await handle_connect(wa_id)
        return
    if text_lower == "disconnect":
        await handle_disconnect(wa_id)
        return
    if text_lower == "cancel":
        await clear_conversation_state(wa_id)
        send_message(wa_id, "Current operation cancelled.")
        return
        
    # Check for OAuth redirect URL paste
    if await handle_auth_code(wa_id, text):
        return

    # Deterministic Google Meet continuations (contact choice, Gmail, new time)
    flow_state, flow_ctx = await get_conversation_state(wa_id)
    if await handle_gmeet_text_reply(wa_id, text, flow_state, flow_ctx):
        return

    # Send typing indicator equivalent (WhatsApp doesn't have a direct API for this, 
    # but we could send a "⏳ Thinking..." message if we wanted, though it clutters the chat.
    # We will just process directly.)
    
    # Send to AI Agent
    try:
        response_text = await process_message(wa_id, text)
        
        # Check if the agent triggered a GMeet workflow that needs interactive UI
        gmeet_data = get_last_gmeet_result(wa_id)
        if gmeet_data:
            action = gmeet_data.get("action")
            msg = gmeet_data.get("message", response_text)

            if action == "awaiting_confirmation":
                # Confirmation gate: bot shows proposed details with Yes/Edit/Cancel.
                # The event is NOT created until the user taps Yes.
                await send_gmeet_confirm_buttons(wa_id, gmeet_data)
                return
            elif action == "conflict":
                await send_gmeet_conflict_buttons(wa_id, gmeet_data)
                return
            elif action == "created":
                send_message(wa_id, msg)
                return
            elif action == "pick_contact":
                await send_gmeet_contact_picker(wa_id, gmeet_data)
                return
            elif action in ("need_email", "no_contact"):
                await send_gmeet_email_request(wa_id, gmeet_data)
                return
            elif action == "missing_time":
                await send_gmeet_time_request(wa_id, gmeet_data)
                return
            elif action == "error":
                send_message(wa_id, msg)
                return
        
        # SHOW_CONNECT_BUTTON — safety fallback for Google connection prompt
        if "SHOW_CONNECT_BUTTON" in response_text:
            connected = await is_google_connected(wa_id)
            if connected:
                send_message(
                    wa_id,
                    "✅ Your Google account is connected! "
                    "Try asking again — I'll access your calendar now.",
                )
            else:
                text_clean = response_text.replace("SHOW_CONNECT_BUTTON", "").strip()
                send_connect_button(wa_id, text_clean or "🔗 Connect your Google account to get started.")
            return
            
        send_message(wa_id, response_text)
        
    except Exception as e:
        logger.error(f"Agent processing error: {e}", exc_info=True)
        send_message(wa_id, "Sorry, I ran into an issue processing that.")

async def _handle_audio(wa_id: str, audio_id: str):
    """Download audio, transcribe, and process as text."""
    # Acknowledge receipt
    send_message(wa_id, "🎧 _Transcribing your voice note..._")
    
    transcript = await transcribe_voice(audio_id)
    
    if not transcript:
        send_message(wa_id, "❌ Sorry, I couldn't understand that audio.")
        return
        
    send_message(wa_id, f"📝 _\"{transcript}\"_")
    await _handle_text(wa_id, transcript)

# ═══════════════════════════════════════════════════════════════
# Background Jobs
# ═══════════════════════════════════════════════════════════════

def job_check_drafts():
    try:
        count = run_async(check_draft_timeouts())
        if count > 0:
            logger.info(f"Cleaned up {count} expired drafts.")
    except Exception as e:
        logger.error(f"Draft timeout job error: {e}")

def job_check_unconfirmed():
    try:
        count = run_async(check_unconfirmed_timeouts())
        if count > 0:
            logger.info(f"Marked {count} tasks as unconfirmed.")
    except Exception as e:
        logger.error(f"Unconfirmed timeout job error: {e}")

def job_completion_checks():
    try:
        tasks = run_async(get_tasks_needing_completion_check())
        for t in tasks:
            msg = f"Hey! How did your '{t.title}' go?"
            send_completion_buttons(t.wa_id, t.id, msg)
            run_async(mark_completion_check_sent(t.id))
            logger.info(f"Sent completion check for task #{t.id}")
    except Exception as e:
        logger.error(f"Completion check job error: {e}")

@retry(stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_result(lambda ok: ok is False))
def _send_attempt(wa_id: str, text: str) -> bool:
    return send_message(wa_id, text)


def send_message_retry(wa_id: str, text: str) -> bool:
    """send_message with up to 3 attempts + exponential backoff (PRD §FR-9)."""
    try:
        return _send_attempt(wa_id, text)
    except Exception:
        logger.error(f"All reminder send retries failed for {wa_id}")
        return False


async def _notify_assignee_reminder(t, label: str) -> None:
    """Send the assignee a reminder via the approved template (PRD §FR-9 + §12).

    Free-form messages to an assignee who hasn't messaged us would be
    blocked by Meta's 24h window, so the assignee path is template-only.
    Skips opted-out / unreachable assignees.
    """
    if t.assignee_unreachable:
        return

    e164 = t.assignee_phone
    if t.assignee_id:
        async with async_session() as session:
            u = (await session.execute(
                select(User).where(User.id == t.assignee_id)
            )).scalar_one_or_none()
        if not u or u.consent_status == "OPT_OUT":
            return
        e164 = u.phone_e164 or t.assignee_phone
    if not e164:
        return

    from bot.services.phone import to_wa_id
    wa = to_wa_id(e164)
    if not wa:
        return

    async with async_session() as session:
        creator = (await session.execute(
            select(User).where(User.wa_id == t.wa_id)
        )).scalar_one_or_none()
    booker = (creator.display_name if creator and creator.display_name else "Your contact")

    when = _format_task_local(t)
    from bot.services.whatsapp import send_meeting_notification
    ok = send_meeting_notification(
        attendee_phone=wa, booker_name=booker,
        meeting_title=f"{t.title} ({label})",
        meeting_time=when, meet_link=t.meeting_link or "",
    )
    logger.info(f"Assignee reminder for task #{t.id} → sent={ok}")


def _format_task_local(t) -> str:
    from bot.utils.time import to_local_aware
    try:
        return to_local_aware(t.scheduled_at).strftime("%a %b %d, %-I:%M %p IST")
    except Exception:
        return "soon"


def _dispatch_task_reminder(t, label: str, num: int) -> None:
    # Claim the cycle FIRST so an overlapping/next poll can't re-dispatch and
    # double-notify the assignee. The retry budget for FR-9 is the 3 in-cycle
    # attempts in send_message_retry — NOT unbounded cross-poll re-sends.
    run_async(mark_reminder_sent(t.id, num))

    # Creator — free-form (they're an active user, inside the 24h window).
    msg = f"🔔 Reminder: '{t.title}' {label}."
    if t.mode == "online" and t.meeting_link:
        msg += f"\n🔗 Link: {t.meeting_link}"
    ok = send_message_retry(t.wa_id, msg)
    logger.info(f"Creator reminder #{num} for task #{t.id} → sent={ok}")

    # Assignee — template-only, opt-out aware (bilateral, FR-9). Sent exactly
    # once per cycle regardless of the creator send result.
    try:
        run_async(_notify_assignee_reminder(t, label))
    except Exception as e:
        logger.warning(f"Assignee reminder error for task #{t.id}: {e}")


def job_task_reminders():
    """Task T-24h / T-1h reminders to BOTH parties (PRD §FR-9).

    Uses polling because task reminders are computed dynamically from
    `scheduled_at - 24h / 1h` rather than stored as absolute fire times. A
    5-min poll is fine for a 24h-out reminder. General one-shot reminders
    use DateTrigger via reminder_scheduler (±1s) and are NOT polled here.
    """
    try:
        n24, n1 = run_async(get_tasks_needing_reminders())
        for t in n24:
            _dispatch_task_reminder(t, "is tomorrow", num=1)
        for t in n1:
            _dispatch_task_reminder(t, "starts in 1 hour", num=2)
    except Exception as e:
        logger.error(f"Task reminders job error: {e}", exc_info=True)


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
    scheduler.add_job(job_check_drafts, IntervalTrigger(minutes=30))
    scheduler.add_job(job_check_unconfirmed, IntervalTrigger(minutes=60))
    scheduler.add_job(job_completion_checks, IntervalTrigger(minutes=15))
    # T-24h / T-1h task reminders — coarse polling is fine, they don't need
    # second-precision and the trigger times are dynamic.
    scheduler.add_job(job_task_reminders, IntervalTrigger(minutes=5))
    # General reminders use DateTrigger jobs via reminder_scheduler for
    # ±1s precision. This is the defense-in-depth reconciliation only.
    scheduler.add_job(job_reminders_reconcile, IntervalTrigger(minutes=5))
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
