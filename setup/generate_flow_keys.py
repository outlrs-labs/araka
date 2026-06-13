"""Generate the RSA keypair for the WhatsApp Flow endpoint, and (optionally)
upload the public key to Meta.

Usage:
    # 1) generate keys (writes flow_private.pem + flow_public.pem in CWD)
    python setup/generate_flow_keys.py

    # 2) upload the public key to your phone number (reads .env for creds)
    python setup/generate_flow_keys.py --upload

Meta signs the public key; the matching private key stays on your server and
is referenced by FLOW_PRIVATE_KEY_PATH. Set a passphrase with FLOW_KEY_PASSPHRASE
(then pass --passphrase here to match) for an encrypted private key.
"""

import argparse
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate(passphrase: str = ""):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (serialization.BestAvailableEncryption(passphrase.encode())
           if passphrase else serialization.NoEncryption())
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    )
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with open("flow_private.pem", "wb") as f:
        f.write(priv_pem)
    with open("flow_public.pem", "wb") as f:
        f.write(pub_pem)
    print("Wrote flow_private.pem and flow_public.pem")
    print("\nSet in .env:")
    print("  FLOW_PRIVATE_KEY_PATH=flow_private.pem")
    if passphrase:
        print("  FLOW_KEY_PASSPHRASE=<the passphrase you used>")
    print("\n--- PUBLIC KEY (upload this to Meta) ---")
    print(pub_pem.decode())


def upload(passphrase: str = ""):
    import requests
    sys.path.insert(0, os.getcwd())
    from bot.config import config

    if not os.path.exists("flow_public.pem"):
        print("flow_public.pem not found — run without --upload first.")
        sys.exit(1)
    with open("flow_public.pem") as f:
        pub = f.read()

    url = (f"https://graph.facebook.com/{config.WA_API_VERSION}"
           f"/{config.WA_PHONE_NUMBER_ID}/whatsapp_business_encryption")
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"},
        data={"business_public_key": pub},
        timeout=20,
    )
    print(f"Upload status {resp.status_code}: {resp.text}")
    if resp.ok:
        print("\nVerify it registered:")
        print(f"  curl '{url}' -H 'Authorization: Bearer <TOKEN>'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true", help="upload flow_public.pem to Meta")
    ap.add_argument("--passphrase", default=os.getenv("FLOW_KEY_PASSPHRASE", ""))
    args = ap.parse_args()
    if args.upload:
        upload(args.passphrase)
    else:
        generate(args.passphrase)
