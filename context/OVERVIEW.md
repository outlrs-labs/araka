# araka — Product & feature overview

*Verified against code 2026-08-23.*

## 1. What it is

araka is a **WhatsApp-native scheduling assistant** built on the WhatsApp Business Cloud API. The user texts naturally; araka coordinates the meeting with **both sides**, then keeps notes, reminders, to-dos, and email one message away.

```
"set up a call with Priya tomorrow at 3"
        │
        ▼
 resolve contact ──► check calendar conflicts ──► confirm card (button tap)
        │                                                   │
        ▼                                                   ▼
 Google Meet event created ◄──────────────────── user taps Confirm
        │
        ├──► creator gets confirmation + Meet link
        └──► attendee WhatsApped via approved utility template (STOP-aware)
```

## 2. Why it exists (positioning)

- **Allowed lane under Meta's 2026 rules.** Meta bans *general-purpose* AI chatbots on WhatsApp but allows business-specific utilities (scheduling = the textbook example). araka is deliberately narrow.
- **Bilateral coordination is the moat.** Single-player assistants can't message the other person on a compliant channel. araka does, via template + opt-out (`assignee_unreachable` flag on STOP).
- **Deterministic by design.** Framework migration to autonomous-agent stacks was evaluated and rejected (see `Document/framework-migration-eval.md`) — reliability comes from guards, not model intelligence.

## 3. Tech stack (verified)

| Layer | Choice | Detail |
|---|---|---|
| Chat/tool-calling LLM | Sarvam `sarvam-105b` | OpenAI-compatible client, temp 0.1, sync call offloaded via `asyncio.to_thread` |
| Voice transcription | Groq `whisper-large-v3` | Hardcoded in `transcribe.py:40`; 60 s timeout, 1 retry |
| Web server | Flask 3.0.3 + gunicorn `-w 1` | Single worker is mandatory (in-process scheduler) |
| Concurrency | 1 background asyncio loop + thread offloading | `bot/utils/aio.py` `@offloaded` for blocking calls |
| DB | SQLite WAL via SQLAlchemy async (`aiosqlite`) | `notebot.db`; Fernet encryption for tokens/contacts cache |
| Scheduler | APScheduler in-process + external heartbeat | `reconcile_reminders.py` every ~60 s via systemd timer |
| Google | Calendar · Meet (via Calendar) · Contacts (+OtherContacts) · Gmail (readonly+send) · Sheets | OAuth2 localhost paste-flow; scopes in `config.py:73–87` |
| Forms | WhatsApp Flows ×3 (gmeet static / gmeet dynamic / email) | Dynamic uses RSA-OAEP + AES-128-GCM `/flow` endpoint |
| Proxy/host | Caddy TLS → Oracle Cloud "Always Free" Ubuntu VM | `140-245-193-44.sslip.io` |

## 4. Feature overview

### 4.1 Scheduling (the core)
- `set_gmeet` — book a NEW meeting: contact resolution → conflict check → confirmation gate → event + Meet link → bilateral notify.
- Attendee unknown? Either ask in chat or pop a native **WhatsApp Flow form** (static date/time/email, or dynamic with live free-busy slots from the encrypted `/flow` endpoint).
- Editing a proposal purges recent ChatMemory so the LLM never sees half-finished bookings (prevents false "I've scheduled it").

### 4.2 Calendar management
- `calendar_create` (personal events), `calendar_get_all`, `calendar_cancel` (query/cancel_all/pick-list), `calendar_reschedule` (propose→confirm), `calendar_add_attendee` (add guest to an EXISTING event — never creates).
- `calendar_delete`/`calendar_update` exist in `tools.py` but are **not exposed** to the LLM (internal-only, hallucination safety).

### 4.3 Reminders
- One-off (`remind me tomorrow 5pm …`) and daily-recurring reminders.
- Relative reminders ("in 20 min") caught by the Tier-0 regex fast path — no LLM involved.
- Fired via APScheduler `DateTrigger` (±1 s); reconcile sweep (5 min in-app + ~60 s systemd heartbeat) re-fires anything missed; atomic claim prevents double-fire.
- Out-of-24h-window sends fall back to an approved utility template (`WA_REMINDER_TEMPLATE_NAME`).

