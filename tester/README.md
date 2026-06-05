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

## Files

- `__init__.py` — sets the isolated DB + dummy env (must import first)
- `harness.py` — `Simulator`, mock installation, personas, DB helpers
- `llm_mock.py` — deterministic stand-in for the Groq model
- `scenarios.py` — the user scripts + assertions
- `run.py` — runner with a pass/fail table

## Adding a scenario

Write `async def scenario_x(sim):` in `scenarios.py`, use `await sim.text(...)`
/ `await sim.tap(...)` to act as the user and `sim.last(wa_id)` /
`sim.all_text(wa_id)` to inspect replies, then add it to the `ALL` list.
