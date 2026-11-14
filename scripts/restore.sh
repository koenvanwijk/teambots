#!/usr/bin/env bash
# Restore a full backup made by scripts/backup.sh.
#
#   scripts/restore.sh <snapshot-dir>        # e.g. backups/20261114-203000
#
# Restores the database, the project/overview Docker volumes, and (on request) .env.
# STOP THE APP FIRST so nothing writes mid-restore:  ./stop-local.sh  (or: docker compose down)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
SRC="${1:?usage: scripts/restore.sh <snapshot-dir>}"
DATA_DIR="${TB_DATA_DIR:-$ROOT/data}"
DB="${TB_DB_FILE:-$DATA_DIR/teambots.db}"
[ -d "$SRC" ] || { echo "no such snapshot: $SRC" >&2; exit 1; }

echo "[restore] from $SRC"
echo "[restore] Make sure the app is stopped. Continuing in 3s (Ctrl-C to abort)..."
sleep 3

# 1) Database (the live file is moved aside, not deleted).
if [ -f "$SRC/teambots.db" ]; then
  mkdir -p "$DATA_DIR"
  [ -f "$DB" ] && mv "$DB" "$DB.pre-restore-$(date +%s)" && echo "  old db moved aside"
  cp "$SRC/teambots.db" "$DB" && echo "  db: restored"
fi

# 2) Docker volumes (recreated and refilled from each tarball).
if command -v docker >/dev/null 2>&1; then
  for tgz in "$SRC"/volume-*.tgz; do
    [ -e "$tgz" ] || continue
    v="$(basename "$tgz")"; v="${v#volume-}"; v="${v%.tgz}"
    docker volume create "$v" >/dev/null
    docker run --rm -v "$v":/v -v "$SRC":/b alpine sh -c "rm -rf /v/* /v/.[!.]* 2>/dev/null; tar xzf /b/$(basename "$tgz") -C /v" \
      && echo "  volume $v: restored"
  done
fi

# 3) Local-backend workspaces.
if [ -f "$SRC/workspaces-local.tgz" ]; then
  rm -rf "$DATA_DIR/workspaces"
  tar xzf "$SRC/workspaces-local.tgz" -C "$DATA_DIR" && echo "  workspaces(local): restored"
fi

# 4) .env is NOT restored automatically (it may differ from the running config).
if [ -f "$SRC/env.backup" ]; then
  echo "  env: NOT restored automatically. If you need the backed-up secrets, compare and copy:"
  echo "       diff $SRC/env.backup .env"
fi
echo "[restore] done. Start the app again."
