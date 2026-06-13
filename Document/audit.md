# Araka — Compliance, Security, Architecture & Performance Audit

*Date: 2026-06-14 · Scope: live production bot on `140.245.193.44` (Sarvam 105b chat, Groq/Whisper voice). Evidence cited as `file:line`. Verdicts: ✅ pass · ⚠️ partial/risk · ❌ mismatch.*

---

## 0. TL;DR

Araka is **fundamentally on the right side** of Meta's 2026 rules — it's a *purpose-built scheduling assistant*, exactly the "business-specific AI" Meta allows, not a banned general-purpose chatbot. The big compliance wins from earlier (webhook signature, opt-in/consent, STOP, data deletion, template-only third-party messaging) are **in place**. The real gaps are: (1) **brand/positioning risk** under the AI-as-product clause, (2) **business not verified** (blocks Flow publishing), (3) **PII plaintext at rest**, (4) **no rate limiting** (cost-DoS exposure), (5) **single-VM SPOF** + webhook drops failures. None are architectural rewrites — all are bounded fixes.

| Area | Posture |
|---|---|
| Meta WhatsApp policy | ✅ mostly — ⚠️ positioning + ❌ verification |
| Security | ⚠️ good transport, weak at-rest + no rate limit |
| Architecture | ✅ fine for pilot, ⚠️ single point of failure |
| Latency | ✅ good (Sarvam sub-second) — a few easy wins |

---

## 1. Meta WhatsApp Business policy — pass / mismatch

