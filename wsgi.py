"""Production WSGI entrypoint for FollowUp Bot.

Run with a SINGLE worker so exactly one scheduler exists:

    gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:5000 wsgi:application

Why one worker (critical): the bot runs an in-process APScheduler + a
background asyncio loop. Two workers would each start their own scheduler
and fire every reminder twice. Do NOT use --preload (the scheduler must
start inside the worker, not the pre-fork master).

The app's __main__ block (DB init, scheduler start, ngrok auto-detect) is
NOT run by gunicorn, so we reproduce the needed startup here.
"""

import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("wsgi")

from bot.config import config           # noqa: E402
from bot.database import init_db        # noqa: E402
from bot.main import app, run_async, start_jobs   # noqa: E402  (importing starts the bg loop)

# Surface bad/placeholder config loudly — but don't crash-loop the host.
for err in config.validate():
    log.error(f"CONFIG PROBLEM: {err}")

if not config.BASE_URL:
    log.warning("BASE_URL is not set — OAuth callback + privacy link need your public https URL.")

log.info("Initialising database…")
run_async(init_db())

log.info("Starting background scheduler…")
start_jobs()

log.info("FollowUp Bot ready (production WSGI).")

# gunicorn looks for `application`
application = app