### 4.4 Notes
- `save_note` / `get_notes` / `delete_note`. Free text + detected date.

### 4.5 To-do list
- Lives in a **per-user Google Sheet** (not SQLite): `add_todo` / `get_todos` / `complete_todo` / `delete_todo`.

### 4.6 Email
- Read: `gmail_search` (readonly scope, summaries or full bodies).
- Send: `compose_email` opens a Flow form; user types To/Subject/Body; submit goes straight `Flow → gmail.send`. **The AI never sees or writes content.** No flow configured ⇒ feature reported unavailable (never falls back to chat drafting).

### 4.7 People
- `contacts_search` over a cache-aside layer (`contacts_cache` table, Fernet-encrypted emails/phones) backed by Google Contacts + Gmail OtherContacts.

### 4.8 Voice notes
- Audio messages → media download → Groq Whisper → processed as text.

### 4.9 Post-meeting transcripts (opt-in, default OFF)
- `MEET_TRANSCRIPTS_ENABLED=true` adds a 10-min poller (`job_meeting_transcripts`): ended meetings → fetch transcript (Google Meet REST v2 or Zoom VTT) → LLM summary → `meeting_summaries` table (row = idempotency key). Requires extra Google scope `meetings.space.created`, requested only when enabled. Zoom fetch needs Server-to-Server OAuth creds; VTT parsing works, fetch unimplemented.
- **Pull path:** `get_meeting_summaries(query)` finds a stored summary ("action items from the pricing call?") → deterministic card → buttons turn its action items into real todos (`mtg_todos_add`) or reminders after the user supplies a clock time (`mtg_remind_set` → `awaiting_mtg_reminder_time`). Push path delivers through the same card.

### 4.10 Trust/compliance surface
- Onboarding: WhatsApp-number confirm → name → timezone → transparent consent screen (Terms/Privacy buttons before Connect Google). Google optional.
- `STOP`/`START`, `delete my data` handled deterministically at any point.
- Webhook HMAC signature verification, fail-closed; durable dedupe by `message_id`.
- Per-user rate limit: sliding window, 20 msg / 60 s (`main.py:263`).

### 4.11 What araka deliberately does NOT do
- No open-ended chat (capability questions answered from the real tool list).
- No autonomous actions: every create/cancel/reschedule/add needs an explicit tap.
- No inventing times: server rejects any clock time absent from the user's literal words (`bot/utils/intent.py` `has_explicit_clock_time()`).
- "Remove X from the meeting" routes to **no tool** (it must not delete whole events).

## 5. Environments & ops facts

| Thing | Value |
|---|---|
| Public host | `https://140-245-193-44.sslip.io` (Caddy auto-TLS via sslip.io) |
| Webhook | `POST /webhook`, verify token GET handshake, X-Hub-Signature-256 checked |
| Health | `GET /health` |
| Flow endpoint | `POST /flow` (RSA private key `flow_private.pem` chmod 600, excluded from backups/git) |
| systemd | `followup-bot.service`; `followup-reminder.service` + `.timer` (~60 s heartbeat) |
| Backups | `setup/backup.sh` nightly 2 AM: SQLite online `.backup` + integrity check + gzip + optional AES-256 + 14-day retention |
| OAuth | Localhost paste-flow — `BASE_URL` stays blank |

## 6. Open risks (carried from audit, still true)

1. Business Verification incomplete → blocks publishing Flows to non-testers.
2. PII plaintext at rest (only Google tokens + contacts cache encrypted).
3. `messages.statuses` webhook not processed → blind to failed sends / quality drops.
4. Single-VM SPOF (backups mitigate data loss, not downtime).
5. Dedupe write still blocks the webhook ack (audit latency item #1, unfixed — see DOC_ISSUES.md §audit).
