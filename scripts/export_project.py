#!/usr/bin/env python3
"""Export one project (team) from TeamBots into a single portable archive.

A project lives in two places: rows in the central database (its bots, conversations,
messages, memory, secrets, usage, ...) and its own workspace volume (files, the bot's
browser profile, its `gh` login). This gathers both.

    scripts/export_project.py <project-id-or-name> [--db PATH] [--out DIR] [--no-usage]

It produces  teambots-project-p<id>-<stamp>.tgz  containing:
    project.json     every database row scoped to the project, plus the users it references
    workspace.tgz    the project's workspace volume (tb-p<id>), if present
    knowledge.tgz    the project's knowledge base with its git history (data/knowledge/p<id>), if present
    MANIFEST.json    project id/name, export time, and a fingerprint of the secret key

Secrets (bot secrets, endpoint API keys, MCP headers) stay ENCRYPTED in the export. They
can only be decrypted on an instance that uses the SAME TB_SECRET_KEY. MANIFEST.json records
a fingerprint of the source key so the import side can warn on a mismatch. This export is
read-only; it never changes the source database.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def secret_key_fingerprint() -> str:
    """First bytes of sha256(TB_SECRET_KEY), so we can compare keys without storing one."""
    key = os.environ.get("TB_SECRET_KEY")
    if not key:
        env = os.path.join(HERE, ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("TB_SECRET_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""


def rows(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def in_clause(values):
    """(placeholders, params) for a WHERE ... IN (...) over a Python list."""
    values = list(values)
    if not values:
        return "(NULL)", []
    return "(" + ",".join("?" * len(values)) + ")", values


def collect(cur, team_id: int, include_usage: bool) -> dict:
    data = {}
    data["team"] = rows(cur, "SELECT * FROM team WHERE id=?", (team_id,))
    if not data["team"]:
        raise SystemExit(f"no project with id {team_id}")

    data["teammember"] = rows(cur, "SELECT * FROM teammember WHERE team_id=?", (team_id,))
    data["endpoint"] = rows(cur, "SELECT * FROM endpoint WHERE team_id=?", (team_id,))
    data["bot"] = rows(cur, "SELECT * FROM bot WHERE team_id=?", (team_id,))
    data["conversation"] = rows(cur, "SELECT * FROM conversation WHERE team_id=?", (team_id,))

    bot_ids = [b["id"] for b in data["bot"]]
    conv_ids = [c["id"] for c in data["conversation"]]
    bp, bpar = in_clause(bot_ids)
    cp, cpar = in_clause(conv_ids)

    data["botaccess"] = rows(cur, f"SELECT * FROM botaccess WHERE bot_id IN {bp}", bpar)
    data["channelmember"] = rows(cur, f"SELECT * FROM channelmember WHERE conversation_id IN {cp}", cpar)
    data["message"] = rows(cur, f"SELECT * FROM message WHERE conversation_id IN {cp}", cpar)
    data["memory"] = rows(cur, f"SELECT * FROM memory WHERE bot_id IN {bp}", bpar)
    data["skill"] = rows(cur, f"SELECT * FROM skill WHERE bot_id IN {bp}", bpar)
    data["routine"] = rows(cur, f"SELECT * FROM routine WHERE bot_id IN {bp} OR conversation_id IN {cp}", bpar + cpar)
    data["approval"] = rows(cur, f"SELECT * FROM approval WHERE conversation_id IN {cp} OR bot_id IN {bp}", cpar + bpar)
    data["approvalrule"] = rows(cur, f"SELECT * FROM approvalrule WHERE bot_id IN {bp}", bpar)
    # Bot secrets and servers, plus the project-wide ones (team_id set, bot_id 0); values stay encrypted.
    scope_cols = {r[1] for r in cur.execute("PRAGMA table_info(secret)").fetchall()}
    scope = " OR (team_id=? AND bot_id=0)" if "team_id" in scope_cols else ""
    spar = list(bpar) + ([team_id] if scope else [])
    data["secret"] = rows(cur, f"SELECT * FROM secret WHERE bot_id IN {bp}{scope}", spar)
    data["mcpserver"] = rows(cur, f"SELECT * FROM mcpserver WHERE bot_id IN {bp}{scope}", spar)
    if include_usage:
        data["llmusage"] = rows(cur, "SELECT * FROM llmusage WHERE team_id=?", (team_id,))

    # The users this project references (members, owners, authors). Global accounts, so we
    # export them read-only and strip the password hash; people re-link their login on import.
    uids = set()
    for tbl, col in [("teammember", "user_id"), ("bot", "owner_id"), ("botaccess", "user_id"),
                     ("conversation", "user_id"), ("memory", "user_id"), ("routine", "owner_id"),
                     ("approval", "requester_id"), ("approvalrule", "user_id"),
                     ("message", "author_user_id"), ("message", "requester_id")]:
        for r in data.get(tbl, []):
            if r.get(col):
                uids.add(r[col])
    up, upar = in_clause(sorted(uids))
    users = rows(cur, f"SELECT * FROM user WHERE id IN {up}", upar)
    for u in users:
        u["password_hash"] = "!"   # never carry credentials across instances
    data["user"] = users
    return data


def export_volume(team_id: int, dest_tgz: str) -> str:
    """Save the project's workspace. Docker volume tb-p<id>, else local data/workspaces/p<id>."""
    key = f"p{team_id}"
    vol = f"tb-{key}"
    try:
        have = subprocess.run(["docker", "volume", "inspect", vol],
                              capture_output=True).returncode == 0
    except FileNotFoundError:
        have = False
    if have:
        subprocess.run(["docker", "run", "--rm", "-v", f"{vol}:/v:ro",
                        "-v", f"{os.path.dirname(dest_tgz)}:/b", "alpine",
                        "tar", "czf", f"/b/{os.path.basename(dest_tgz)}", "-C", "/v", "."],
                       check=True)
        return "docker"
    local = os.path.join(os.environ.get("TB_DATA_DIR", os.path.join(HERE, "data")), "workspaces", key)
    if os.path.isdir(local):
        with tarfile.open(dest_tgz, "w:gz") as t:
            t.add(local, arcname=".")
        return "local"
    return "none"


