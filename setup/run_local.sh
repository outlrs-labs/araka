#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# FollowUp Bot — local run helper
#   ./setup/run_local.sh
# Starts the webhook server. Expose it with ngrok in another tab:
#   ngrok http 5000
# then put the https URL into .env as BASE_URL and in the Meta webhook.
# ════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."   # project root

# 1. venv
if [ ! -d "venv" ]; then
  echo "→ Creating virtualenv..."
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

# 2. deps
echo "→ Installing requirements..."
pip install -q -r requirements.txt

# 3. .env check
if [ ! -f ".env" ]; then
  echo "✗ No .env found. Run:  cp setup/.env.example .env  (then edit it)"
  echo "  Generate secrets with:  python setup/generate_secrets.py"
  exit 1
fi

# 4. credentials.json check
if [ ! -f "credentials.json" ]; then
  echo "⚠  credentials.json not found — Google features will be disabled until you add it."
fi

# 5. run
echo "→ Starting FollowUp Bot on port ${FLASK_PORT:-5000}..."
exec python -m bot.main


