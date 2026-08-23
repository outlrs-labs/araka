# araka — Architecture & function graphs

*Verified against code 2026-08-23. All graphs are Mermaid.*

## 1. System architecture

```mermaid
graph TD
    WA([WhatsApp Cloud API])
    CADDY["Caddy :443 TLS (sslip.io)"]
    FLASK["Flask + gunicorn -w 1 · 127.0.0.1:5000"]

    subgraph PROC["araka process (single worker, single asyncio loop)"]
        ROUTER["main.py — webhook router<br/>signature → dedupe → ack fast"]
        FASTPATH["fast_path.py — Tier-0 regexes<br/>(relative reminders, list reminders)"]
        AGENT["agent.py — Sarvam tool-calling loop<br/>20 tools · 20-msg memory · guards"]
        CB["callback_handler.py — buttons/lists/flows<br/>confirmation gates live here"]
        FLOWEP["flow_endpoint.py — RSA/AES /flow"]
        SCHED["APScheduler (in-process)<br/>reconcile 5m · prune-memory 30m · transcripts 10m*"]
        TOOLS["tools.py — tool implementations"]
    end

    DB[("SQLite WAL<br/>notebot.db · 10 tables")]
    SARVAM(["Sarvam sarvam-105b"])
    GROQ(["Groq whisper-large-v3"])
    GOOGLE(["Google: Calendar · Meet · Contacts<br/>Gmail · Sheets · Meet transcripts*"])
    META(["Meta Graph API"])

    WA -->|HTTPS| CADDY --> FLASK --> ROUTER
    ROUTER -->|text| FASTPATH
    FASTPATH -->|no match| AGENT
    ROUTER -->|interactive / nfm_reply| CB
    ROUTER -->|data_exchange| FLOWEP
    ROUTER -->|audio| GROQ
    AGENT <-->|chat completions| SARVAM
    AGENT --> TOOLS
    CB --> TOOLS
    TOOLS --> DB
    TOOLS --> GOOGLE
    SCHED --> DB
    SCHED -.->|transcripts, MEET_TRANSCRIPTS_ENABLED=true| GOOGLE
    SCHED -->|reminders / nudges| META
    TOOLS --> META
```

**Why one worker:** APScheduler lives in-process; a second gunicorn worker would double-fire every reminder. Concurrency comes from the background asyncio loop (`main.py` boots it on a thread) with blocking calls offloaded to worker threads (`bot/utils/aio.py`, `asyncio.to_thread` in `agent.py`/`transcribe.py`).

## 2. Codebase map

```
bot/
├── main.py                     Flask app: routes, signature check, dedupe,
│                               rate limit, message router, scheduler boot
├── agent.py                    Sarvam loop: SYSTEM_PROMPT, TOOLS (20),
│                               TOOL_MAP, guard state, memory, JSON-leak recovery
├── config.py                   every env var + validate() + Google scopes
├── database.py                 10 SQLAlchemy models + migration runner + WAL pragmas
├── tools.py                    the 20 tool bodies (+2 internal-only:
│                               calendar_delete/calendar_update)
├── handlers/
│   └── callback_handler.py     interactive router + gmeet/add-guest/reschedule/
│                               email flow state machines + confirmation executors
└── services/
    ├── fast_path.py            Tier-0 anchored regexes (bypass LLM)
    ├── whatsapp.py             Meta Graph client: sync + _async senders,
    │                           templates, flows, media download
    ├── onboarding.py           await_wa_confirm→await_name→await_tz→consent
    ├── google_auth.py          OAuth paste-flow, Fernet token crypto, disconnect
    ├── calendar.py             CRUD, Meet link, day_busy(), free-slot finder
    ├── contacts.py             People API + otherContacts over contacts_cache
    ├── gmail.py                search/read + send_email
    ├── sheets.py               per-user todo Sheet (3 tabs)
    ├── gmeet_flow.py           booking pipeline: resolve→conflicts→create+notify
    ├── task_service.py         Task lifecycle, conversation state store,
    │                           datetime parsing, gmeet slot cache
    ├── reminder_scheduler.py   DateTrigger jobs, atomic claim, reload_pending()
    ├── flow_endpoint.py        RSA-OAEP-SHA256 unwrap + AES-128-GCM (+IV flip)
    ├── transcribe.py           Groq Whisper voice→text (60 s timeout)
    └── meetings/               transcript→summary pipeline (opt-in)
        ├── pipeline.py         run_once(): ended tasks → fetch → summarise → row
        ├── transcript_source.py provider-agnostic contract
        ├── meet_source.py      Meet REST v2 (Workspace-tier only)
        ├── zoom_source.py      VTT parser tested; fetch unimplemented
        └── summariser.py       TranscriptContext → MeetingSummary via LLM
bot/utils/
    ├── time.py                 IST/UTC, ISO parse, AM/PM disambiguation
    ├── intent.py               has_explicit_clock_time() — THE anti-hallucination guard
    └── aio.py                  @offloaded decorator
root:
    reconcile_reminders.py      standalone 1-shot heartbeat (systemd timer)
setup/                          .env.example, systemd units, key generators,
                                backup.sh, flows/*.json (source of record only)
tester/                         harness + llm_mock + scenarios.ALL = 34 + prompt_eval
Document/                       older docs (see context/DOC_ISSUES.md for drift)
context/                        this folder
```

