# araka — codebase map

*What every folder and file does, and what is safe to delete. Last verified 18 Aug 2026 against the live tree.*

Read this before deleting anything. The "safe to delete" column is the honest answer, not a guess — each entry was checked by grepping the whole repo for references.

---

## How a message flows

```
WhatsApp ──▶ Caddy (443/TLS) ──▶ gunicorn -w1 ──▶ bot/main.py  /webhook
                                                      │
   verify signature → dedupe → 200 OK fast → background asyncio loop
                                                      │
        onboarding gate → fast_path (regex, no LLM) → agent.py (LLM + tools)
                                                      │
                                              tools.py → services/* → Google
                                                      │
                                       main.py renders the reply → WhatsApp
```

One gunicorn worker, one shared event loop. That is why blocking calls matter so much here — see `bot/utils/aio.py`.

---

## `bot/` — the application

| File | What it does | Safe to delete? |
|---|---|---|
| `main.py` | Flask app. Webhook routes, signature check, dedupe, message routing, OAuth callback, Flow endpoint, background jobs. The front door. | **No** |
| `agent.py` | The LLM loop. System prompt, the 19 tool schemas, tool dispatch, guardrails (allow-list, call cap, one-write-per-request), chat memory, tool-leak recovery. | **No** |
| `tools.py` | The 19 tool implementations the model can call. Each wraps a service and enforces its own rules (confirmation gates, the anti-hallucination time check). | **No** |
| `database.py` | SQLAlchemy models + the migration runner. 9 tables. | **No** |
| `config.py` | Env var loading and validation. Fails loudly at boot on placeholders. | **No** |

### `bot/handlers/`

| File | What it does | Safe to delete? |
|---|---|---|
| `callback_handler.py` | Every button/list tap, and the deterministic continuation of a booking flow. This is where confirmation gates actually execute — the model proposes, this file acts. | **No** |

### `bot/services/` — one concern each

| File | What it does | Safe to delete? |
|---|---|---|
| `whatsapp.py` | Meta Graph API client. Text, buttons, lists, templates, Flows, media download. Has both sync and `_async` senders — see note below. | **No** |
| `google_auth.py` | OAuth2 flow, token storage (Fernet-encrypted), refresh, disconnect. | **No** |
| `calendar.py` | Calendar CRUD, Meet link creation, conflict detection, free-slot finder. | **No** |
| `contacts.py` | Contact lookup with a cache-aside layer over the People API. Cache is a **speed** layer only — Google stays the source of truth. | **No** |
| `gmail.py` | Mail search (read) and send. | **No** |
| `sheets.py` | The to-do list, which lives in a per-user Google Sheet rather than SQLite. | **No** |
| `gmeet_flow.py` | The booking state machine: resolve attendee → check conflicts → confirmation gate → create + notify. | **No** |
| `task_service.py` | Task records, conflict queries, conversation state, and the gmeet slot store that keeps a booking alive across turns. | **No** |
| `onboarding.py` | First-contact flow: WhatsApp confirm, name, timezone, consent, terms/privacy. | **No** |
| `reminder_scheduler.py` | **User-requested** reminders only ("remind me at 5pm"). Fires via APScheduler DateTrigger. Nothing to do with meetings. | **No** |
| `fast_path.py` | Tier-0 regex shortcuts that answer common messages **before** the LLM is called. Zero hallucination risk, big latency win. | **No** |
| `flow_endpoint.py` | RSA/AES decryption for WhatsApp Flow data-exchange. Required by `POST /flow`. | **No** — needed while Flows are enabled |
| `transcribe.py` | Voice notes → text via Groq Whisper. | Only if you drop voice support |
| `phone.py` | E.164 normalisation and wa_id conversion. Small, used by the booking path. | **No** |

### `bot/services/meetings/` — transcripts (scaffolding, not wired)

| File | What it does | Safe to delete? |
|---|---|---|
| `transcript_source.py` | The provider-agnostic contract: `TranscriptContext`, `Participant`, `TranscriptSegment`. Speaker attribution is first-class. | Yes, if you abandon the transcript feature |
| `meet_source.py` | Google Meet REST v2 fetch. **Untested** — free Gmail accounts cannot generate transcripts at all. | Yes, same |
| `zoom_source.py` | Zoom design notes + a working, tested VTT parser. Fetch is unimplemented (needs Server-to-Server OAuth). | Yes, same |
| `summariser.py` | `TranscriptContext` → LLM → structured summary. Has its own token budget; deliberately not routed through `agent.py`. | Yes, same |
| `prompts/join_meet_prompt.md` | The summarisation prompt, kept as a file so it can be edited and diffed without touching code. | Yes, same |