def main():
    ap = argparse.ArgumentParser(description="Export one TeamBots project to a portable archive.")
    ap.add_argument("project", help="project id (e.g. 4) or exact name (e.g. 'Samen Tobias')")
    ap.add_argument("--db", default=os.path.join(os.environ.get("TB_DATA_DIR", os.path.join(HERE, "data")), "teambots.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "exports"))
    ap.add_argument("--no-usage", action="store_true", help="skip the llmusage cost history (can be large)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"database not found: {args.db}")
    con = sqlite3.connect(args.db)
    cur = con.cursor()

    if args.project.isdigit():
        team_id = int(args.project)
    else:
        found = rows(cur, "SELECT id FROM team WHERE name=?", (args.project,))
        if not found:
            raise SystemExit(f"no project named {args.project!r}")
        if len(found) > 1:
            raise SystemExit(f"{len(found)} projects named {args.project!r}; use the id")
        team_id = found[0]["id"]

    data = collect(cur, team_id, include_usage=not args.no_usage)
    con.close()
    name = data["team"][0]["name"]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    os.makedirs(args.out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "project.json"), "w") as f:
            json.dump(data, f, indent=2, default=str)
        vol_kind = export_volume(team_id, os.path.join(tmp, "workspace.tgz"))
        kb_dir = os.path.join(os.environ.get("TB_DATA_DIR", os.path.join(HERE, "data")), "knowledge", f"p{team_id}")
        if os.path.isdir(kb_dir):  # AGENTS.md, memory, profiles and skills, with who changed what
            with tarfile.open(os.path.join(tmp, "knowledge.tgz"), "w:gz") as t:
                t.add(kb_dir, arcname=".")
        manifest = {
            "teambots_export": 1,
            "project_id": team_id,
            "project_name": name,
            "exported_at": stamp,
            "row_counts": {k: len(v) for k, v in data.items()},
            "workspace": vol_kind,
            "knowledge": os.path.isdir(kb_dir),
            "secret_key_fp": secret_key_fingerprint(),
            "note": "secrets are encrypted with the source TB_SECRET_KEY; the target must share it to decrypt.",
        }
        with open(os.path.join(tmp, "MANIFEST.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        out_tgz = os.path.join(args.out, f"teambots-project-p{team_id}-{stamp}.tgz")
        with tarfile.open(out_tgz, "w:gz") as t:
            for fn in ("MANIFEST.json", "project.json", "workspace.tgz", "knowledge.tgz"):
                p = os.path.join(tmp, fn)
                if os.path.exists(p):
                    t.add(p, arcname=fn)

    total = sum(len(v) for v in data.values())
    print(f"Exported project {team_id} ({name!r}): {total} rows, workspace={vol_kind}")
    print(f"  -> {out_tgz}")
    if any(data["secret"]) or any(data["endpoint"]) or any(data["mcpserver"]):
        print("  note: encrypted secrets included; the target needs the same TB_SECRET_KEY to use them.")


if __name__ == "__main__":
    main()
