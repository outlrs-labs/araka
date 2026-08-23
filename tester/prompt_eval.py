"""Real-model prompt eval — the harness the mock suite structurally cannot be.

`tester/run.py` replaces the LLM with a deterministic mock, so it proves the
plumbing works but can NEVER catch the model inventing a question: the mock
always emits the right tool call. Every hallucination in production got past
a green suite for exactly that reason.

This harness replays real transcripts against the LIVE model, using the real
system prompt and the real tool-selection logic, and scores what comes back:

    - did it pick the right tool (or correctly pick none)?
    - did it repeat a question it already asked?
    - did it name a person nobody mentioned?
    - did it ask for a detail that already exists?
    - did it claim success no tool confirmed?

Non-deterministic and costs API calls, so it is NOT part of `tester/run.py`
and must not gate CI. Run it before shipping a prompt change.

    python -m tester.prompt_eval                # all cases, 1 sample each
    python -m tester.prompt_eval -n 3           # 3 samples each (flakiness)
    python -m tester.prompt_eval -k add_guest   # filter by name

Exit code 0 only if every sample of every case passes.
"""

from __future__ import annotations

import argparse
import os
import sys

# Import order matters: bot.config reads the real .env from the project root.
from bot import agent
from bot.config import config
from bot.utils.time import now_prompt_str


GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

USER_NAME = "Harsh"
USER_TZ = "Asia/Kolkata"

# A meeting that ALREADY EXISTS. Several cases hang off this, because the
# whole bug class is the model treating an existing meeting as a new one.
BOOKED = (
    "saved. Meeting with Mohit Puns.\n"
    "Mon Jul 27, 08:00 PM to 08:30 PM IST\n"
    "meet: https://meet.google.com/viu-dfsa-axw\n"
    "reminders set for 24h and 1h before."
)


