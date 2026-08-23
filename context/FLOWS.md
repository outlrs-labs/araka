# araka — Runtime flows

*Verified against code 2026-08-23. All diagrams are Mermaid. State names and button IDs are the literal strings in code.*

## 1. Webhook message lifecycle (every inbound event)

```mermaid
sequenceDiagram
    participant WA as WhatsApp/Meta
    participant C as Caddy TLS
    participant F as Flask (gunicorn -w1)
    participant L as asyncio loop

    WA->>C: POST /webhook
    C->>F: proxy
    F->>F: verify X-Hub-Signature-256 (fail-closed → 403)
    F->>DB: _record_message_once(message_id) — unique-index dedupe
    alt duplicate
        F-->>WA: 200 OK (skipped)
    else new
        F->>WA: mark_read(msg_id)
        F->>L: run_coroutine_threadsafe(_process_update)
        F-->>WA: 200 OK immediately
        L->>L: rate-limit 20/60s → STOP/START/delete-my-data<br/>→ onboarding gate → route by type
    end
```

**Route table (`bot/main.py`):** `GET /` · `GET /health` · `GET /webhook` (Meta verify handshake) · `POST /webhook` · `GET /auth/callback` · `POST /flow`.

**Routing by type:** `text` → fast_path → agent · `audio` → Whisper → text path · `interactive` (button_reply/list_reply) → callback_handler · `nfm_reply` (Flow submit) → handle_flow_completion · `button` (template quick-replies) → callback_handler.

## 2. Onboarding (FR-1 state machine)

```mermaid
stateDiagram-v2
    [*] --> await_wa_confirm: first message from new number<br/>(User created, consent=PENDING)
    await_wa_confirm --> await_name: tap "Yes, that's me" (onboard_wa_yes)<br/>records OPT_IN + consent_given_at
    await_wa_confirm --> await_wa_confirm: "Different number" → ask to type it
    await_name --> await_tz: types a name
    await_tz --> consent: tap "Yes, use IST" or types IANA tz
    state consent {
        [*] --> screen: buttons Terms / Privacy / Connect Google
        screen --> screen: show_terms / show_privacy cards
    }
    consent --> connect_google: tap connect_google
    connect_google --> [*]: OAuth paste-flow completes<br/>onboarding_complete=true
```

- Google is offered, not forced — notes/reminders/to-dos work without it.
- Until complete, ALL text feeds this machine; audio gets a "finish setup first" nudge.
- `onboard_tz_yes` / `onboard_tz_change` / `onboard_wa_yes` / `onboard_wa_other` are the literal button IDs.

## 3. Google OAuth (localhost paste-flow)

```mermaid
sequenceDiagram
    participant U as User (WhatsApp)
    participant B as araka
    participant G as Google

    U->>B: tap Connect Google
    B->>DB: create OAuthState(state, wa_id) — survives restarts
    B->>U: auth URL (user opens on phone/laptop)
    G->>U: consent screen (scopes incl. gmail.send, contacts.other.readonly;<br/>+ meetings.space.created only if MEET_TRANSCRIPTS_ENABLED)
    U->>B: taps "Connect Google" again with pasted code (or GET /auth/callback)
    B->>G: exchange code → tokens
    B->>DB: store google_token_json Fernet-encrypted (+google_email)<br/>mark onboarding_complete
```

Disconnect (`disconnect_google`) revokes + wipes token and contacts cache. `delete my data` wipes every user-scoped row (see DATA_MODEL).

## 4. Meeting booking — `set_gmeet` (the core flow)

```mermaid
flowchart TD
    A["user: 'set up a call with Priya tomorrow at 3'"] --> B[agent selects set_gmeet]
    B --> T{time-guard:<br/>has_explicit_clock_time?}
    T -->|model passed a time<br/>user never typed| TREJ[tool rejects — asks user]
    T -->|ok| C[resolve attendee via contacts_cache<br/>→ People API → otherContacts]

    C -->|multiple matches| P1[gmeet_contact_ list picker<br/>state=awaiting_gmeet_contact]
    C -->|no email known| E1{Flow configured?}
    E1 -->|yes WA_GMEET_FLOW_ID| FL[send_gmeet_flow card<br/>state=awaiting_gmeet_flow]
    E1 -->|no| E2[state=awaiting_gmeet_email<br/>ask in chat]
    C -->|email known| Q1{time given?}

    P1 --> RESOLVED
    FL -->|nfm_reply| FC[handle_gmeet_flow_completion] --> RESOLVED
    E2 -->|user types email| RESOLVED
    Q1 -->|no| QT[state=awaiting_gmeet_time ask] --> RESOLVED
    Q1 -->|yes| RESOLVED{conflict check<br/>calendar day_busy}

    RESOLVED -->|free| CC[confirm card gmeet_confirm_*<br/>title·date·time·attendee<br/>state=awaiting_gmeet_confirmation]
    RESOLVED -->|busy| CS[slot offers conflict_*<br/>state=awaiting_conflict_resolution] --> CC

    CC -->|"tap Confirm"| GO[gmeet_flow.create_event_and_notify]
    CC -->|"Edit / cancel_flow"| PURGE[purge recent ChatMemory rows<br/>clear conversation state]
    CS -->|pick slot| CC

    GO --> EV[Calendar event + Meet link<br/>Task status=scheduled external_event_id set]
    EV --> N1[creator: confirmation message]
    EV --> N2[attendee: utility template gmeet_confirmation<br/>{{1}}booker {{2}}title {{3}}datetime {{4}}link<br/>buttons meeting_add_calendar|link · meeting_decline<br/>respects assignee_unreachable STOP flag]
```

