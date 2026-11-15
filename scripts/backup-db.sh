#!/usr/bin/env bash
# Nightly SQLite backup of the TeamBots database (online, WAL-safe).
#
#   scripts/backup-db.sh [DEST_DIR]
#
# Environment:
#   TB_DATA_DIR       directory that holds teambots.db   (default: ./data)
#   TB_DB_FILE        full path of the database          (default: $TB_DATA_DIR/teambots.db)
#   TB_BACKUP_DIR     destination root                   (default: ./backups)
#   TB_BACKUP_KEEP    number of dated snapshots to keep  (default: 14)
#
# Safe to run while the app is live. Uses SQLite's online backup API when
# Python is available, otherwise `sqlite3 .backup`. Does not copy .env or
# Docker volumes; use scripts/backup.sh for a full snapshot.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DATA_DIR="${TB_DATA_DIR:-$ROOT/data}"
DB="${TB_DB_FILE:-$DATA_DIR/teambots.db}"
DEST="${1:-${TB_BACKUP_DIR:-$ROOT/backups}}"
KEEP="${TB_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"
mkdir -p "$OUT"

if [ ! -f "$DB" ]; then
  echo "[backup-db] no database at $DB (skipped)"
  rmdir "$OUT" 2>/dev/null || true
  exit 0
fi

echo "[backup-db] $DB -> $OUT/teambots.db"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$DB" "$OUT/teambots.db" <<'ENDPY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
s.close()
d.close()
ENDPY
elif command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$OUT/teambots.db'"
else
  echo "[backup-db] need python3 or sqlite3" >&2
  exit 1
fi

{
  echo "created=$STAMP"
  echo "host=$(hostname)"
  echo "source=$DB"
  echo "bytes=$(wc -c < "$OUT/teambots.db" | tr -d ' ')"
} > "$OUT/MANIFEST.txt"

# Prune older snapshots; keep the most recent $KEEP folders.
ls -1dt "$DEST"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -rf

echo "[backup-db] done; retaining the last $KEEP snapshots in $DEST"