# ── Cases ───────────────────────────────────────────────────────
# history: prior turns as (role, text), oldest first. `user` is the turn
# under test. Checks are deliberately about BEHAVIOUR, not wording.
CASES = [
    {
        "name": "add_guest_routes_to_add_attendee",
        "why": "The production loop: this must not become a new booking.",
        "history": [("user", "meeting with Mohit Puns tomorrow 8 PM, google meet"),
                    ("assistant", BOOKED)],
        "user": "can we add harsh yadav SST Baba in this meet as well?",
        "expect_tool": "calendar_add_attendee",
        "forbid_text": ["title", "what time", "when should", "which mohit"],
    },
    {
        "name": "add_guest_does_not_reask_original_attendee",
        "why": "It asked 'which Mohit' four times in production. Never again.",
        "history": [("user", "meeting with Mohit Puns tomorrow 8 PM, google meet"),
                    ("assistant", BOOKED),
                    ("user", "can we add harsh yadav in this meet as well?"),
                    ("assistant", "which harsh should i add? send their gmail.")],
        "user": "Please add yharsh499@gmail.com to this meet as well",
        "expect_tool": "calendar_add_attendee",
        "forbid_text": ["which mohit", "mohit sharma", "mohitosh", "title"],
        "require_arg_contains": ("attendee_email", "yharsh499@gmail.com"),
    },
    {
        "name": "greeting_does_not_resurrect_stale_question",
        "why": "A bare 'hi' re-raised the old contact question from memory.",
        "history": [("user", "can we add harsh yadav in this meet as well?"),
                    ("assistant",
                     "which Mohit should I invite - Mohit Sharma Delhi or "
                     "Mohitosh Rait? also, what's the meeting title?")],
        "user": "hi",
        "expect_tool": None,
        "forbid_text": ["mohit", "title", "which"],
        "max_words": 30,
    },
    {
        "name": "no_invented_person",
        "why": "It substituted a name from an earlier meeting for the new one.",
        "history": [("user", "meeting with Mohit Puns tomorrow 8 PM, google meet"),
                    ("assistant", BOOKED)],
        "user": "also invite priya to that meeting",
        "expect_tool": "calendar_add_attendee",
        "forbid_text": ["mohit sharma", "mohitosh", "which mohit"],
        "require_arg_contains": ("attendee_name", "priya"),
    },
    {
        "name": "new_meeting_still_routes_to_set_gmeet",
        "why": "Guard against over-correcting: a genuine booking must not "
               "become an add-attendee call.",
        "history": [],
        "user": "set up a call with Priya tomorrow at 3 pm",
        "expect_tool": "set_gmeet",
    },
    {
        "name": "no_success_claim_before_tool_result",
        "why": "Announcing a booking that hasn't happened is the worst lie "
               "a scheduling bot can tell.",
        "history": [],
        "user": "book a meeting with priya tomorrow 4 pm",
        "expect_tool": "set_gmeet",
        # If it answers in text instead of calling the tool, it must not
        # pretend the meeting exists.
        "forbid_text": ["i've scheduled", "i have scheduled", "booked it",
                        "is scheduled", "done —", "meeting is set"],
    },
    {
        "name": "add_guest_phrasing_also_as_attendee",
        "why": "Real user message. The old regex gate sent this to gmail_search "
               "because 'gmail' appears inside the address.",
        "history": [("user", "meeting with Akshay Bhaiya today 7 PM, google meet"),
                    ("assistant", "saved. Meeting with Akshay Bhaiya.\n"
                                  "Mon Jul 27, 07:00 PM to 07:30 PM IST")],
        "user": "Add yharsh499@gmail.com also as attendee",
        "expect_tool": "calendar_add_attendee",
        "forbid_text": ["when should", "what time", "date and time"],
        "require_arg_contains": ("attendee_email", "yharsh499@gmail.com"),
    },
    {
        "name": "add_guest_bare_phrasing",
        "why": "'add X as attendee' got NO tools at all under the regex gate.",
        "history": [("user", "meeting with Akshay Bhaiya today 7 PM, google meet"),
                    ("assistant", "saved. Meeting with Akshay Bhaiya.")],
        "user": "add rahul also",
        "expect_tool": "calendar_add_attendee",
        "forbid_text": ["when should", "what time"],
    },
    {
        "name": "title_from_chat_is_used",
        "why": "The user's typed topic was being dropped and replaced with "
               "'Meeting with <name>'.",
        "history": [],
        "user": "set up a meeting with priya tomorrow 4 pm to discuss q4 pricing",
        "expect_tool": "set_gmeet",
        "require_arg_contains": ("title", "pricing"),
    },
    {
        "name": "states_a_real_limitation",
        "why": "New capabilities section: it should decline what it can't do "
               "instead of inventing a flow.",
        "history": [("user", "meeting with Mohit Puns tomorrow 8 PM, google meet"),
                    ("assistant", BOOKED)],
        "user": "can you remove mohit from that meeting?",
        "expect_tool": None,
        "require_any": ["can't", "cannot", "can not", "not able", "don't support",
                        "do not support", "no way to", "unable"],
    },
]


# ── Runner ──────────────────────────────────────────────────────

def _build_request(case: dict):
    """Rebuild the exact system prompt + tool scope production would use."""
    selected = agent._select_tools_for_text(case["user"])
    enabled = ", ".join(t["function"]["name"] for t in selected) \
        or "none — ask a short clarifying question"

    system = agent.SYSTEM_PROMPT.format(
        time=now_prompt_str(),
        active_flow_context="\n### Live state (from DB — trust this, not memory)"
                            "\n- Notes saved: 0\n- Active reminders: 0",
        google_connected="yes",
        user_name=USER_NAME,
        timezone=USER_TZ,
        enabled_tools=enabled,
    )
    messages = [{"role": r, "content": c} for r, c in case["history"]]
    messages.append({"role": "user", "content": case["user"]})
    return system, messages, selected


def _call(system: str, messages: list, tools: list, user_text: str):
    """Go through the production `_call_groq`, not the raw client.

    That way the eval inherits the real temperature, token cap, tool scoping
    and retry behaviour — including the empty-completion retry — instead of
    testing a parallel code path that could drift from production.
    """
    state = agent._ToolRequestState(user_text=user_text)
    state.allowed_tool_names = {t["function"]["name"] for t in tools}
    token = agent._tool_request_state.set(state)
    try:
        resp = agent._call_groq({"role": "system", "content": system}, messages)
    finally:
        agent._tool_request_state.reset(token)

    msg = resp.choices[0].message
    calls = [(c.function.name, c.function.arguments or "")
             for c in (msg.tool_calls or [])]
    return calls, (msg.content or "")


