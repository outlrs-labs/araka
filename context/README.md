# araka — `context/` folder

*Generated 2026-08-23 · every fact verified directly against the live code (`bot/`, `tester/`, `setup/`), not copied from the older docs in `Document/`. Where this folder and `Document/` disagree, this folder reflects the code.*

This is the onboarding pack for any engineer (or agent) touching araka. Read in order:

| File | What it gives you |
|---|---|
| **[OVERVIEW.md](OVERVIEW.md)** | What araka is, feature overview, tech stack, trust model |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | System graph, codebase map, module dependency + function-call graphs |
| **[FLOWS.md](FLOWS.md)** | Every runtime flow: message lifecycle, onboarding, booking, add-guest, reschedule/cancel, reminders, email, voice, OAuth, Flow crypto |
| **[DATA_MODEL.md](DATA_MODEL.md)** | All 10 SQLite tables, ERD, keying strategy |
| **[GUARDRAILS.md](GUARDRAILS.md)** | The anti-hallucination trust model: confirmation gates, time guard, tool scoping, rate limit |
| **[DOC_ISSUES.md](DOC_ISSUES.md)** | Everything found wrong/stale in `Document/` (with file:line evidence) |

## The 60-second version

araka is a WhatsApp-native scheduling assistant. You text it *"set up a call with Priya tomorrow at 3"*; it resolves Priya from Google Contacts, checks your Google Calendar, gets an explicit confirm tap, books a Google Meet event — and **also WhatsApps Priya** via a Meta-approved template (honouring `STOP`). Plus: notes, one-off/recurring reminders, a Sheets-backed to-do list, Gmail read/search, send-email via a private Flow form, voice notes (Whisper), and an opt-in post-meeting transcript→summary pipeline that you can **pull on demand** ("action items from the pricing call?") and turn into real todos/reminders from the summary card.

**Core design rule:** the LLM proposes, deterministic code disposes. Nothing irreversible (create/cancel/reschedule/add-guest) happens without a user tap on a confirmation button, and the server rejects any clock time the user didn't literally type.

## Verified numbers (as of 2026-08-23)

| Metric | Value | Source of truth |
|---|---|---|
| Tools exposed to LLM | **20** | `bot/agent.py` `TOOLS` / `TOOL_MAP` |
| DB tables | **10** | `bot/database.py` (incl. `meeting_summaries`) |
| Test scenarios | **34** | `tester/scenarios.py` `ALL` |
| APScheduler interval jobs | **2 always + 1 optional** | `bot/main.py` `start_jobs()` (L706–718) |
| Rate limit | 20 msg / 60 s per user | `bot/main.py` L263–264 |
| Chat LLM | Sarvam `sarvam-105b` (OpenAI-compatible) | `bot/config.py` L61–63 |
| Voice | Groq `whisper-large-v3` (hardcoded) | `bot/services/transcribe.py` L40 |
| Routes | `/`, `/health`, `/webhook` (GET+POST), `/auth/callback`, `/flow` | `bot/main.py` L146–217 |

> Note: several docs in `Document/` say "9 tables", "11/20/22 scenarios", or list scheduler jobs that no longer exist. See [DOC_ISSUES.md](DOC_ISSUES.md).
