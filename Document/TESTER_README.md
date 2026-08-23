# tester/ — PRD user simulation

This suite plays the role of real WhatsApp users from the PRD's Appendix A
and asserts the bot behaves correctly. It drives the **real** message
router (`bot.main._process_update`) — the same code path the live webhook
uses — so onboarding, the agent loop, the Google-Meet state machine, the
confirmation gate, conflict handling, reminders and opt-out all run for
real.

Only the outside world is faked, so no keys or network are needed:

| Faked | How |
|---|---|
| WhatsApp send / templates | captured into per-number inboxes |
| Google Contacts & Calendar | canned data in `harness.CONTACTS` / fake event |
| Groq LLM | deterministic `llm_mock` that emits `set_gmeet` calls |
| Database | throwaway temp SQLite (set in `tester/__init__.py`) |

## Run

```bash
python -m tester.run
# or
./tester/run_tests.sh
```

Exit code is 0 if every scenario passes, 1 otherwise.

## The blind spot this suite has — and the second harness that covers it

The LLM here is a **deterministic mock**: it always emits the correct tool
call. That makes this suite fast, free and CI-safe, and it genuinely proves
the plumbing — routing, state machines, confirmation gates, DB writes.

What it can **never** catch is the model itself misbehaving: picking the wrong
tool, repeating a question it already asked, naming a person nobody mentioned,
or claiming something was booked when it wasn't. A production hallucination
loop shipped past a fully green run for exactly that reason.

For that, use the real-model eval:

```bash
python -m tester.prompt_eval          # 1 sample per case
python -m tester.prompt_eval -n 3     # 3 samples — surfaces flakiness
python -m tester.prompt_eval -k add_guest
```

It replays real transcripts against live Sarvam using the **production system
prompt and production tool-selection logic**, then scores the reply. It needs
`SARVAM_API_KEY`, costs API calls, and is non-deterministic — so it is
deliberately **not** part of `tester/run.py` and must not gate CI. Run it
before shipping any change to `SYSTEM_PROMPT`, the tool declarations, or
`_select_tools_for_text`. A `~ FLAKY` result is real signal: the behaviour
isn't reliable even at temperature 0.1.

## Scenarios

| Script | PRD | What it proves |
|---|---|---|
| A.1 Onboarding | FR-1 | WA# confirm → name → timezone → flags set, Google offered |
| A.2 Happy path | FR-2/6/7/8 | intent → contact resolve → confirmation gate → Task created → assignee notified |
| A.4 Missing info | FR-3/4 | no time given → bot asks → then schedules |
| A.3 Conflict | FR-5 | conflict detected → Keep both → schedules |
| A.5 Completion | FR-10 | end+1h → Done/Reschedule → marks completed |
| FR-8 STOP | FR-8/§12 | STOP → OPT_OUT + assignee_unreachable; START re-enables |
| FR-9 Reminders | FR-9 | T-24h reminder to creator (free-form) AND assignee (template); opt-out suppresses |
| Add guest to existing meet | — | "add X to this meet" adds a guest behind a confirm gate; never asks for a title/time the event already has |
| Add guest never re-asks | — | The bot must not repeat its own question; a bare "hi" must not resurrect a stale one |
| Add guest picks the event | — | Two candidate events → picker, never a silent guess |
| Tool routing matrix | — | Which tools each phrasing exposes; also that "remove X from the meeting" never exposes `calendar_cancel` |
| Empty reply isn't success | — | An empty model completion must not be reported as "done." |

## Files

- `__init__.py` — sets the isolated DB + dummy env (must import first)
- `harness.py` — `Simulator`, mock installation, personas, DB helpers
- `llm_mock.py` — deterministic stand-in for the chat model
- `scenarios.py` — the user scripts + assertions
- `run.py` — runner with a pass/fail table
- `prompt_eval.py` — real-model eval (opt-in, needs an API key; see above)

## Adding a scenario

Write `async def scenario_x(sim):` in `scenarios.py`, use `await sim.text(...)`
/ `await sim.tap(...)` to act as the user and `sim.last(wa_id)` /
`sim.all_text(wa_id)` to inspect replies, then add it to the `ALL` list.
