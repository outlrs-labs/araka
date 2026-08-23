"""Run the PRD user-simulation suite.

    python -m tester.run

Exits 0 if all scenarios pass, 1 otherwise. Uses an isolated temp DB and
fully mocked WhatsApp / Google / Groq — safe to run anywhere, no secrets.
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from tester.harness import Simulator, install_mocks, setup_db, reset_rate_limit
from tester import scenarios

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


async def _run() -> bool:
    await setup_db()
    sim = Simulator()
    install_mocks(sim)

    results = []
    for name, fn in scenarios.ALL:
        # Scenarios share one wa_id, so without this the per-user rate limit
        # accumulates across the suite and later scenarios get throttled —
        # a test-isolation artifact, not a product failure.
        reset_rate_limit()
        try:
            await fn(sim)
            results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e)))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    print(f"\n{BOLD}FollowUp Bot — PRD user simulation{RESET}")
    print("─" * 52)
    passed = 0
    for name, ok, detail in results:
        if ok:
            passed += 1
            print(f"  {GREEN}✓ PASS{RESET}  {name}")
        else:
            print(f"  {RED}✗ FAIL{RESET}  {name}")
            first = detail.strip().splitlines()[0] if detail.strip() else ""
            print(f"         {DIM}{first}{RESET}")
    print("─" * 52)
    total = len(results)
    color = GREEN if passed == total else RED
    print(f"  {color}{BOLD}{passed}/{total} scenarios passed{RESET}\n")

    # Print full tracebacks for failures (after the summary).
    for name, ok, detail in results:
        if not ok and "Traceback" in detail:
            print(f"{RED}── {name} ──{RESET}\n{detail}")

    return passed == total


def main() -> None:
    ok = asyncio.run(_run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
