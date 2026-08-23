# Oracle Cloud (Ubuntu) — Production Deployment (free, ad-free)

Hardened, $0 deployment of FollowUp Bot on an Oracle Cloud "Always Free"
**Ubuntu** instance. No paid services, no expiring trials, no ad-supported
tunnels. Free *forever*: Always Free VM + Caddy + Let's Encrypt + sslip.io.

> **Ubuntu variant** — login user `ubuntu`, package manager `apt`, firewall
> `ufw`. Your instance IP: **`140.245.193.44`**.

**Architecture**
```
WhatsApp / Google ──HTTPS:443──> Caddy (auto Let's Encrypt cert, auto-renew)
                                      │  reverse_proxy 127.0.0.1:5000
                                      ▼
                                gunicorn -w1  (bound to loopback only)
                                      │
                          FollowUp Bot  +  SQLite on disk  +  APScheduler
```
Production properties: auto-restart on crash, auto-start on reboot,
auto-renewing TLS, automatic OS security patches, SSH brute-force protection,
the app port never exposed, nightly DB backups, health watchdog.

Commands are labelled **🌐 console** / **💻 Mac** / **🖥️ VM**. Fill in only
`<KEY_FILE>` (your downloaded private key, e.g. `ssh-key-2026-06-02.key`).

---

## 0. Concepts (60 seconds)

- **Public IP** — your VM's internet address (`140.245.193.44`). Find it:
  Console → Compute → Instances → your instance, or `curl ifconfig.me` on the VM.
- **Ephemeral vs Reserved** — ephemeral changes if you recreate the VM;
  **reserved is static**. Reserve it (Part 3) so the webhook URL is permanent.
- **Two firewalls** — Oracle blocks ports in the *cloud* (VCN Security List)
  **and** in the *VM* (`ufw`). Both must allow 80/443 (Part 4).
- **No domain needed** — `sslip.io` turns your IP into a hostname
  `140-245-193-44.sslip.io` with zero signup, so Let's Encrypt can issue a cert.

---

## 1. Account + region
oracle.com/cloud/free → card for identity check only. Home region near you
(permanent).

## 2. Create the VM

🌐 Console → Compute → Instances → **Create Instance**:
- Image: **Canonical Ubuntu 22.04** (or 24.04)
- Shape: `VM.Standard.A1.Flex` (1 OCPU / 6 GB) — ARM, most generous.
  If "out of capacity": use `VM.Standard.E2.1.Micro` (1 GB, x86, always
  available — add the 2 GB swap in Part 5).
- SSH key: upload your public key (or paste it).
- Public IPv4: **Yes**.

## 3. Reserve a static public IP (production)
🌐 Console → your instance → *Resources → Attached VNICs* → primary VNIC →
*IPv4 Addresses* → the public IP `140.245.193.44` → **Edit → Reserved**. Now
it never changes.

## 4. Open the firewall — BOTH layers

### 4a. 🌐 Cloud (VCN Security List)
Console → Networking → Virtual Cloud Networks → your VCN → **Security Lists →
Default → Add Ingress Rules**. Add two — Source `0.0.0.0/0`, TCP, port **80**;
again port **443**.

### 4b. 🖥️ VM (ufw) — run after first SSH (Part 5)
```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status
```
> We never open 5000 — the app binds to loopback only (Part 7).

## 5. First login + OS hardening

💻 **Mac** — connect as `ubuntu`:
```bash
chmod 600 ~/Downloads/<KEY_FILE>
ssh -i ~/Downloads/<KEY_FILE> ubuntu@140.245.193.44
```

🖥️ **VM** — patch, harden:
```bash
export IP=140.245.193.44

# Patches + runtime + tools
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git rsync

# SSH brute-force protection
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban

# Automatic security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # answer Yes

# Sane log timezone + cap journal size
sudo timedatectl set-timezone Asia/Kolkata
sudo sed -i 's/#SystemMaxUse=/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

# Swap — harmless on A1.Flex; REQUIRED on E2.1.Micro (1 GB)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# VM firewall (from 4b)
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

## 6. Upload the app

💻 **Mac** (new terminal) — sends code + your `.env` + `credentials.json`,
skips venv/DB:
```bash
cd "/Users/harshyadav/Documents/Bot tele copy"
rsync -avz \
  --exclude 'venv' --exclude '*.db' --exclude '*.db-*' \
  --exclude '__pycache__' --exclude '.git' --exclude '.DS_Store' \
  -e "ssh -i ~/Downloads/<KEY_FILE>" \
  ./ ubuntu@140.245.193.44:/home/ubuntu/followup-bot/
```

## 7. Install + run as a hardened service

🖥️ **VM**:
```bash
cd ~/followup-bot
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt

# Verify secrets are real (not placeholders)
./venv/bin/python - <<'PY'
from bot.config import config
e = config.validate()
print("\n".join(e) if e else "Config OK ✅")
PY
# If it lists anything: nano .env → fix → re-run. Keep BASE_URL= blank.

