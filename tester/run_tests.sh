#!/usr/bin/env bash
# Run the PRD user-simulation suite against the real bot handlers.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="venv/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" -m tester.run