## 3. Module dependency graph

```mermaid
graph LR
    MAIN[main.py] --> AGENT[agent.py]
    MAIN --> CB[callback_handler.py]
    MAIN --> ONB[onboarding.py]
    MAIN --> TS[task_service.py]
    MAIN --> RSC[reminder_scheduler.py]
    MAIN --> FP[fast_path.py]
    MAIN --> FE[flow_endpoint.py]
    MAIN --> TRX[transcribe.py]
    MAIN -.->|opt-in flag| MT[meetings/pipeline.py]

    AGENT --> TOOLS[tools.py]
    CB --> TOOLS
    CB --> TS

    TOOLS --> CAL[calendar.py]
    TOOLS --> CON[contacts.py]
    TOOLS --> GM[gmail.py]
    TOOLS --> SH[sheets.py]
    TOOLS --> GF[gmeet_flow.py]
    TOOLS --> WA[whatsapp.py]
    TOOLS --> TS

    CAL --> GAUTH[google_auth.py]
    CON --> GAUTH
    GM --> GAUTH
    SH --> GAUTH
    MT --> GAUTH

    AGENT --> DB[database.py]
    CB --> DB
    TOOLS --> DB
    FP --> DB
    RSC --> DB
    MT --> DB

    AGENT --> UTIL[utils/intent.py · utils/time.py · utils/aio.py]
    TOOLS --> UTIL
    CB --> UTIL
```

## 4. Function graph — inbound text turn

```mermaid
flowchart TD
    A[handle_webhook<br/>main.py:217] --> B[_verify_webhook_signature<br/>HMAC-SHA256 fail-closed]
    B -->|bad| X[403]
    B --> C[_record_message_once<br/>durable dedupe, unique msg_id]
    C -->|dup| Z[skip]
    C --> D[mark_read]
    D --> E[run_coroutine_threadsafe<br/>_process_update → return 200]
    E --> F[_rate_limited? 20/60s sliding window]
    F -->|over| Z2[drop + one warning per window]
    F --> G[ensure_user get-or-create]
    G -->|text| H{STOP / START /<br/>delete my data?}
    H -->|yes| HH[deterministic handler, done]
    H -->|no| I{onboarded?}
    I -->|new| J[start_onboarding]
    I -->|in progress| K[handle_onboarding_message]
    I -->|yes| L[handle_gmeet_text_reply<br/>active conversation state?]
    L -->|handled| DONE[send reply via whatsapp.py]
    L -->|no| M[fast_path.try_handle<br/>anchored regexes]
    M -->|match| DONE
    M -->|no match| N[process_message agent.py]
    N --> O[load ChatMemory 20 msgs,<br/>2h TTL filter on read]
    O --> P[_select_tools_for_text<br/>full toolset minus deny-list]
    P --> Q[_call_groq name is stale!<br/>= Sarvam call, to_thread, 3 retries]
    Q -->|tool_calls| R[guard checks:<br/>allow-list · ≤4 calls · 1 write/turn<br/>time-guard on clock times]
    R --> S[TOOL_MAP dispatch → tools.py]
    S --> T[result cached for deterministic<br/>renderer in main.py]
    T --> Q
    Q -->|final text| U[empty-completion guard<br/>never claim success silently]
    U --> V[save memory → send reply]
    V --> W{cached structured action?<br/>confirm cards, pickers}
    W -->|yes| W2[render card/buttons instead of prose]
```

## 5. Function graph — interactive/button turn

