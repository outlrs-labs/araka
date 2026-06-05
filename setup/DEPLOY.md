# Deploying FollowUp Bot (always-on, no local machine)

Your bot has two hard hosting requirements, both because of how it works:

1. **Always-on** — it runs an in-process reminder scheduler. A host that
   "sleeps" (Render free, Vercel, Cloud Run scale-to-zero) would stop firing
   reminders.
2. **Persistent disk** — it stores users, tasks and Google tokens in SQLite.
   A host with an ephemeral filesystem wipes everything on every redeploy.

And one rule: **run a single process** (`gunicorn -w 1`). Two copies = every
reminder fires twice. The included `wsgi.py` + `Dockerfile` already enforce this.

---

## Option A — Fly.io (recommended: free `*.fly.dev` HTTPS, ~10 min)

Fly gives you a stable HTTPS URL automatically (no domain/cert work) and a
persistent volume. At this size it sits within the free allowance (a card is
required for verification).

```bash
# 1. Install + log in
curl -L https://fly.io/install.sh | sh
fly auth signup        # or: fly auth login

# 2. Create the app (edit the name in fly.toml first if you like)
fly launch --no-deploy --copy-config --name <your-unique-name>

# 3. Persistent disk for SQLite (1 GB is plenty for 100 users)
fly volumes create followup_data --size 1 --region bom

# 4. Secrets (NEVER bake these into the image)
fly secrets set \
  WA_PHONE_NUMBER_ID=... \
  WA_ACCESS_TOKEN=... \
  WA_VERIFY_TOKEN=... \
  WA_APP_SECRET=... \
  GROQ_API_KEY=... \
  GOOGLE_TOKEN_ENCRYPTION_KEY=... \
  FLASK_SECRET=...

# 5. Deploy
fly deploy

# 6. Tell the bot its own URL, then redeploy once
fly secrets set BASE_URL=https://<your-unique-name>.fly.dev
```

**credentials.json** (Google OAuth client): simplest is to leave it in the
build (it's copied by the Dockerfile). To keep it out of the image instead,
add `credentials.json` to `.dockerignore` and inject it as a secret:

```bash
fly secrets set GOOGLE_CREDENTIALS_B64="$(base64 -i credentials.json)"
```
…then write it at boot (ask me to add a 3-line decode step to `wsgi.py`).

Logs / status:
```bash
fly logs
fly status
```

---

## Option B — Oracle Cloud "Always Free" (truly $0 forever)

A real VM that runs 24/7 for free (ARM Ampere: up to 4 vCPU / 24 GB). More
setup, and you need a domain for a stable HTTPS webhook URL.

```bash
# On the VM (Ubuntu):
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo> followup-bot && cd followup-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

cp setup/.env.example .env && nano .env        # fill in real values
#   DATABASE_URL=sqlite+aiosqlite:///notebot.db   (default is fine on a VM)
#   BASE_URL=https://your-domain                  (see TLS below)
# put credentials.json in this folder

# Run it under systemd (auto-restart, starts on boot):
sudo cp setup/followup-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now followup-bot
journalctl -u followup-bot -f
```

**HTTPS** (pick one):
- **Caddy + a free domain** (e.g. DuckDNS): `sudo apt install caddy`, then a
  2-line Caddyfile `your-domain { reverse_proxy localhost:5000 }` → automatic
  Let's Encrypt TLS.
- **Cloudflare Tunnel** (if your domain is on Cloudflare): no open ports, free
  TLS, `cloudflared tunnel ...` → maps a hostname to `localhost:5000`.

Open port 443 (or 80) in the Oracle security list if you use Caddy directly.

---

## Option C — AWS EC2 or Google Compute Engine (a VM you manage)

Same shape as Oracle: one small **always-on VM** + SQLite on its disk + the
`followup-bot.service` systemd unit + Caddy/Cloudflare for HTTPS. Pick this if
you're already in the AWS/GCP ecosystem.

> **You do NOT need RDS / Cloud SQL at this scale.** See the note at the end.

| Provider | Always-on VM free? | Notes |
|---|---|---|
| **Oracle** | ✅ forever | ARM 4 vCPU / 24 GB — most generous |
| **GCP** | ✅ forever, but tiny | 1× e2-micro (~1 GB shared), US regions only |
| **AWS** | ⚠️ 12 months only | t3.micro 750 h/mo for 1 year, then ~$8–10/mo |

### Google Compute Engine (Always Free e2-micro)
```bash
# VM must be in us-west1 / us-central1 / us-east1 for the free tier
gcloud compute instances create followup-bot \
  --machine-type=e2-micro --zone=us-central1-a \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --tags=http-server,https-server

gcloud compute firewall-rules create allow-web \
  --allow=tcp:80,tcp:443 --target-tags=http-server,https-server

gcloud compute ssh followup-bot --zone=us-central1-a
# …then run the app-setup + systemd steps from Option B above.
```

### AWS EC2 (free for 12 months)
1. EC2 → Launch instance → **Ubuntu 22.04**, **t3.micro** (free-tier eligible), 30 GB gp3.
2. Security group: allow inbound **22** (SSH), **80**, **443**.
3. `ssh ubuntu@<public-ip>` → run the app-setup + systemd steps from Option B.

On the 1 GB micro VMs (GCP/AWS), add a swap file so installs/LLM calls don't OOM:
```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
HTTPS: same Caddy-or-Cloudflare-Tunnel choice as Option B.

### "Should I use managed Postgres (RDS / Cloud SQL)?"
**Not yet.** At ~100 users, SQLite-on-the-VM is simpler, faster (no network
hop), and free. Managed Postgres adds ~$9–15/mo and only pays off once you run
**multiple app instances** — which also requires externalising the scheduler
(it currently runs in-process). The code is SQLAlchemy-based so it's portable,
but the lightweight auto-migration uses a little SQLite-specific DDL
(`BOOLEAN DEFAULT 0`, etc.), so switching is ~an hour of cleanup (or an Alembic
setup), **not** a one-line `DATABASE_URL` change. Do it when you actually
outgrow one box, not before.

---

## After deploying (any option)

1. **Meta webhook** → set Callback URL to `https://<your-url>/webhook`, verify
   token = your `WA_VERIFY_TOKEN`, subscribe to `messages`. (See
   `META_TEMPLATE_SETUP.md`.)
2. **Google OAuth** → no domain needed. The bot uses the **paste flow**, so
   just ensure `http://localhost` is in the OAuth client's Authorized redirect
   URIs (it is, for a Desktop/Installed client). Users tap the Google link,
   sign in, and paste the resulting `http://localhost/?code=...` URL back into
   chat. `BASE_URL` can stay blank. (To switch to a domain auto-callback later,
   add `https://<your-url>/auth/callback` here and point `_get_redirect_uri()`
   at it.)
3. Smoke test:
   - `https://<your-url>/health` → `{"status":"ok"}`
   - Message the bot on WhatsApp → onboarding starts; the privacy line links to
     your Google Doc.

## Backups

Your whole database is one file. Back it up periodically:
- **Fly:** `fly ssh console -C "cat /data/notebot.db" > backup.db` (or snapshot the volume).
- **Oracle:** `cp notebot.db ~/backups/notebot-$(date +%F).db` via cron.

## When to graduate off this setup

This single-box design is ideal up to a few hundred users. Beyond that, move
SQLite → managed Postgres and the in-process scheduler → an external scheduler
(cron/queue), which lets you run multiple stateless workers. Not needed now.