# Install systemd unit (already written for ubuntu user + loopback bind)
sudo cp setup/followup-bot.service /etc/systemd/system/
# Ensure loopback-only bind (safe to run even if already correct)
sudo sed -i 's/0.0.0.0:5000/127.0.0.1:5000/' /etc/systemd/system/followup-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now followup-bot
systemctl status followup-bot --no-pager
```
The app now listens on `127.0.0.1:5000` — not reachable from the internet
directly. Logs: `journalctl -u followup-bot -f`.

## 8. HTTPS (Caddy + sslip.io — no account, auto-renew)

🖥️ **VM**:
```bash
# Caddy via official apt repo
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Config from your IP, then start
export HOST="${IP//./-}.sslip.io"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${HOST} {
    reverse_proxy 127.0.0.1:5000
}
EOF
sudo systemctl enable --now caddy
sudo systemctl restart caddy
sleep 25
echo "PUBLIC URL → https://${HOST}"
curl -s https://${HOST}/health
```
Expect `{"status":"ok",...}`. For your IP the URL is
**`https://140-245-193-44.sslip.io`**. Caddy renews the cert forever.

## 9. Production hygiene — backups + watchdog

🖥️ **VM**:
```bash
# Nightly DB backup at 02:00, keep 14 days
mkdir -p ~/backups
( crontab -l 2>/dev/null; echo "0 2 * * * cp ~/followup-bot/notebot.db ~/backups/notebot-\$(date +\%F).db && find ~/backups -name 'notebot-*.db' -mtime +14 -delete" ) | crontab -

# Health watchdog — restart if the app stops answering
echo "*/5 * * * * root curl -fsS http://127.0.0.1:5000/health >/dev/null || systemctl restart followup-bot" | sudo tee /etc/cron.d/followup-watchdog
```
systemd already restarts on crash + reboot; the watchdog adds "hung but alive"
recovery.

## 10. Connect WhatsApp + Google

1. 🌐 Meta dashboard → WhatsApp → Configuration:
   - Callback URL: `https://140-245-193-44.sslip.io/webhook`
   - Verify token: your `WA_VERIFY_TOKEN`
   - Subscribe to **messages**.
2. **Google:** paste flow — keep `http://localhost` in the OAuth client's
   redirect URIs. `BASE_URL` stays blank.

## 11. Verify
- `https://140-245-193-44.sslip.io/health` → ok.
- WhatsApp → bot onboards.
- `connect` → tap Google link → sign in → paste `http://localhost/?code=...`
  back → connected.
- `Set a gmeet with <contact> tomorrow 4 pm` → confirm → Yes → saved.

## 12. Day-2 operations
```bash
journalctl -u followup-bot -f                 # logs
sudo systemctl restart followup-bot           # after a config change
# deploy new code: rerun the rsync from Part 6, then:
cd ~/followup-bot && ./venv/bin/pip install -r requirements.txt && sudo systemctl restart followup-bot
```

---

## Troubleshooting (Ubuntu)

| Symptom | Fix |
|---|---|
| `/health` over HTTPS hangs | A port isn't open — recheck **both** firewalls (Part 4). `sudo ufw status` should show 80,443 ALLOW. |
| `ufw` blocks port 80/443 after reboot | Re-run `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw reload`. |
| Caddy can't reach the app | `curl http://127.0.0.1:5000/health` on the VM — if that fails, check `systemctl status followup-bot`. |
| Caddy won't get a cert | Reserved IP must match the sslip host; 80 **and** 443 open in VCN + ufw; `journalctl -u caddy -f`. If sslip.io is rate-limited, use a free DuckDNS subdomain (ask me). |
| `apt install caddy` fails | Run the full Caddy apt repo setup from Part 8 — Ubuntu's default repos don't include Caddy. |
| Meta "verification failed" | `WA_VERIFY_TOKEN` must match Meta exactly; check bot is running (`systemctl status followup-bot`). |
| Bot exits on start | `journalctl -u followup-bot -e` → prints `CONFIG PROBLEM: …` for any placeholder/missing key. |
| OOM crash on E2.1.Micro | Confirm swap is active: `free -h` should show 2G swap. If not, re-run the swap commands from Part 5. |

## Security checklist (all done above)
- [x] App bound to `127.0.0.1` — not internet-reachable directly
- [x] Only 22/80/443 open (`ufw` + VCN); TLS at Caddy with auto-renew
- [x] Automatic OS security patches (`unattended-upgrades`)
- [x] SSH key-only + `fail2ban`
- [x] Webhook signature verification on (`REQUIRE_WA_SIGNATURE=true`)
- [x] Google tokens encrypted at rest (`GOOGLE_TOKEN_ENCRYPTION_KEY` set)
- [x] Nightly backups + log cap + health watchdog