Sources: [WhatsApp 2026 AI policy](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban) · [Meta Business policy guide](https://gmcsco.com/your-simple-guide-to-whatsapp-api-compliance-2026/) · [24-hour rule](https://www.enchant.com/whatsapp-business-platform-24-hour-rule) · [opt-in guide](https://helo.ai/resources/blog/whatsapp-opt-in-complete-guide).

| # | Policy requirement | Verdict | Evidence / why |
|---|---|---|---|
| 1 | **General-purpose AI chatbots banned** (Jan 15 2026); business-specific AI (appointments, support, orders) allowed | ⚠️ **risk** | Araka *is* a scheduling assistant = allowed lane. **BUT** the onboarding ("i'm an automated AI agent… i live right here in your texts now") and the "araka, your AI" framing reads like *AI-as-the-product*, which the [Business Solution Terms](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban) prohibit ("AI as the *primary* functionality"). **Fix:** position as "a scheduling assistant" not "an AI you chat with"; keep replies task-scoped; avoid open-ended chit-chat. |
| 2 | **Opt-in before messaging** | ✅ | Onboarding records consent: `onboarding.py confirm_whatsapp()` sets `consent_status=OPT_IN`, `consent_given_at`. Privacy notice shown on first turn. |
| 3 | **Honor opt-out / STOP** | ✅ | `main.py:306 _maybe_handle_optout` → `OPT_OUT` + flags assignee tasks `unreachable`; `_maybe_handle_optin` re-enables. Welcome says "say unsubscribe anytime". |
| 4 | **24-hour customer-service window** (free-form only inside it; templates outside) | ✅ | Assignee reminders are **template-only** (`whatsapp.py send_meeting_notification`); creator (active, in-window) gets free-form. |
| 5 | **Business-initiated → approved templates, correct category** | ⚠️ | Uses a utility template (`WA_MEETING_TEMPLATE_NAME`). **Confirm it's categorised UTILITY** (not marketing) in Meta, else higher cost + stricter rules. No template-status cache (re-send blindly). |
| 6 | **Third-party (attendee) consent** | ⚠️ | Attendee never explicitly opted in, but first contact is **template + STOP-aware** (`create_event_and_notify` respects `is_assignee_reachable`). Mitigated, not perfect — keep it utility/transactional only, never promotional. |
| 7 | **Webhook signature verification** | ✅ | `main.py:99 _verify_webhook_signature` (HMAC-SHA256), `REQUIRE_WA_SIGNATURE=true` + `WA_APP_SECRET` set → **fail-closed**. (Was a P0 in the old audit; now fixed.) |
| 8 | **Prohibited content / sensitive data** | ✅ | No promotional/spam/health/finance content. Gmail/calendar data is read for the user only, not redistributed. |
| 9 | **Quality rating & messaging-limit monitoring** | ❌ | The `messages.statuses` webhook (delivered/read/**failed**/blocked) is **not processed** — you're blind to quality-rating drops and failed sends. |
| 10 | **Business Verification** | ❌ | Not verified → **"Integrity requirements not met"** blocks Flow publishing. Required for production Flows + higher messaging tiers. |
| 11 | **Right to erasure / privacy (DPDP/GDPR)** | ✅ | `main.py:370 _maybe_handle_data_deletion` ("delete my data") wipes user rows; privacy notice links a published doc. |
| 12 | **Display name / no leaking the user's number** | ✅ | Onboarding collects a display name; attendee templates use the booker's name, not raw `wa_id`. |

**Net:** compliant *as a scheduling utility*. The two things that can actually bite you: **#1 (positioning)** and **#10 (verification)**. Fix the brand language and start verification.

---

## 2. Technical / architecture audit

```
WhatsApp ─HTTPS─> Caddy(443, LE) ─> gunicorn -w1 (127.0.0.1:5000)
                                         │  Flask (sync) + 1 background asyncio loop (thread)
                                         ├─ /webhook  → dedupe(SQLite) → fire-and-forget _process_update
                                         ├─ /flow     → RSA/AES decrypt → screen logic (Sarvam-free)
                                         └─ APScheduler (in-process): reminders, T-24h/T-1h, completion
   Brain: Sarvam 105b (chat+parse) · Voice: Groq Whisper · Store: SQLite WAL (1 file)
   Google: Calendar/Meet/Contacts/Gmail · Meta: messages + templates + Flows
```

**Strengths**
- Clean separation: deterministic guards (confirmation gate, anti-hallucination time check) keep the LLM out of the trust path — this is genuinely good design and rare.
- Idempotent webhooks (`WebhookMessage` unique index), precise reminder scheduling (APScheduler DateTrigger), restart-safe OAuth (`OAuthState` table).
- Single-worker gunicorn is **correct** here (the in-process scheduler must not duplicate).
- Indexes on all hot columns (`wa_id`, `message_id`, `status`, `assignee_id`).

**Risks / weaknesses**
| Issue | Impact | Fix |
|---|---|---|
| **Single VM = SPOF** | VM dies → bot down, reminders stop | Nightly DB backup exists ✅; add an uptime monitor + documented restore. Multi-instance needs externalising the scheduler (later). |
| **Webhook always returns 200** (`main.py:240`) even on processing failure | Meta won't retry → lost messages | Persist a dead-letter row on failure, or return 500 for transient errors. |
| **Dedup write blocks the ack** (`run_async(_record_message_once).result()` before 200) | adds DB latency to every webhook | Move dedup into the async task; ack first. |
| **SQLite ceiling** | fine to low-thousands DAU; WAL contention after | Postgres migration *only when revenue demands* (≈1 day's work; some SQLite-specific DDL in `_sync_migrate`). |
| **In-process scheduler** | lost on crash between poll + fire (mitigated by reconcile sweep) | Acceptable now; externalise at scale. |
| **No automated tests for cancel/gmail/flow live paths** | regressions slip (you hit several this week) | The 11/11 sim suite is great; add a couple of `calendar_cancel`/nfm_reply unit tests. |

---

## 3. Cybersecurity risks

| # | Risk | Severity | Detail | Fix |
|---|---|---|---|---|
| 1 | **PII plaintext at rest** | **High** | SQLite stores `wa_id` (phone), attendee names/phones, chat memory, notes, Gmail/calendar snippets in clear (`database.py` has no column encryption). Only Google tokens are Fernet-encrypted. Disk/backup theft = full PII leak. | Envelope-encrypt sensitive columns (reuse the Fernet key pattern) or full-disk encryption on the volume; pseudonymise `wa_id` (SHA-256 + pepper). |
| 2 | **No per-user rate limiting** | **High** | Confirmed: none. One abuser flooding `/webhook` runs up **Sarvam + Google token cost** unbounded and can degrade the single worker. | `aiolimiter` per-`wa_id` (e.g. 20 msg/min) + a global cap; drop/queue beyond. |
| 3 | **Secrets in `.env` on disk** | Medium | All keys (Sarvam, WA token, Fernet, Flask secret, flow key) live in `/home/ubuntu/followup-bot/.env`. Single VM compromise = total. | Acceptable for pilot; tighten file perms (`chmod 600 .env`), and move to a secret manager if you scale. **Rotate the Sarvam key** (it was pasted in chat). |
| 4 | **Flow private key on disk** | Medium | `flow_private.pem` (chmod 600 ✅) decrypts all Flow traffic. | Fine as-is; ensure backups don't include it (rsync excludes it ✅). |
| 5 | **WhatsApp access token longevity** | Medium | A long-lived token in `.env`; if leaked, attacker can send as your business. | Prefer system-user tokens with rotation; monitor `messages.statuses` for anomalies. |
| 6 | **Error/info leakage** | Low (improved) | User-facing errors are now generic ✅; tool results still embed `f"Failed: {e}"` but only go to the model, not the user. | Keep; add a Sentry-style tracebacks sink server-side. |
| 7 | **No `/flow` signature check** | Low | Endpoint relies on RSA/GCM integrity (forgery infeasible) but skips `X-Hub-Signature-256`. | Add HMAC check as defense-in-depth. |
| 8 | **`/health` + `/` unauthenticated** | Low | Leak only "service running". | Fine. |

---

## 4. Latency & "smooth"

**Current profile (estimates):** Sarvam 105b ≈ **0.7s** / 30b ≈ **0.5s** per call (measured); each tool round-trip adds another LLM call + a Google API hop (~0.2–0.4s). A simple reply ≈ 1s; a gmeet booking (resolve → propose → create) ≈ 2–3s. Webhook ack is delayed by the blocking dedup write.

**Easy wins (ordered by ROI):**
1. **Ack the webhook first, process after** — move the `_record_message_once` dedup into the async task so `/webhook` returns 200 in <30 ms. (`main.py:207`)
2. **Async LLM client** — `client.chat.completions.create` is **sync**, blocking the worker thread during the call. Switch to `AsyncOpenAI` + `await` → real concurrency, lower p99 under load. (`agent.py:31,542`)
3. **Cap context before the call** — you send 20 memory msgs + 16 tool defs every time. Trim oldest memory when the prompt is large; drops input tokens ~10–20% and avoids outliers.
4. **Use `sarvam-30b` for the common path** — 0.2s faster, cheaper; keep 105b only if you see reasoning errors. One env flip (`SARVAM_MODEL=sarvam-30b`).
5. **Fewer tool round-trips** — the parse helpers in `task_service.py` make extra LLM calls; for deterministic cases the regex path (`extract_meeting_datetime`) already short-circuits — make sure it runs *first* (it does in `callback_handler`).
6. **A typing/"…" ack** for slow ops (gmeet create) so it *feels* instant.

Not needed yet: Redis cache, edge compute, multi-worker — premature at pilot scale.

---

## 5. Model usage

| Use | Model | Notes |
|---|---|---|
| Chat + tool calling | **Sarvam `sarvam-105b`** (128K) | Live-verified tool calling, sub-second, India-native. Default for reliability. |
| Field parsing (time/task) | Sarvam 105b (`task_service.py`) | Could use 30b to save cost — parsing is simpler. |
| Voice → text | **Groq `whisper-large-v3`** | Sarvam **Saarika ASR** would be better for Hindi/Indic accents — worth a future swap. |

**Recommendations:** (a) watch token spend — log `usage` per call; (b) consider **30b for parsing / 105b for chat** (split by task) to cut cost ~40% with no quality loss on the easy calls; (c) the hardening that filters junk tool-args (`agent.py`) should stay — Sarvam emits empty-key args occasionally.

---

## 6. Prioritized action list

**P0 (do soon)**
- [ ] Start **Business Verification** (unblocks Flow publish + higher tiers). [#10]
- [ ] Soften **AI-as-product positioning** in onboarding/replies → "scheduling assistant". [#1]
- [ ] Add **per-user rate limiting**. [security #2]
- [ ] **Rotate the Sarvam key**; `chmod 600 .env`. [security #3]

**P1**
- [ ] Process the **`messages.statuses`** webhook (quality/failed-send telemetry). [#9]
- [ ] **Encrypt PII columns** at rest (or encrypt the volume). [security #1]
- [ ] **Ack webhook before processing** + **async LLM client** (latency). [latency 1–2]
- [ ] Confirm template is **UTILITY** category; add a template-status cache. [#5]

**P2**
- [ ] Dead-letter on webhook processing failure. [arch]
- [ ] `/flow` `X-Hub-Signature-256` check. [security #7]
- [ ] Split models (30b parse / 105b chat); log token usage. [model]
- [ ] Sarvam Saarika ASR for voice. [model]

---

## Sources
- [WhatsApp 2026 AI policy — what's banned vs allowed](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban) · [Meta bans ChatGPT/Perplexity from WhatsApp](https://gulfnews.com/technology/no-more-chatgpt-and-perplexity-on-whatsapp-meta-bans-major-ai-chatbots-from-2026-1.500316016)
- [WhatsApp API compliance 2026](https://gmcsco.com/your-simple-guide-to-whatsapp-api-compliance-2026/) · [24-hour rule](https://www.enchant.com/whatsapp-business-platform-24-hour-rule) · [opt-in guide](https://helo.ai/resources/blog/whatsapp-opt-in-complete-guide) · [template compliance](https://www.infobip.com/docs/whatsapp/compliance/template-compliance)
- Codebase: this repo (`bot/main.py`, `bot/agent.py`, `bot/services/*`, `bot/database.py`) + live VM `140.245.193.44`.
