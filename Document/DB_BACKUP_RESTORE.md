# Database backup & restore (araka)

The bot's entire state — users, Google tokens, reminders, meetings, notes — is
one SQLite file: `~/followup-bot/notebot.db`. This is how it's protected and
how to bring it back.

## How backups work

- **Script:** `setup/backup.sh`, run daily at 2 AM by cron.
- **Method:** SQLite **online `.backup`** (a consistent snapshot — safe to take
  while the bot is running, includes WAL writes). *Not* `cp`, which can capture
  a torn/stale file.
- **Integrity:** every snapshot is `PRAGMA integrity_check`-ed; a bad one is
  discarded, not kept.
- **Compressed:** `.gz`.
- **Encrypted:** if `~/followup-bot/.backup_key` exists, AES-256 (`.gz.enc`).
- **Perms:** files `600`, dir `700`.
- **Retention:** 14 days, auto-pruned.
- **Location:** `~/backups/notebot-YYYY-MM-DD_HHMMSS.db.gz[.enc]`
- **At rest:** the live DB and backups sit on an OCI block volume that is
  AES-256 encrypted at rest by default.

## Enable backup encryption (recommended before any off-box copy)

```bash
# one-time: generate a 32-byte key, lock it down
openssl rand -base64 32 > ~/followup-bot/.backup_key
chmod 600 ~/followup-bot/.backup_key
```
Keep a copy of this key somewhere safe and OFF the VM — without it, encrypted
backups cannot be restored.

## Restore (full procedure)

```bash
# 1. Stop the bot so nothing writes mid-restore.
sudo systemctl stop followup-bot followup-reminder.timer

cd ~/followup-bot
BK=~/backups/notebot-YYYY-MM-DD_HHMMSS.db.gz       # pick the backup to restore

# 2. Decrypt (only if the file ends in .enc).
#    openssl enc -d -aes-256-cbc -pbkdf2 -in "$BK.enc" -out "$BK" -pass file:.backup_key

# 3. Decompress to a temp file.
gunzip -c "$BK" > /tmp/restore.db

# 4. VERIFY before overwriting anything.
sqlite3 /tmp/restore.db 'PRAGMA integrity_check;'   # must print: ok
sqlite3 /tmp/restore.db 'SELECT count(*) FROM users;'

# 5. Swap in (keep the current file as .pre-restore just in case).
mv notebot.db notebot.db.pre-restore 2>/dev/null || true
rm -f notebot.db-wal notebot.db-shm                 # clear stale WAL
mv /tmp/restore.db notebot.db

# 6. Restart.
sudo systemctl start followup-bot followup-reminder.timer
curl -s http://127.0.0.1:5000/health
```

## Off-box copy (survives VM loss) — optional, free

On-VM backups protect against accidents/bad migrations but **not a dead VM**.
To survive that, push the daily encrypted backup off the box, e.g. OCI Object
Storage (Always-Free tier). After configuring the OCI CLI + a bucket, add to
the end of `backup.sh`:

```bash
oci os object put -bn araka-backups --file "$OUT" --force >/dev/null 2>&1 || \
    echo "$(date -Is) off-box upload failed" >&2
```

## Quick health checks

```bash
ls -la ~/backups/                                   # recent backups, 600 perms
tail ~/backups/backup.log                           # last run result
sqlite3 ~/followup-bot/notebot.db 'PRAGMA integrity_check;'   # live DB ok?
```
