#!/usr/bin/env python3
"""Import a project archive made by export_project.py into a TeamBots instance.

    scripts/import_project.py <archive.tgz> [--db PATH] [--dry-run]

This is the v1, safe-by-refusal importer: it inserts the project's rows and restores its
workspace, but it does NOT remap ids. If any id it would insert (the project, a bot, a
conversation, a message, ...) already exists in the target database, it refuses and changes
nothing, so it can never overwrite or merge into existing data. Import into a fresh instance,
or back into the same instance after a loss. Cross-instance merging with id remapping is not
supported here.

A referenced user that already exists with the SAME username is reused (not duplicated);
one whose id is taken by a DIFFERENT username is treated as a clash and refuses the import.
Stop the app before importing so nothing writes concurrently.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Parent-before-child, so it works whether or not foreign keys are enforced.
INSERT_ORDER = [
    "user", "team", "endpoint", "bot", "conversation", "teammember", "botaccess",
    "channelmember", "message", "memory", "skill", "routine", "approval",
    "approvalrule", "secret", "mcpserver", "llmusage",
]


def target_key_fp(db_dir: str) -> str:
    key = os.environ.get("TB_SECRET_KEY")
    if not key:
        env = os.path.join(HERE, ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("TB_SECRET_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""


def existing_ids(cur, table, ids):
    if not ids:
        return set()
    ph = ",".join("?" * len(ids))
    cur.execute(f"SELECT id FROM {table} WHERE id IN ({ph})", list(ids))
    return {r[0] for r in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser(description="Import a TeamBots project archive.")
    ap.add_argument("archive")
    ap.add_argument("--db", default=os.path.join(os.environ.get("TB_DATA_DIR", os.path.join(HERE, "data")), "teambots.db"))
    ap.add_argument("--dry-run", action="store_true", help="check for collisions and report; change nothing")
    args = ap.parse_args()

    if not os.path.exists(args.archive):
        raise SystemExit(f"no such archive: {args.archive}")
    if not os.path.exists(args.db):
        raise SystemExit(f"database not found: {args.db}")

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(args.archive, "r:gz") as t:
            t.extractall(tmp, filter="data")   # reject absolute paths / traversal in an archive from elsewhere
        manifest = json.load(open(os.path.join(tmp, "MANIFEST.json")))
        data = json.load(open(os.path.join(tmp, "project.json")))
        team_id = manifest["project_id"]
        name = manifest.get("project_name", "?")
        print(f"[import] project {team_id} ({name!r}) from {os.path.basename(args.archive)}")

        con = sqlite3.connect(args.db)
        cur = con.cursor()

        # Secret-key fingerprint check: encrypted secrets only decrypt under the source key.
        src_fp, dst_fp = manifest.get("secret_key_fp", ""), target_key_fp(os.path.dirname(args.db))
        has_secrets = data.get("secret") or data.get("endpoint") or data.get("mcpserver")
        if has_secrets and src_fp and dst_fp and src_fp != dst_fp:
            print(f"  WARNING: TB_SECRET_KEY differs (source {src_fp} != target {dst_fp}).")
            print("           Imported secrets/API keys will NOT be decryptable on this instance.")

        # Collision checks across every table that carries ids.
        problems = []
        reuse_users = set()        # user ids that already exist with the same username -> reuse
        for u in data.get("user", []):
            cur.execute("SELECT username FROM user WHERE id=?", (u["id"],))
            row = cur.fetchone()
            if row is None:
                continue
            if row[0] == u["username"]:
                reuse_users.add(u["id"])
            else:
                problems.append(f"user id {u['id']} is taken by a different account ({row[0]!r})")
        for table in INSERT_ORDER:
            if table == "user":
                continue
            recs = data.get(table, [])
            ids = [r["id"] for r in recs if "id" in r]
            clash = existing_ids(cur, table, ids)
            if clash:
                problems.append(f"{table}: ids already present: {sorted(clash)[:8]}")

        if problems:
            print("[import] REFUSED — the target already contains conflicting rows:")
            for p in problems:
                print("   - " + p)
            print("[import] Nothing was changed. Import into a fresh instance, or restore a full backup instead.")
            sys.exit(2)

        if args.dry_run:
            print("[import] dry run OK — no id collisions. Rerun without --dry-run to apply.")
            return

        # Insert everything in one transaction.
        inserted = {}
        try:
            cur.execute("BEGIN")
            for table in INSERT_ORDER:
                recs = data.get(table, [])
                n = 0
                for r in recs:
                    if table == "user" and r["id"] in reuse_users:
                        continue
                    cols = list(r.keys())
                    ph = ",".join("?" * len(cols))
                    cur.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                                [r[c] for c in cols])
                    n += 1
                inserted[table] = n
            con.commit()
        except Exception as e:
            con.rollback()
            raise SystemExit(f"[import] FAILED, rolled back: {e}")
        con.close()

        # Restore the workspace volume.
        ws = os.path.join(tmp, "workspace.tgz")
        if os.path.exists(ws) and manifest.get("workspace") != "none":
            vol = f"tb-p{team_id}"
            if manifest.get("workspace") == "docker" or _docker_available():
                subprocess.run(["docker", "volume", "create", vol], check=True, capture_output=True)
                subprocess.run(["docker", "run", "--rm", "-v", f"{vol}:/v", "-v", f"{tmp}:/b", "alpine",
                                "sh", "-c", "tar xzf /b/workspace.tgz -C /v"], check=True)
                print(f"  workspace restored to docker volume {vol}")
            else:
                dest = os.path.join(os.environ.get("TB_DATA_DIR", os.path.join(HERE, "data")), "workspaces", f"p{team_id}")
                os.makedirs(dest, exist_ok=True)
                with tarfile.open(ws, "r:gz") as t:
                    t.extractall(dest, filter="data")
                print(f"  workspace restored to {dest}")

        total = sum(inserted.values())
        print(f"[import] done: inserted {total} rows ({inserted}). Reused {len(reuse_users)} existing user(s).")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    main()
