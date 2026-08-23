# What is wrong / stale in `Document/`

*Audit date: 2026-08-23. Every item below was verified against the live code, with file/line evidence. Severity: 🔴 wrong now (will mislead) · 🟡 outdated-but-flagged · ⚪ cosmetic.*

The single biggest theme: **`Document/` docs disagree with each other AND with the code on three numbers** (tables, scenarios, scheduler jobs), and the **transcripts feature went from "inert scaffolding" to "wired"** without any doc noticing.

---

## 1. CONTEXT.md — the "canonical" file has drifted

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 1.1 | §3 "APScheduler jobs" | Lists `job_check_drafts` (30m), `job_check_unconfirmed` (60m), `job_completion_checks` (15m), `job_task_reminders` (5m) alongside reconcile + prune | **Those four jobs don't exist.** `main.py start_jobs()` (L706–718) registers only: `job_reminders_reconcile` 5 min, `job_prune_chat_memory` 30 min, and — only if `MEET_TRANSCRIPTS_ENABLED=true` — `job_meeting_transcripts` 10 min. The completion-check/draft-timeout features were REMOVED (their columns are even documented as vestigial in CODEBASE_MAP.md §"Vestigial" — CONTEXT contradicts its own sibling doc) | 🔴 |
| 1.2 | §5 title & body | "Data model — **9 tables**" | **10 tables.** `meeting_summaries` exists in `database.py:151–176` and is missing from the table list entirely | 🔴 |
| 1.3 | §11 heading + §6 map | "**20 scenarios**" (§11) vs "**11-scenario end-to-end simulation suite**" (§6) | Both wrong; also self-contradictory within the same file. Actual `tester/scenarios.py ALL` = **33 entries** (L1478–1512) | 🔴 |
| 1.4 | §13 | "No per-user rate limiting beyond the basic 20 msg/60s check… confirm whether that satisfies" — phrased as if uncertain/unimplemented | A full sliding-window limiter IS implemented (`main.py:259–282`, warning message, per-window notification dedupe). The audit's "High severity, confirmed none" is factually obsolete | 🟡 |
| 1.5 | §10 Config reference | Presented as grouped summary of `.env.example`, claiming that file is "kept current" | `.env.example` itself is stale — it's missing `WA_GMEET_FLOW_DYNAMIC`, `FLOW_PRIVATE_KEY_PATH`, `FLOW_KEY_PASSPHRASE`, `WA_FLOW_DRAFT_MODE`, `MEET_TRANSCRIPTS_ENABLED`, `ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET`, `MEET_TRANSCRIPT_LOOKBACK_HOURS`, all of which exist in `config.py` and some of which §7/§8 of CONTEXT describe as real features | 🔴 |
| 1.6 | whole file | No mention anywhere of: transcripts feature wiring, `meeting_summaries`, Zoom config, `MEET_TRANSCRIPTS_ENABLED` scope coupling (`config.py:89–117`) | The newest feature is invisible in the canonical context doc | 🟡 |

## 2. CODEBASE_MAP.md

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 2.1 | `bot/services/meetings/` section | "The whole folder is **inert**. Nothing imports it from the running bot, so deleting it cannot break anything today." Also listed under "Delete now, zero risk" | **False since the transcripts feature was wired:** `main.py:661` does `from bot.services.meetings import pipeline` inside `job_meeting_transcripts`; `database.py` defines the `MeetingSummary` model the pipeline writes; `config.py` has `MEET_TRANSCRIPTS_ENABLED`/Zoom vars; four test scenarios exercise it (`transcript_context`, `transcript_pipeline_is_inert`, `zoom_fetch`, `provider_routing`). Deleting it breaks the flag-gated job and the test suite | 🔴 |
| 2.2 | tester section | "`scenarios.py` — **22** end-to-end scenarios" | Actual = **33** | 🔴 |
| 2.3 | bot/ table | "`database.py` … **9 tables**" | 10 | 🔴 |
| 2.4 | reminder_scheduler row | "User-requested reminders only… Nothing to do with meetings" | Correct today — but README still describes attendee nudges living here (see 3.2); the two docs contradict each other | 🟡 |

## 3. README.md

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 3.1 | Quick start / codebase map | "`bash tester/run_tests.sh # 11 scenarios`", "11 end-to-end scenarios" | **33** | 🔴 |
| 3.2 | Reminder-engine mermaid diagram | Shows `T-24h / T-1h attendee nudge via template`, `completion sweep`, `draft-timeout sweep` | All three jobs/features were removed from the code. Only creator-requested reminders exist. Diagram documents ghost behaviour | 🔴 |
| 3.3 | Data-model ERD | Shows 8 entities; omits `contacts_cache` and `meeting_summaries` | Model list incomplete (and no table count stated, so less harmful) | 🟡 |
| 3.4 | Tool reference | Accurate (19 tools incl. add-attendee) ✓ | no action | ⚪ |