**Conversation states used by the booking machine** (`task_conversation_state.flow_state`, context JSON carries `flow_kind=gmeet` + `gmeet_data`):

| State | Meaning |
|---|---|
| `awaiting_gmeet_time` | need a start time |
| `awaiting_gmeet_email` | need attendee's email |
| `awaiting_gmeet_contact` | contact picker shown |
| `awaiting_gmeet_flow` | Flow form open |
| `awaiting_conflict_resolution` | slot picker after conflict |
| `awaiting_gmeet_confirmation` | confirm card shown |

Any active state short-circuits the LLM: `handle_gmeet_text_reply` runs BEFORE fast_path/agent so "3pm works" lands in the machine, not in chat.

## 5. Add guest to existing event — `calendar_add_attendee`

```mermaid
flowchart TD
    A["'add harsh yadav to this meet'"] --> B[calendar_add_attendee tool]
    B --> C{identify existing event<br/>by query/attendee/time}
    C -->|ambiguous| EP[add_attendee_event_ list picker<br/>state=awaiting_add_attendee_event]
    C -->|clear| D{new guest's email known?}
    EP --> D
    D -->|multiple people match| CP[add_attendee_contact_ picker<br/>state=awaiting_add_attendee_contact]
    D -->|unknown email| AE[state=awaiting_add_attendee_email ask]
    CP & AE & D -->|resolved| CONF[confirm card add_attendee_confirm_*<br/>state=awaiting_add_attendee_confirmation]
    CONF -->|Confirm| ADD[update Google event attendees<br/>+ notify new guest via template]
    CONF -->|No| CLR[cancel state]
```

This flow exists because of the July hallucination incident: "add X" previously matched `set_gmeet` and the bot tried to book a NEW meeting. The tool description explicitly forbids that ("Never use set_gmeet to add someone to a meeting that already exists").

## 6. Reschedule & cancel

```mermaid
flowchart TD
    subgraph RESCHED["calendar_reschedule"]
        R1["'move pricing call to fri 4pm'"] --> R2[find event by query]
        R2 --> R3{time-guard on new time} --> R4{conflict check}
        R4 --> R5[card calendar_reschedule_confirm_*<br/>state=awaiting_calendar_reschedule_confirmation]
        R5 -->|Confirm| R6[calendar_update internal-only<br/>result rendered deterministically by main.py]
    end
    subgraph CANCEL["calendar_cancel"]
        C1["'cancel my 4pm'"] --> C2[single clear match → delete]
        C1 -->|vague| C3[list upcoming events to choose]
        C1 -->|"cancel all today"| C4[scope cancel_all to today]
        C1 -->|"cancel everything"| C5[confirm then cancel_all]
    end
    NOTE["'remove X from the meeting' routes to NO TOOL —<br/>never cancel the whole event for one guest"]
```

## 7. Reminders

```mermaid
flowchart TD
    subgraph SETPATH["setting"]
        S1["fast_path regexes:<br/>'remind me in 20 min to call mom'<br/>'remind me to X in 20 mins' / inverted order"] -->|instant| S4[Reminder row + schedule_reminder DateTrigger<br/>reply written to ChatMemory too]
        S2[LLM path set_reminder] --> S3{relative_minutes given?<br/>server computes time itself} --> S4
        S2b[daily recurring: is_recurring + recur_time_hhmm] --> S4
    end
    subgraph FIREPATH["firing (three independent triggers, one atomic claim)"]
        F1[DateTrigger at remind_at ±1s]
        F2[in-app reconcile sweep every 5 min]
        F3[systemd heartbeat ~60s separate process]
        F1 & F2 & F3 --> FA["_fire_reminder_async:<br/>UPDATE reminders SET is_sent WHERE id=? AND is_sent=False<br/>⇒ exactly one winner"]
        FA --> FB{user inside 24h window?}
        FB -->|yes| FC1[free-form message]
        FB -->|no + template configured| FC2[utility template send_reminder_notification<br/>one body var = reminder text]
        FB -->|no + no template| FD[fails silently logged]
        FA --> FE{still undelivered stuck?<br/>ADMIN_WA_ID set}
        FE --> FF[operator alert ping]
    end
    LIST["list/delete: list_reminders · delete_reminder<br/>(+'reminders'/'list my reminders' hits fast_path)"]
```

> Old docs mention automatic **T-24h/T-1h attendee nudges** and completion sweeps — those jobs were removed; only creator-requested reminders exist now.

## 8. Email — read vs send