```mermaid
flowchart TD
    A[type=interactive or button] --> B[handle_interactive_reply<br/>callback_handler.py:68]
    B --> C{route by reply_id prefix}
    C -->|connect_google / disconnect_google| D[google_auth handle_connect/_disconnect]
    C -->|show_capabilities / show_terms / show_privacy| E[deterministic info cards]
    C -->|gmeet_confirm_*| F["_handle_gmeet_confirm<br/>(executes create_event_and_notify)"]
    C -->|calendar_reschedule_confirm_*| G[_handle_calendar_reschedule_confirm]
    C -->|add_attendee_confirm_*| H[_handle_add_attendee_confirm]
    C -->|add_attendee_contact_* / add_attendee_event_*| I[pickers]
    C -->|gmeet_contact_*| J[_handle_gmeet_contact_choice]
    C -->|conflict_*| K[_handle_conflict slot offers]
    C -->|meeting_add_calendar\|link / meeting_decline| L[attendee template quick-replies]
    C -->|onboard_*| M[onboarding steps]
    C -->|cancel_flow| N[purge state + recent memory]
    C -->|unknown| O["unknown action. try again."]
    F & G & H --> P[clear task_conversation_state<br/>send confirmations]
```

## 6. Background jobs (actual, verified)

```mermaid
flowchart LR
    subgraph INPROC["APScheduler in main.py start_jobs()"]
        J1["job_reminders_reconcile<br/>every 5 min"] --> F1[sweep due unsent reminders →<br/>reminder_scheduler._fire_reminder_async<br/>atomic UPDATE…WHERE is_sent=False]
        J2["job_prune_chat_memory<br/>every 30 min"] --> F2[DELETE chat_memory older than 2 h]
        J3["job_meeting_transcripts<br/>every 10 min<br/>ONLY if MEET_TRANSCRIPTS_ENABLED"] --> F3[meetings/pipeline.run_once:<br/>ended tasks → transcript → summary →<br/>meeting_summaries idempotency row]
    end
    subgraph ONESHOT["reminder_scheduler (same scheduler instance)"]
        J4["DateTrigger per reminder<br/>(±1 s precision)"] --> F4[_fire_reminder_async:<br/>free-form in window else<br/>utility template fallback]
        J5["reload_pending() on boot<br/>rehydrates from DB"] --> F4
    end
    subgraph EXTERNAL["systemd followup-reminder.timer ~60 s"]
        J6[reconcile_reminders.py<br/>standalone process] --> F5[same atomic claim ⇒ safe alongside app]
    end
```

> Removed features that old docs still mention: `job_check_drafts`, `job_check_unconfirmed`, `job_completion_checks`, `job_task_reminders`, and automatic T-24h/T-1h attendee nudges **no longer exist** (vestigial columns remain on `tasks`; see CODEBASE note in DOC_ISSUES.md).

## 7. Sync-vs-async rule (the #1 way to break this app)

Every WhatsApp send and every Google call has two forms:

```mermaid
graph LR
    subgraph COROUTINE["inside async def (event loop)"]
        A2[send_message_async / @offloaded calls]
    end
    subgraph THREAD["plain thread (APScheduler job)"]
        B2[send_message sync form OK here]
    end
    WRONG["❌ calling sync form inside a coroutine<br/>freezes THE one event loop for ALL users"]
```

- `whatsapp.py` exposes both `send_message` and `send_message_async`.
- `calendar.py`/`gmail.py` wrap blocking Google calls with `@offloaded`.
- `sheets.py` routes each request through `_run(...)`.
- Sarvam + Whisper calls go through `asyncio.to_thread`.

## 8. Testing architecture

```mermaid
graph TD
    subgraph MOCKED["python -m tester.run — CI-safe, no network"]
        H[harness.Simulator<br/>drives real bot.main._process_update] --> SC[scenarios.ALL = 34]
        LM[llm_mock deterministic<br/>emits set_gmeet AND<br/>calendar_add_attendee etc.] --> H
        FAKE[fake WhatsApp inbox per number<br/>canned Google responses] --> H
        SC --> TMP[(temp SQLite DB)]
    end
    subgraph LIVE["python -m tester.prompt_eval — opt-in, costs money"]
        PE[prompt_eval replays real transcripts<br/>against live Sarvam with prod prompt<br/>+ prod _select_tools_for_text] --> SCORE[scored: right tool · no repeated question ·<br/>no invented person · no false success]
    end
    BLINDSPOT["Mocked suite can NEVER catch model-invented questions<br/>(the July hallucination class) — that's prompt_eval's job"]
```