## 4. TESTER_README.md

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 4.1 | Scenarios table (~L58–71) | Lists **12** scenarios | **33** in `ALL`. Missing ~21 rows (heartbeat, tool-leak, add-guest ×3, same-name contacts, transcript ×3, zoom fetch, provider routing, gmail-limit, markup, long-answer budget, unknown-contacts, phantom-promise, overload-retry…) | 🔴 |
| 4.2 | Row "A.5 Completion FR-10 end+1h → marks completed" | Documents a completion scenario | No such scenario; completion checks were removed from the product | 🔴 |
| 4.3 | Row "FR-9 Reminders … T-24h reminder to creator AND assignee (template)" | Describes two-leg auto reminders | Real scenario tests a user-created reminder with template fallback for the creator only; assignee auto-nudges don't exist | 🔴 |
| 4.4 | Harness table | "Groq LLM … deterministic llm_mock that emits `set_gmeet` calls" | (a) mocked chat model is Sarvam now; (b) `llm_mock` emits many tools incl. `calendar_add_attendee` | 🟡 |

## 5. META_TEMPLATE_SETUP.md

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 5.1 | L22–23 | "Reminders to the assignee use the same template" | Auto assignee reminders removed. Template is used by booking notification + add-guest notify only (`gmeet_flow.py:431`, `tools.py:809`). Variable order/buttons in this doc ARE correct | 🟡 |

## 6. ORACLE_DEPLOY.md

| # | Where | Claim | Reality | Sev |
|---|---|---|---|---|
| 6.1 | Part 6 ~L123 | rsync from `/Users/harshyadav/Documents/Bot tele copy` | Folder renamed to `araka main`. Instruction copies a nonexistent path | 🔴 |
| 6.2 | Part 9 ~L192 | Nightly backup = plain `cp notebot.db ~/backups/` | Contradicts `setup/backup.sh` whose header says it *replaces* unsafe cp (cp can capture torn WAL state). Teaches the retired unsafe method | 🔴 |
| 6.3 | Part 7 ~L148 | "(systemd unit already written for ubuntu user + loopback bind)" | Shipped `followup-bot.service` binds `-b 0.0.0.0:5000`; loopback happens only after the sed fix in the very next command line — parenthetical contradicts its own fix | 🟡 |

## 7. Strategy/migration docs (historical value, but stale premises)

| # | File | Issue | Sev |
|---|---|---|---|
| 7.1 | araka-vs-poke-strategy.md L47, L68 | Unit economics built on "Groq llama-4-scout" — chat brain is Sarvam `sarvam-105b` now | 🟡 |
| 7.2 | araka-vs-poke-strategy.md Sources | Cites `futurePlan.md` — deleted from Document/ | 🟡 |
| 7.3 | framework-migration-eval.md L33 | Premise "You're on llama-4-scout" — its own recommendation #2 (swap the model) has since been DONE (Sarvam). Doc should be marked executed | 🟡 |
| 7.4 | framework-migration-eval.md rec #3 | Says field-level PII encryption missing — tokens + contacts cache already Fernet-encrypted; only notes/chat/task PII remain plaintext | 🟡 |

## 8. Code-internal doc rot (not in Document/, but doc nonetheless)

| # | Location | Issue | Sev |
|---|---|---|---|
| 8.1 | `bot/agent.py:1–4` module docstring | "Groq + function-calling… 9 tools + 10-msg memory" | Runtime is Sarvam, 19 tools, 20-msg memory. (`_call_groq` naming staleness was already flagged by CONTEXT; the docstring's counts weren't) | 🟡 |
| 8.2 | `requirements.txt:11` | Comment "# AI Provider (Groq)" above `openai`+`groq` | Chat provider is Sarvam; Groq is transcription-only. Known/historical but still misleading | ⚪ |
| 8.3 | `setup/.env.example:33–36` | Section titled "Groq (LLM)" presenting GROQ_MODEL as the LLM; also `GROQ_MODEL` default in `config.py` (`llama-3.3-70b-versatile`) is never used by transcription (whisper hardcoded) | 🟡 |
| 8.4 | `setup/.env.example:65` | References `Document/WHATSAPP_FLOWS_SETUP.md` — deleted file | 🔴 |
| 8.5 | `setup/.env.example` | Missing all Flow-dynamic/transcripts/Zoom keys listed in 1.5 | 🔴 |

## 9. Cross-doc contradiction matrix (who says what)

| Fact | CONTEXT.md | CODEBASE_MAP.md | README.md | TESTER_README.md | Code |
|---|---|---|---|---|---|
| Test scenario count | 20 (§11) / 11 (§6) | 22 | 11 | 12 listed | **33** |
| DB tables | 9 | 9 | (no count, ERD misses 2) | — | **10** |
| Scheduler jobs | 6 listed (4 phantom) | implies reconcile+reminder heartbeat | completion/draft/nudges shown | completion scenario shown | **reconcile + prune (+ transcripts opt-in)** |
| meetings/ folder | not mentioned | inert, delete freely | not mentioned | — | **wired behind MEET_TRANSCRIPTS_ENABLED** |
| Rate limiting | "confirm whether needed" | — | pending-from-audit P0 | — | **implemented 20/60s** |

## 10. Suggested fix order

1. Patch CONTEXT.md §3 (jobs), §5 (add `meeting_summaries`), §6/§11 (33 scenarios), §10 (env vars actually in `.env.example` or fix the example).
2. Rewrite CODEBASE_MAP.md meetings/ section (wired, flag-gated) + counts.
3. Regenerate README's reminder diagram + scenario count.
4. Update TESTER_README scenario table from `scenarios.ALL`.
5. Fix ORACLE_DEPLOY rsync path + backup instructions.
6. Refresh `setup/.env.example` (add dynamic-flow + transcript keys, drop dead doc link, relabel Groq section).
7. Add one-line banners to strategy docs: "model premise outdated — Sarvam swap done."
