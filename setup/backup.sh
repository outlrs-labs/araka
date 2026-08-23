#!/usr/bin/env bash
#
# Safe, consistent SQLite backup for araka. Replaces the unsafe `cp notebot.db`
# (which can capture a torn/stale file because WAL writes live in -wal).
#
# Steps: online .backup snapshot -> integrity check -> gzip -> optional AES
# encryption (if a key file exists) -> lock perms -> prune old backups.
#
# Wire via cron (2 AM daily):
#   0 2 * * * /home/ubuntu/followup-bot/setup/backup.sh >> /home/ubuntu/backups/backup.log 2>&1
#
set -euo pipefail

DB="/home/ubuntu/followup-bot/notebot.db"
DIR="/home/ubuntu/backups"
KEYFILE="/home/ubuntu/followup-bot/.backup_key"   # optional: enables encryption
RETAIN_DAYS=14
STAMP="$(date +%F_%H%M%S)"
SNAP="$DIR/notebot-$STAMP.db"

mkdir -p "$DIR"
chmod 700 "$DIR"

# 1. Consistent snapshot via the online backup API (safe on a live WAL DB).
sqlite3 "$DB" ".backup '$SNAP'"

# 2. Integrity check — refuse to keep a corrupt backup.
RES="$(sqlite3 "$SNAP" 'PRAGMA integrity_check;' | head -1)"
if [ "$RES" != "ok" ]; then
    echo "$(date -Is) BACKUP INTEGRITY FAILED: $RES" >&2
    rm -f "$SNAP"
    exit 1
fi

# 3. Compress.
gzip -f "$SNAP"            # -> $SNAP.gz
OUT="$SNAP.gz"

# 4. Encrypt if a key is present (protects off-box / exfiltrated copies).
if [ -f "$KEYFILE" ]; then
    openssl enc -aes-256-cbc -pbkdf2 -salt \
        -in "$OUT" -out "$OUT.enc" -pass "file:$KEYFILE"
    rm -f "$OUT"
    OUT="$OUT.enc"
fi

# 5. Lock perms.
chmod 600 "$OUT"

# 6. Retention — prune every backup form older than RETAIN_DAYS.
find "$DIR" -name 'notebot-*.db*' -mtime +"$RETAIN_DAYS" -delete

echo "$(date -Is) backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