def _check(case: dict, calls: list, text: str) -> list:
    """Return a list of failure strings ([] means the sample passed)."""
    fails = []
    names = [n for n, _ in calls]
    want = case.get("expect_tool")

    # Sarvam intermittently returns nothing at all, usually when no tool is
    # enabled. Production degrades safely (see the empty-completion guard in
    # agent.process_message), so flag it as a model no-op rather than a wrong
    # answer — the two need different fixes.
    if not text.strip() and not names:
        return ["MODEL NO-OP: empty completion (handled safely in production, "
                "but the model declined to answer)"]

    if want and want not in names:
        fails.append(f"expected tool {want!r}, got {names or 'no tool call'}")
    if want is None and names:
        fails.append(f"expected NO tool call, got {names}")

    # Models emit typographic quotes ("can’t"); checks are written with ASCII.
    low = text.lower().replace("’", "'").replace("‘", "'")
    for bad in case.get("forbid_text", []):
        # Only judge prose the user would actually see.
        if bad in low:
            fails.append(f"reply contains forbidden {bad!r}")

    req = case.get("require_any")
    if req and not any(r in low for r in req):
        fails.append(f"reply missing any of {req}: {text[:90]!r}")

    arg_req = case.get("require_arg_contains")
    if arg_req:
        key, needle = arg_req
        blob = " ".join(a for n, a in calls if n == want).lower()
        if needle.lower() not in blob:
            fails.append(f"tool arg {key} should contain {needle!r}, got {blob[:90]!r}")

    mw = case.get("max_words")
    if mw and len(text.split()) > mw:
        fails.append(f"reply too long ({len(text.split())} words > {mw})")

    return fails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--samples", type=int, default=1,
                    help="samples per case (surfaces flakiness)")
    ap.add_argument("-k", "--filter", default="",
                    help="only run cases whose name contains this")
    args = ap.parse_args()

    if not config.SARVAM_API_KEY or "your_" in str(config.SARVAM_API_KEY):
        print(f"{RED}SARVAM_API_KEY is not set — this harness needs the live "
              f"model.{RESET}\nSet it in .env, or run the mocked suite instead: "
              f"python -m tester.run")
        sys.exit(2)

    cases = [c for c in CASES if args.filter in c["name"]]
    print(f"\n{BOLD}araka — real-model prompt eval{RESET}  "
          f"{DIM}model={agent.MODEL} samples={args.samples}{RESET}")
    print("─" * 64)

    total = passed = 0
    details = []
    for case in cases:
        system, messages, tools = _build_request(case)
        ok_n = 0
        case_fails = []
        for _ in range(args.samples):
            total += 1
            try:
                calls, text = _call(system, messages, tools, case["user"])
            except Exception as e:  # noqa: BLE001
                case_fails.append(f"API error: {type(e).__name__}: {e}")
                continue
            fails = _check(case, calls, text)
            if fails:
                case_fails.append(f"{'; '.join(fails)}\n{DIM}reply: {text[:150]!r}{RESET}")
            else:
                ok_n += 1
                passed += 1

        mark = (f"{GREEN}✓ PASS{RESET}" if ok_n == args.samples
                else f"{YELLOW}~ FLAKY{RESET}" if ok_n
                else f"{RED}✗ FAIL{RESET}")
        print(f"  {mark}  {case['name']}  {DIM}{ok_n}/{args.samples}{RESET}")
        if case_fails:
            details.append((case, case_fails))

    print("─" * 64)
    color = GREEN if passed == total else RED
    print(f"  {color}{BOLD}{passed}/{total} samples passed{RESET}\n")

    for case, fails in details:
        print(f"{RED}── {case['name']} ──{RESET}")
        print(f"{DIM}why: {case['why']}{RESET}")
        print(f"{DIM}user: {case['user']!r}{RESET}")
        for f in fails:
            print(f"   • {f}")
        print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