**The whole folder is inert.** Nothing imports it from the running bot, so deleting it cannot break anything today.

### `bot/utils/`

| File | What it does | Safe to delete? |
|---|---|---|
| `time.py` | Timezone handling — IST/UTC conversion, ISO parsing, AM/PM disambiguation. Load-bearing for correctness. | **No** |
| `intent.py` | `has_explicit_clock_time()` — the anti-hallucination guard that rejects any time the user did not literally type. **This is araka's single most important safety function.** | **Absolutely not** |
| `aio.py` | The `@offloaded` decorator that keeps blocking Google calls off the shared event loop. | **No** |

---

## `tester/` — the safety net

| File | What it does | Safe to delete? |
|---|---|---|
| `harness.py` | Fake WhatsApp transport + a Simulator that drives the **real** webhook entry point. Only the outside world is faked. | **No** |
| `scenarios.py` | 22 end-to-end scenarios. Each one exists because a real bug got through once. | **No** |
| `llm_mock.py` | Deterministic stand-in for the LLM, so tests never hit the network. | **No** |
| `run.py` | The runner: `python -m tester.run`. | **No** |
| `prompt_eval.py` | Prompt-quality checks against the **live** Sarvam API. Costs real credits — not part of the normal suite. | Keep, but run deliberately |
| `run_tests.sh` | Convenience wrapper. | Yes, cosmetic |

Run the suite from an isolated copy, never the app directory — it uses a temp DB, but the production `.env` sits in the app dir.

---

## `setup/` — deployment

| File | What it does | Safe to delete? |
|---|---|---|
| `followup-bot.service` | systemd unit for the app. | **No** |
| `followup-reminder.service` / `.timer` | 60-second heartbeat that reconciles due reminders — defence in depth if the in-process scheduler misses one. | **No** |
| `backup.sh` | Nightly SQLite backup: online snapshot → integrity check → gzip → prune. Needs the **execute bit**; a deploy that strips it silently kills backups. | **No** |
| `generate_secrets.py` | Generates `FLASK_SECRET` and `GOOGLE_TOKEN_ENCRYPTION_KEY`. | **No** — needed on rebuild |
| `generate_flow_keys.py` | RSA keypair for WhatsApp Flow encryption. | **No** — same |
| `run_local.sh` | Local dev launcher. | Yes, if you don't run locally |
| `flows/*.json` | Flow definitions, uploaded to Meta once. Nothing in the code reads them at runtime — they're kept as the source of record for what was uploaded. | Keep as reference; not runtime |
| `.env.example` | Template for the real `.env`. | **No** |

---

## Root

| File | What it does | Safe to delete? |
|---|---|---|
| `wsgi.py` | gunicorn entry point. | **No** |
| `reconcile_reminders.py` | Standalone script the systemd timer runs. Reminder table only. | **No** |
| `requirements.txt` | Pinned dependencies. | **No** |
| `README.md` | Project overview. | **No** |
| `async-await-explained.html` | A personal learning note. Nothing references it. | **Yes — delete** |

---

## Safe to delete, in short

**Delete now, zero risk:**
- `async-await-explained.html` — a stray learning note in the repo root
- `tester/run_tests.sh` — a one-line convenience wrapper
- `bot/services/meetings/` — the entire transcript folder, if you're not pursuing that feature

**Delete only if you drop the feature:**
- `transcribe.py` → no voice notes
- `flow_endpoint.py` + `setup/flows/` → no WhatsApp Flow forms
- `sheets.py` → no Google Sheets to-do list

**Never delete:**
- `bot/utils/intent.py` — the anti-hallucination time check
- `bot/utils/time.py` — timezone correctness
- `tester/` — the only thing standing between a refactor and a production bug
- `setup/backup.sh` — and check its execute bit after every deploy

**Vestigial but leave alone:** several columns on the `tasks` table (`completion_response`, `completion_check_sent*`, `reminder_1_sent`, `reminder_2_sent`, `reminder_offsets_min`, `rescheduled_to_id`) belonged to the completion-check and automatic-reminder features, both removed. Nothing reads them. They stay because dropping a column in SQLite means rebuilding the table — a real risk for no real gain.

---

## One thing to know before editing

Every `send_message` / `send_buttons` / `send_list` has **two forms**: a sync one and an `_async` one. Use the `_async` form inside any `async def`. The sync form exists because APScheduler jobs and the reminder scheduler call it from plain threads, where `await` is not available. Calling the sync form from a coroutine freezes the one event loop that serves every user, for the full duration of the HTTP round trip.

The same rule governs Google calls: everything in `calendar.py` and `gmail.py` carries `@offloaded`, and `sheets.py` routes each request through `_run(...)`. If you add a new Google call, offload it.