```mermaid
flowchart TD
    subgraph READ["gmail_search (readonly)"]
        A1["'any mail from priya?'"] --> A2[agent calls gmail_search<br/>query/limit/include_body]
        A2 --> A3[Gmail API → formatted digest<br/>day/week summaries pass limit=30,<br/>bodies only for single-email questions]
    end
    subgraph SEND["compose_email (privacy path)"]
        B1["'email john@x.com'"] --> B2{WA_EMAIL_FLOW_ID set?}
        B2 -->|no| B3[report feature unavailable —<br/>NEVER drafts content in chat]
        B2 -->|yes| B4[send_email_flow form<br/>prefill To ONLY if user typed literal address]
        B4 --> B5[user types To/Subject/Body]
        B5 --> B6[nfm_reply → handle_email_flow_completion<br/>detected by intent=send_email + body field]
        B6 --> B7[gmail.send_email direct<br/>AI never sees content]
        B7 --> B8["reply: sent to <addr>"]
    end
```

## 9. Dynamic Flow data-exchange (`POST /flow`) — crypto

```mermaid
sequenceDiagram
    participant M as Meta
    participant E as /flow endpoint

    M->>E: {encrypted_flow_data, encrypted_aes_key, initial_vector}
    Note over E: ① RSA-OAEP-SHA256 unwrap AES key<br/>(flow_private.pem chmod 600)
    Note over E: ② AES-128-GCM decrypt payload
    E->>E: build SCHEDULE/SUMMARY screen<br/>with LIVE free-busy slots (calendar.py)
    Note over E: ③ bit-invert IV (each byte XOR 0xFF) —<br/>the #1 way to get this wrong
    Note over E: ④ re-encrypt with SAME AES key + flipped IV<br/>⑤ base64 → text/plain
    E-->>M: encrypted screen response
```

Enabled by `WA_GMEET_FLOW_DYNAMIC=true`; static form keeps working otherwise. Ping/health payloads handled per Meta spec; errors return encrypted error screens, not plaintext.

## 10. Voice notes

```mermaid
flowchart LR
    V1[audio message] --> V2[download_media via Graph API<br/>whatsapp.py]
    V2 --> V3[temp .ogg file]
    V3 --> V4[Groq whisper-large-v3<br/>60 s timeout, 1 retry, to_thread]
    V4 -->|transcript| V5[_handle_text normal path]
    V4 -->|fail/empty| V6["sorry, i couldn't understand that audio."]
```

Groq key optional — feature degrades gracefully (`config.validate()` doesn't require it).

## 11. Transcript pipeline (opt-in)

```mermaid
flowchart LR
    T0["MEET_TRANSCRIPTS_ENABLED=true<br/>(adds meetings.space.created scope<br/>to NEXT OAuth consents)"] --> T1[job_meeting_transcripts every 10 min]
    T1 --> T2[pipeline.run_once]
    T2 --> T3{tasks ended recently<br/>without summary row?}
    T3 -->|Google Meet eligible| T4[meet_source Meet REST v2]
    T3 -->|Zoom creds set| T5[zoom_source fetch UNIMPLEMENTED<br/>VTT parse tested]
    T4 & T5 --> T6[summariser LLM → MeetingSummary]
    T6 --> T7[INSERT meeting_summaries FIRST<br/>row = idempotency key → deliver]
    T3 -->|none| T8[sleep]
```

Free Gmail accounts can't generate Meet transcripts — hence default-off and Workspace-tier gating.

## 11b. Summary pull path ("what were the action items from that call?")

```mermaid
flowchart TD
    Q1["'action items from the pricing call?'"] --> T1[get_meeting_summaries tool<br/>reads meeting_summaries + tasks]
    T1 -->|no rows| T2[no_summaries message]
    T1 -->|1 clear match / only one| T3[send_meeting_summary_card:<br/>full text card, then buttons]
    T1 -->|several match| T4[mtgsummary_ list picker<br/>state=awaiting_mtg_pick] --> T3
    T3 --> B1[mtg_todos_add tap] --> T5[sheets.add_todo_to_sheet<br/>one batch ≤5 items → confirmation]
    T3 --> B2[mtg_remind_set tap] --> T6[state=awaiting_mtg_reminder_time<br/>bot ASKS for a clock time]
    T6 -->|"tomorrow 9 am"| T7[set_reminder per item<br/>user_text time-guard applies]
    T6 -->|time-free reply| T8[refused — never guesses a time]
```

Rules baked in: the LLM never narrates or edits summaries (deterministic renderer); the card never steals an active booking's conversation slot; reminder times must come from the user's literal words (same anti-hallucination guard as everywhere else).

## 12. Consent & compliance flows

```mermaid
flowchart TD
    STOP["STOP / unsubscribe / opt-out…"] --> O1[consent_status=OPT_OUT<br/>flag all tasks where user is assignee<br/>assignee_unreachable=True]
    START[START / resume] --> O2[OPT_IN + unflag tasks]
    DEL["delete my data / forget me / erase my data"] --> O3[wipe: notes, chat_memory, contacts_cache,<br/>task states, oauth_states, reminders, tasks,<br/>User row — right to erasure]
    ANY["STOP/START honoured even for numbers<br/>that only ever received a template"]
```
