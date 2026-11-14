#!/usr/bin/env bash
# Full TeamBots backup: the database, every project workspace, and the .env secret key.
#
# Safe to run while the app is live (the database is copied with SQLite's online-backup
# API, which handles WAL correctly). Cron-friendly and self-pruning.
#
#   scripts/backup.sh [DEST_DIR]        # default DEST_DIR = ./backups
#   TB_BACKUP_KEEP=30 scripts/backup.sh # keep the last 30 snapshots (default 14)
#
# Each run writes one timestamped folder under DEST_DIR containing:
#   teambots.db            the database (consistent snapshot)
#   env.backup             a copy of .env  -> holds TB_SECRET_KEY, without which the
#                          database's encrypted secrets are UNRECOVERABLE. Keep it safe.
#   volume-tb-*.tgz        one tarball per project/overview Docker volume
#   workspaces-local.tgz   local-backend workspaces, if that backend is in use
#   MANIFEST.txt           what this snapshot contains
#
# Restore with scripts/restore.sh <snapshot-dir>.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DEST="${1:-$ROOT/backups}"
DATA_DIR="${TB_DATA_DIR:-$ROOT/data}"
DB="${TB_DB_FILE:-$DATA_DIR/teambots.db}"
KEEP="${TB_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"
mkdir -p "$OUT"
echo "[backup] -> $OUT"

# 1) Database: online backup, safe under concurrent writes.
if [ -f "$DB" ]; then
  python3 - "$DB" "$OUT/teambots.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)          # consistent snapshot, WAL included
s.close(); d.close()
print("  db: ok")
PY
else
  echo "  db: NOT FOUND at $DB (skipped)"
fi

# 2) .env — the encryption key lives here. Lose it and encrypted secrets are gone.
if [ -f "$ROOT/.env" ]; then
  cp "$ROOT/.env" "$OUT/env.backup"
  chmod 600 "$OUT/env.backup"
  echo "  env: ok (contains TB_SECRET_KEY — store this snapshot securely)"
else
  echo "  env: no .env found (skipped)"
fi

# 3) Project workspaces.
#    Docker backend: one named volume per project (tb-p<id>) and per overview bot (tb-home-u<id>).
if command -v docker >/dev/null 2>&1; then
  for v in $(docker volume ls -q --filter name='tb-' 2>/dev/null || true); do
    if docker run --rm -v "$v":/v:ro -v "$OUT":/b alpine tar czf "/b/volume-$v.tgz" -C /v . 2>/dev/null; then
      echo "  volume $v: ok"
    else
      echo "  volume $v: FAILED"
    fi
  done
fi
#    Local backend: plain directories under data/workspaces.
if [ -d "$DATA_DIR/workspaces" ]; then
  tar czf "$OUT/workspaces-local.tgz" -C "$DATA_DIR" workspaces && echo "  workspaces(local): ok"
fi

# 4) Manifest.
{
  echo "created=$STAMP"
  echo "host=$(hostname)"
  echo "db=$([ -f "$OUT/teambots.db" ] && echo yes || echo no)"
  echo "env=$([ -f "$OUT/env.backup" ] && echo yes || echo no)"
  echo "volumes=$(ls "$OUT" 2>/dev/null | grep -c '^volume-' || true)"
} > "$OUT/MANIFEST.txt"

# 5) Prune: keep the most recent $KEEP snapshot folders.
ls -1dt "$DEST"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -rf

echo "[backup] done. Snapshot kept; retaining the last $KEEP."
echo "[backup] REMEMBER: copy $DEST off this machine (and keep env.backup somewhere private)."
