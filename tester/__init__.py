"""PRD user-simulation test suite for FollowUp Bot.

Importing this package configures an ISOLATED, throwaway SQLite database
and dummy credentials *before* any `bot.*` module is imported — so the
real `.env`, real Groq key and real WhatsApp token are never touched.

Run it with:
    python -m tester.run
"""

import os
import tempfile

# Isolated test DB (created fresh each run). Set BEFORE bot.* imports so
# the SQLAlchemy engine binds to it instead of the production notebot.db.
_TEST_DB = os.environ.get("FOLLOWUP_TEST_DB") or tempfile.mktemp(
    prefix="followup_test_", suffix=".db"
)
os.environ["FOLLOWUP_TEST_DB"] = _TEST_DB
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + _TEST_DB

# Dummy config so bot.config import + validate() never need real secrets.
# load_dotenv(override=False) won't clobber these.
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("WA_ACCESS_TOKEN", "test-token")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "test-phone")
os.environ.setdefault("WA_VERIFY_TOKEN", "test-verify")
os.environ.setdefault("REQUIRE_WA_SIGNATURE", "false")
os.environ.setdefault("BASE_URL", "https://test.example.com")
