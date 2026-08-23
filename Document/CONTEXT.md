# araka (FollowUp Bot) — Project Context

*Compiled: 2026-07-25 · Source of truth: live codebase at repo root (`bot/`, `setup/`, `tester/`), cross-checked file-by-file against every doc in this folder. This is the canonical current-state reference — when a fact here conflicts with an older doc, this file wins.*

---

## 1. What it is

araka is a WhatsApp-native scheduling assistant (WhatsApp Business Cloud API). A user texts it naturally ("set up a call with Priya tomorrow at 3"); it resolves the contact via Google Contacts, checks Google Calendar for conflicts, gets an explicit confirm tap, creates a Google Meet event, and **also messages the other person** (bilateral coordination) via a Meta-approved template, honoring `STOP`/opt-out. It also handles notes, one-off/recurring reminders, a Google-Sheets-backed to-do list, and read/send access to Gmail.

**Trust model:** the LLM never books/cancels/invents a time unilaterally. A confirmation gate, an anti-hallucination time check (server rejects any clock time the user didn't literally type), and a one-field-at-a-time accumulator sit between the model and any irreversible action.

---

## 2. Current tech stack (verified against `requirements.txt` / `bot/config.py`)

| Layer | Current | Notes |
|---|---|---|
| Chat/tool-calling LLM | **Sarvam `sarvam-105b`** via OpenAI-compatible client (`SARVAM_BASE_URL`, `SARVAM_API_KEY`) | temperature `0.1`. `bot/agent.py`'s own docstring and internal function name (`_call_groq`) still say "Groq" — that's stale wording left over from before the Sarvam switch; the runtime call goes to Sarvam. `requirements.txt` still lists Groq under the comment `# AI Provider (Groq)` for the same historical reason. |
| Voice transcription | **Groq `whisper-large-v3`** (`bot/services/transcribe.py`) | Groq is real and current for this one path only. |
| Web/app server | Flask 3.0.3 + gunicorn 22.0.0, **`-w 1` (single worker, mandatory)** | The reminder scheduler runs in-process; a 2nd worker double-fires every reminder. |
| DB | SQLite (WAL mode) via SQLAlchemy 2.0.25 (async, `aiosqlite`) | `notebot.db`, `notebot.db-shm`/`-wal` are working files, gitignored. |
| Scheduler | APScheduler 3.10.4, in-process | Plus an independent `reconcile_reminders.py` heartbeat (§8). |
| Google | Calendar, Meet (via Calendar events), Contacts (People API), Gmail (readonly + send), Sheets | OAuth2, tokens Fernet-encrypted at rest when `GOOGLE_TOKEN_ENCRYPTION_KEY` is set. |
| TLS/proxy | Caddy (auto Let's Encrypt via `sslip.io`) | Production only. |
| Host | Oracle Cloud "Always Free" VM, Ubuntu | See §9 for current IP — it has changed since some docs were written. |

---

## 3. Message flow

```
WhatsApp ──HTTPS──▶ Caddy (443, TLS) ──▶ gunicorn -w1 (127.0.0.1:5000)
                                              │
                    POST /webhook  ──▶ verify X-Hub-Signature-256 (fail-closed)
                                       ──▶ dedupe via WebhookMessage (unique message_id)
                                       ──▶ mark_read
                                       ──▶ return 200 fast
                                       ──▶ fire-and-forget on background asyncio loop:
                                             rate-limit (20 msg/60s) → STOP/START/data-deletion
                                             → onboarding gate → route by type:
                                                 text      → fast_path.py (regex, no LLM) → agent.py
                                                 audio     → Groq Whisper → text path
                                                 interactive (button/list) → callback_handler.py
                                                 nfm_reply (Flow submit)   → handle_flow_completion
                    POST /flow     ──▶ RSA/AES decrypt (Meta Flow data-exchange, §7)
```

`agent.py` loads a 20-message rolling `ChatMemory` window (2h TTL pruning), calls Sarvam with the tools `_select_tools_for_text()` scoped to this turn (§4), executes any tool call against `tools.py`, loops until a final text reply, saves memory, sends the reply. Structured tool results are cached and rendered by `main.py`'s deterministic dispatcher rather than described by the model (§12).

**Tier-0 fast path** (`bot/services/fast_path.py`, new since the last full doc pass): a small set of strictly-anchored regexes intercepts obvious cases (e.g. relative reminders, "list my reminders") **before** the LLM is invoked — instant reply, zero hallucination risk on those turns. Anything not matched falls through to the normal agent loop. Every fast-path turn is still written to `ChatMemory` so follow-ups stay coherent.

**Routes** (`bot/main.py`): `GET /`, `GET /health`, `GET/POST /webhook`, `GET /auth/callback`, `POST /flow`.

**APScheduler jobs**: `job_check_drafts` (30 min), `job_check_unconfirmed` (60 min), `job_completion_checks` (15 min), `job_task_reminders` (5 min), `job_reminders_reconcile` (5 min), `job_prune_chat_memory` (30 min).

---

## 4. Tools exposed to the LLM — **19**

Verified directly against `TOOLS`/`TOOL_MAP` in `bot/agent.py` (grepped, not estimated).

| Category | Tools |
|---|---|
| Scheduling | `set_gmeet` — book a NEW meeting |
| Calendar | `calendar_create` · `calendar_get_all` · `calendar_cancel` · `calendar_reschedule` · `calendar_add_attendee` |
| People | `contacts_search` |
| Email (read) | `gmail_search` |
| Email (send) | `compose_email` — opens a WhatsApp Flow form; the AI never sees the composed content (§7) |
| Notes | `save_note` · `get_notes` · `delete_note` |
| Reminders | `set_reminder` · `list_reminders` · `delete_reminder` |
| To-dos (Google Sheets-backed) | `add_todo` · `get_todos` · `complete_todo` · `delete_todo` |

`bot/tools.py` also implements `calendar_delete` and `calendar_update`, which exist but are **deliberately not exposed to the LLM** (used internally by the deterministic gmeet flow, not by free tool-calling — direct calendar-ID hallucination risk).

**Tool scoping matters more than it looks.** `_select_tools_for_text()` in `bot/agent.py` exposes only a small subset per turn. That's a latency and safety win, but it means a request with no matching tool leaves the model *cornered*: it will improvise, and improvising is where hallucination comes from. Any new user-facing capability needs a routing branch, not just a tool. See §12.

---

## 5. Data model — 9 tables (`bot/database.py`)

| Table | Key columns | Purpose |
|---|---|---|
| `users` | `wa_id` (unique), `onboarding_step`, `consent_status`, `google_token_json` (Fernet-encrypted), `google_email`, `timezone` | Core identity + onboarding + consent + Google link |
| `notes` | `user_id` FK, `text`, `detected_date` | Free-text notes |
| `chat_memory` | `user_id`, `role`, `text` | Rolling LLM context, 2h TTL pruned |
| `contacts_cache` | `user_id` FK, `name`/`name_lower`, `email`/`phone` (Fernet-encrypted), `source` | Cache-aside layer over Google Contacts/Gmail otherContacts (<1ms lookup vs ~500ms API). Wiped on data-deletion, disconnect, or "refresh contacts". |
| `webhook_messages` | `message_id` (unique) | Durable dedupe for inbound webhook deliveries |
| `reminders` | `wa_id`, `remind_at`, `is_recurring`, `recur_time`, `is_sent` | One-off + recurring reminders |
| `tasks` | `wa_id`, `assignee_*`, `scheduled_at` (UTC), `status` (draft→scheduled→completed/rescheduled/cancelled/unconfirmed), `external_event_id`, `assignee_unreachable` | The meeting/commitment state machine |
| `task_conversation_state` | `wa_id` (unique), `flow_state`, `context_json` | Per-user interactive-flow tracker (gmeet state machine, etc.) |
| `oauth_states` | `state` (unique), `wa_id` | Durable OAuth handshake — survives a process restart mid-auth |

Everything except `notes`/`chat_memory`/`contacts_cache` is keyed directly by the `wa_id` string rather than a `users.id` FK, keeping the hot path a single indexed lookup. **To-dos live in a per-user Google Sheet**, not SQLite (`bot/services/sheets.py`).

---

## 6. Codebase map

```
bot/
├── main.py                    Flask app · webhook · scheduler boot · routes
├── agent.py                   Sarvam tool-calling loop, 19 tools, 20-msg memory
├── config.py                  every env var + validate()
├── database.py                SQLAlchemy models + SQLite WAL pragmas
├── tools.py                   tool implementations the LLM calls
├── handlers/callback_handler.py   button/list/Flow-reply routing, gmeet + email flow state machines
└── services/
    ├── fast_path.py           NEW — Tier-0 regex router, bypasses the LLM for narrow patterns
    ├── whatsapp.py            Meta Graph API client (send/buttons/list/templates/flow/media)
    ├── onboarding.py          3-step setup + consent screen
    ├── google_auth.py         OAuth paste-flow, Fernet encrypt/decrypt
    ├── calendar.py            Calendar CRUD, conflict + free-slot finder
    ├── gmeet_flow.py          deterministic meeting-booking state machine
    ├── flow_endpoint.py       RSA-OAEP + AES-128-GCM /flow endpoint (dynamic meeting Flow)
    ├── gmail.py               Gmail search/read (readonly) + send_email (gmail.send)
    ├── contacts.py            Google People API search, backed by contacts_cache
    ├── reminder_scheduler.py  APScheduler job registration, precise DateTrigger firing
    ├── task_service.py        task lifecycle, conversation state, datetime parsing
    ├── transcribe.py          Groq Whisper voice→text
    ├── sheets.py               per-user Google Sheet (to-dos + logging tabs)
    └── phone.py                E.164 normalisation

reconcile_reminders.py         NEW — standalone one-shot reminder heartbeat (§8)
setup/                         .env.example, systemd units, key/secret generators, flows/*.json, backup.sh
tester/                        11-scenario end-to-end simulation suite (no live network)
Document/                      this folder
```

---

## 7. WhatsApp Flows (native forms)

Three Flow JSON files exist in `setup/flows/`:

| Flow | File | Trigger | Status |
|---|---|---|---|
| Meeting form (static) | `gmeet_flow.json` | attendee email unknown, no LLM in the loop for the form itself | Live if `WA_GMEET_FLOW_ID` set |
| Meeting form (dynamic) | `gmeet_flow_dynamic.json` | same, but the Time list is filtered live against Google Calendar via an encrypted `/flow` endpoint | Opt-in via `WA_GMEET_FLOW_DYNAMIC=true`; dormant otherwise, static form still works |
| Email compose | `email_flow.json` | `compose_email` tool opens it; user types To/Subject/Body | Live if `WA_EMAIL_FLOW_ID` set |

**Why email is a Flow, not chat:** the form submits straight to `gmail.send_email()` — the LLM only *opens* the form, never reads or writes subject/body. If `WA_EMAIL_FLOW_ID` is unset, `compose_email` reports the feature unavailable rather than falling back to drafting in chat (which would defeat the privacy point). See [`EMAIL_FLOW_SETUP.md`](EMAIL_FLOW_SETUP.md).

**Dynamic Flow crypto** (`bot/services/flow_endpoint.py`): RSA-OAEP-SHA256 unwraps the AES key, AES-128-GCM decrypts the body, the response reuses the same AES key with a **bit-inverted IV** (each byte XOR `0xFF` — this exact detail is the #1 way to get it wrong). Private key `flow_private.pem` (chmod 600) lives only on the VM, excluded from backups and git. Setup steps: [`FLOW_ENDPOINT_SETUP.md`](FLOW_ENDPOINT_SETUP.md).

Handler map: `send_gmeet_flow()` / `handle_gmeet_flow_completion()` and `send_email_flow()` / `handle_email_flow_completion()`, both in `bot/handlers/callback_handler.py`; `handle_flow_completion()` tells the two apart by payload shape (`intent=send_email` + `body` field ⇒ email).

---

## 8. Reminders, templates, and the 24-hour window

A reminder fired to a user who's been quiet >24h is a **business-initiated** message — WhatsApp blocks the free-form send (`131047`). The code already falls back to an approved **utility template** when that happens (`reminder_scheduler._fire_reminder_async` → `send_reminder_notification`); you just need the template created and named in `.env`:

- `WA_REMINDER_TEMPLATE_NAME` / `WA_REMINDER_TEMPLATE_LANGUAGE` — creator-facing reminder fallback. Setup: [`REMINDER_TEMPLATE_SETUP.md`](REMINDER_TEMPLATE_SETUP.md).
- `WA_MEETING_TEMPLATE_NAME` (default `gmeet_confirmation`) / `WA_MEETING_TEMPLATE_LANGUAGE` — **always** used (not just a fallback) to notify the third-party attendee, since they're outside the 24h window by default. Body parameters, verified against `send_meeting_notification()` in `bot/services/whatsapp.py`, **must be in this exact order**: `{{1}}` booker name, `{{2}}` meeting title, `{{3}}` date/time, `{{4}}` Meet link. Two quick-reply buttons, payloads `meeting_add_calendar|<link>` and `meeting_decline`. Full Meta setup steps (webhook + this template + test-mode + go-live): [`META_TEMPLATE_SETUP.md`](META_TEMPLATE_SETUP.md).

  *(This folder used to also have `MEETING_TEMPLATE_SETUP.md`, an earlier and slightly different draft of the same template — same variable order and buttons, different exact wording. It's been removed as a superseded duplicate; `META_TEMPLATE_SETUP.md` is the one to follow.)*

**External reminder heartbeat** (new): `reconcile_reminders.py`, run every ~60s by `setup/followup-reminder.timer` + `.service` (systemd), independent of the web process. If gunicorn/APScheduler is wedged or restarting, this still fires due reminders — reminders are claimed atomically from the DB (source of truth), so it's safe to run alongside the in-app scheduler without double-firing.

---

## 9. Production deployment — current facts

Oracle Cloud "Always Free" Ubuntu VM behind Caddy (auto Let's Encrypt via `sslip.io`), gunicorn bound to loopback only.

- **Current public IP / host: `140.245.193.44`** → `https://140-245-193-44.sslip.io`. Confirmed current by the two most recently written docs that reference it (`FLOW_ENDPOINT_SETUP.md`, `audit.md`, both mid-to-late June) and by `README.md`.
- `ORACLE_DEPLOY.md` previously referenced an older VM's IP (`129.159.235.161`), left over from before the VM was recreated. Corrected in this pass — it now consistently uses `140.245.193.44` throughout.
- Webhook callback: `https://140-245-193-44.sslip.io/webhook`. Health check: `https://140-245-193-44.sslip.io/health`. Flow data-exchange endpoint: `https://140-245-193-44.sslip.io/flow`.
- Google OAuth uses the **localhost paste flow** (no domain callback needed); `BASE_URL` stays blank unless that's deliberately changed later.
- systemd units: `followup-bot.service` (the app), `followup-reminder.service` + `followup-reminder.timer` (the reconcile heartbeat, §8).
- Backups: `setup/backup.sh`, daily 2 AM cron, SQLite online `.backup` (not `cp`) + integrity check + gzip + optional AES-256 encryption + 14-day retention. Full restore procedure: [`DB_BACKUP_RESTORE.md`](DB_BACKUP_RESTORE.md).

---

## 10. Config reference

Canonical list is `setup/.env.example` (kept current — do not duplicate it here beyond flagging what matters). Fields grouped:

- **WhatsApp**: `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`, `WA_VERIFY_TOKEN`, `WA_API_VERSION`, `WA_APP_SECRET` + `REQUIRE_WA_SIGNATURE` (fail-closed signature check), `WA_MEETING_TEMPLATE_NAME/LANGUAGE`, `WA_REMINDER_TEMPLATE_NAME/LANGUAGE`, `WA_GMEET_FLOW_ID`, `WA_GMEET_FLOW_DYNAMIC`, `WA_EMAIL_FLOW_ID`, `FLOW_PRIVATE_KEY_PATH`, `FLOW_KEY_PASSPHRASE`, `WA_FLOW_DRAFT_MODE`.
- **LLM**: `SARVAM_API_KEY`, `SARVAM_MODEL` (`sarvam-105b`), `SARVAM_BASE_URL` — the actual chat brain. `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_BASE_URL` — voice transcription only.
- **Google**: `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_ENCRYPTION_KEY` (Fernet — required for tokens/contacts-cache encryption at rest). Scopes requested: `calendar`, `spreadsheets`, `contacts.readonly`, `gmail.readonly`, `gmail.send`.
- **DB**: `DATABASE_URL` (default `sqlite+aiosqlite:///notebot.db`), `SQLITE_BUSY_TIMEOUT_MS`, `WEBHOOK_DEDUPE_TTL_HOURS`.
- **Bot**: `BOT_TIMEZONE` (default `Asia/Kolkata`), `ADMIN_WA_ID` (stuck-reminder alert target).
- **Server**: `FLASK_PORT`, `FLASK_SECRET`, `BASE_URL` (leave blank unless switching off the localhost OAuth paste flow).

`config.validate()` hard-requires WhatsApp + Sarvam keys; Groq is optional (transcription-only feature degrades gracefully without it).

---

## 11. Testing — two harnesses, deliberately different

### `python -m tester.run` — mocked, deterministic, CI-safe (20 scenarios)

Drives the real message router (`bot.main._process_update`) against a throwaway SQLite DB, with WhatsApp sends captured to per-number inboxes, Google Contacts/Calendar canned, and a deterministic LLM mock — no keys or network needed. Covers onboarding, booking, missing-info accumulation, conflicts, completion checks, STOP/START, both reminder legs, Flow forms, the reminder heartbeat, add-attendee, tool routing, and the empty-completion guard.

**Its structural blind spot:** the LLM is mocked, so it always emits the *correct* tool call. This suite can prove the plumbing works but can **never** catch the model inventing a question — which is exactly how the July hallucination bug shipped green.

### `python -m tester.prompt_eval` — real model, opt-in, NOT for CI

Replays real transcripts against live Sarvam using the production system prompt and the production tool-selection logic, then scores the response: right tool chosen, no repeated question, no invented person, no detail re-asked that already exists, no success claimed without a tool result. Needs `SARVAM_API_KEY`; costs API calls; non-deterministic.

```bash
python -m tester.prompt_eval          # 1 sample per case
python -m tester.prompt_eval -n 3     # 3 samples — surfaces flakiness
python -m tester.prompt_eval -k add_guest
```

Run it before shipping any change to `SYSTEM_PROMPT`, the tool declarations, or `_select_tools_for_text`. A `~ FLAKY` result is a real signal, not noise — it means the behaviour isn't reliable at temperature 0.1.

Full detail: [`TESTER_README.md`](TESTER_README.md).

---

## 12. The hallucination class — root cause and current guards

*Added 2026-07-26 after a production loop where "can we add harsh yadav in this meet as well?" made the bot ask "which Mohit should I invite?" four times.*

The model was not being stupid — it was **cornered**. `_select_tools_for_text()` matched the word "meet" and exposed exactly one tool, `set_gmeet`, whose job is creating a *new* meeting and whose required fields are an attendee, a title and a time. With no `calendar_add_attendee` to reach for, the only way to express "add someone" was to start a booking, so it asked for the title and time the existing event already had. A bare "hi" then exposed *no* tools at all, leaving the model to generate from `ChatMemory` — where it found its own previous question and repeated it.

Three lessons that should shape future changes:

1. **A missing tool is a hallucination bug.** If users ask for something no tool covers, the model will invent a path. Add the capability *or* make the prompt decline explicitly — never leave it unrouted.
2. **A missing routing branch is the same bug.** A tool that exists but is never selected is invisible to the model.
3. **Structured tool results must always reach a deterministic renderer.** `main.py` previously surfaced only `set_gmeet` payloads, and `get_last_gmeet_result()` *popped* the shared cache, silently discarding every other tool's result — so `calendar_reschedule` proposals were dropped and their confirm buttons never appeared.

Guards now in place:
- `calendar_add_attendee` (tool + routing + confirmation gate + pickers).
- `main.py` dispatches **every** cached tool action, not just `set_gmeet`.
- Prompt rules 6–8: never re-ask a question, the person in the current message is the subject, never ask for a detail the tool didn't request.
- An explicit **capabilities/limits** section so capability questions are answered from the real tool list.
- "remove X from the meeting" routes to **no tool** — it previously matched the cancel branch and could have deleted the entire event to satisfy a request to drop one guest.
- An empty model completion is no longer reported as `"done."` (a false success claim).
- `tester/prompt_eval.py` exists specifically to catch this class before it ships.

## 13. Compliance, security & known issues

Condensed from [`audit.md`](audit.md) (2026-06-14 — still the most current audit; treat it as accurate except where noted below) plus what's visibly changed in code since:

**Already addressed since the audit was written:**
- Onboarding copy (`bot/services/onboarding.py`) already says *"araka is a scheduling assistant — not a general chatbot"* — the audit's P0 "AI-as-product positioning" concern is at least partly mitigated. The one line Meta requires ("i'm an automated AI agent") is still there because it's a disclosure requirement, not a positioning choice.

**Still open** (P0/P1 from the audit, unless you've since resolved them outside this codebase):
- Business Verification not yet complete (blocks Flow publishing to non-testers).
- No per-user rate limiting beyond the basic 20 msg/60s check seen in `main.py` — confirm whether that satisfies the audit's cost-DoS concern or whether stronger limiting is still needed.
- PII (phone numbers, attendee names, notes, chat memory) stored in SQLite plaintext; only Google tokens and the contacts cache are Fernet-encrypted.
- `messages.statuses` webhook (delivery/read/failed/quality) is not processed — no visibility into failed sends or quality-rating drops.
- Single-VM SPOF; mitigated by nightly backups + the external reminder heartbeat, not by redundancy.

Full risk table, severities, and the prioritized P0/P1/P2 action list live in `audit.md` — this file doesn't duplicate it, just flags what's moved since.

---

## 14. Business/strategy context (pointers, not duplicated here)

- [`araka-vs-poke-strategy.md`](araka-vs-poke-strategy.md) — competitive positioning vs. Poke (iMessage assistant); the core thesis is that Meta's general-chatbot ban on WhatsApp plus araka's bilateral (both-sides) coordination is a structural moat Poke can't follow into. Still current strategic framing as of 2026-06-12.
- [`framework-migration-eval.md`](framework-migration-eval.md) — evaluated and rejected migrating to Hermes Agent / OpenClaw (wrong category — general autonomous agents vs. this project's deliberately narrow, deterministic, guard-railed design); recommended swapping/tuning the model first, and only reaching for a structured-output library like Pydantic-AI on `agent.py` if tool-calling reliability is still an issue after that. Decision stands unless explicitly revisited.

---

## 15. Document index (current contents of this folder)

| File | Covers |
|---|---|
| **`CONTEXT.md`** | this file — canonical current-state reference |
| `DB_BACKUP_RESTORE.md` | backup mechanics + full restore procedure |
| `EMAIL_FLOW_SETUP.md` | send-email-from-chat Flow, `gmail.send` scope |
| `FLOW_ENDPOINT_SETUP.md` | dynamic meeting-Flow endpoint (RSA/AES setup, keys, Meta wiring) |
| `META_TEMPLATE_SETUP.md` | webhook config + assignee/meeting notification template |
| `ORACLE_DEPLOY.md` | full VM provisioning/hardening/deploy walkthrough |
| `REMINDER_TEMPLATE_SETUP.md` | reminder-fallback template for the 24h-window problem |
| `TESTER_README.md` | how to run/extend the simulation test suite |
| `PRIVACY_POLICY.md` / `TERMS_AND_CONDITIONS.md` | published legal docs |
| `audit.md` | compliance/security/architecture/latency audit (2026-06-14) |
| `araka-vs-poke-strategy.md` | competitive strategy analysis |
| `framework-migration-eval.md` | Hermes/OpenClaw migration evaluation (decision: no) |

### Removed during this pass (2026-07-25) — superseded, not just old

These were deleted as outdated duplicates/snapshots. They remain recoverable from git history (`git log -- Document/<file>`) if needed for reference.

| Removed file | Why |
|---|---|
| `report.md` | Architecture snapshot dated 2026-05-17 describing a since-replaced stack (9 tools, Groq `llama-3.3-70b` as the chat model). Fully superseded by `audit.md` + this file. |
| `futurePlan.md` | Due-diligence report dated 2026-05-19, referencing a since-renamed project folder ("Bot tele copy") and a stale tool count (13). Fully superseded by `audit.md` + this file. |
| `DEPLOY.md` | Generic pre-decision hosting comparison (Fly.io/Oracle/AWS/GCP). The project settled on Oracle; `ORACLE_DEPLOY.md` is the actual, detailed, current deployment doc. |
| `MEETING_TEMPLATE_SETUP.md` | Earlier draft of the same assignee-notification template covered more completely in `META_TEMPLATE_SETUP.md` (dated later, June 1 vs May 26). |
| `WHATSAPP_FLOWS_SETUP.md` | Internally inconsistent — mixed the old VM IP (`129.159.235.161`) and the new one (`140.245.193.44`) in the same file because it was only partially updated after the VM was recreated. Its accurate content is folded into `FLOW_ENDPOINT_SETUP.md`, `META_TEMPLATE_SETUP.md`, and this file. |
