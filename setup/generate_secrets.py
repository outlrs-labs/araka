#!/usr/bin/env python3
"""Generate the secret values needed in .env.

Usage:
    python setup/generate_secrets.py

Prints a ready-to-paste block. Re-run any time you need to ROTATE a
credential (rotating the verify token / app secret is the Week-1
security task).
"""

import secrets


def main() -> None:
    verify_token = secrets.token_urlsafe(32)
    flask_secret = secrets.token_urlsafe(32)

    try:
        from cryptography.fernet import Fernet
        fernet_key = Fernet.generate_key().decode()
    except ImportError:
        fernet_key = "(install requirements first: pip install cryptography)"

    print("\n# ── Generated secrets — paste into your .env ──\n")
    print(f"WA_VERIFY_TOKEN={verify_token}")
    print(f"FLASK_SECRET={flask_secret}")
    print(f"GOOGLE_TOKEN_ENCRYPTION_KEY={fernet_key}")
    print("\n# Note: WA_ACCESS_TOKEN and WA_APP_SECRET come from the Meta")
    print("# App dashboard — they are NOT generated here.\n")


if __name__ == "__main__":
    main()
