#!/usr/bin/env python3
"""Copy the OpenClaw ICT GitHub Copilot OAuth token into TeamBots.

Never prints the token, only a prefix/length (ghu_…, len=N). Run from the
TeamBots root after .env is in place:  .venv/bin/python scripts/import_ict_copilot.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

# Load TeamBots .env before importing app.config.
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)

sys.path.insert(0, str(ROOT))

from app import config, copilot, llm  # noqa: E402
from app.db import Endpoint, Team, TeamMember, User, init_db, select, session  # noqa: E402


def describe(token: str) -> str:
    if not token:
        return "empty"
    prefix = token[:4] + "…" if len(token) >= 4 else "…"
    return f"{prefix} (len={len(token)})"


def load_openclaw_token() -> tuple[str, str]:
    path = Path.home() / ".openclaw" / "auth-profiles.json"
    if not path.exists():
        raise SystemExit(f"OpenClaw auth profiles not found at {path}")
    data = json.loads(path.read_text())
    profiles = data.get("profiles", data) if isinstance(data, dict) else {}
    if not isinstance(profiles, dict):
        raise SystemExit("auth-profiles.json has no profiles object")
    keys = [
        "github-copilot:ictgroup",
        '"github-copilot:ictgroup"',
    ]
    row = None
    used = None
    for k in keys:
        if k in profiles:
            row, used = profiles[k], k
            break
    if row is None:
        # Fall back to a case-insensitive substring search on keys only.
        for k, v in profiles.items():
            if "ictgroup" in k.replace('"', "") and "copilot" in k:
                row, used = v, k
                break
    if not isinstance(row, dict) or not row.get("token"):
        raise SystemExit("no github-copilot:ictgroup token in auth-profiles.json")
    token = row["token"]
    if not isinstance(token, str) or not token.strip():
        raise SystemExit("ictgroup profile has an empty token")
    domain = "ictgroup.ghe.com"
    base = row.get("baseUrl") or row.get("base_url") or ""
    if isinstance(base, str) and "ghe.com" in base:
        try:
            domain = copilot.normalize_domain(base)
        except ValueError:
            pass
    print(f"OpenClaw profile {used!r}: token {describe(token)}")
    return token.strip(), domain


def pick_model(ids: list[str], preferred: str = "claude-sonnet-5") -> str:
    if preferred in ids:
        return preferred
    for i in ids:
        if i.replace("_", "-").lower() == preferred:
            return i
    for i in ids:
        if preferred in i.lower().replace("_", "-"):
            return i
    return preferred


async def main() -> int:
    init_db()
    token, domain = load_openclaw_token()
    with session() as s:
        user = s.exec(select(User).where(User.username == "koenvanwijk")).first()
        team = s.exec(select(Team).where(Team.name == "Zandoerle")).first()
        if not user or not team:
            print("TeamBots user koenvanwijk or team Zandoerle not found")
            return 1
        membership = s.exec(select(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.user_id == user.id)).first()
        if not membership:
            print("koenvanwijk is not a member of Zandoerle")
            return 1
        existing = s.exec(select(Endpoint).where(
            Endpoint.team_id == team.id, Endpoint.name == "ICT Copilot")).first()
        fields = dict(
            team_id=team.id, owner_id=user.id, name="ICT Copilot",
            provider="github_copilot", github_domain=domain,
            base_url=copilot.fallback_base(domain),
            api_key_enc=config.encrypt(token),
            default_model="claude-sonnet-5",
            shared=True,
        )
        if existing:
            for k, v in fields.items():
                if k in ("team_id", "owner_id", "name"):
                    continue
                setattr(existing, k, v)
            s.add(existing)
            action = "updated"
            ep = existing
        else:
            ep = Endpoint(**fields)
            s.add(ep)
            action = "created"
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    print(f"Endpoint 'ICT Copilot' {action} on Zandoerle (id={ep_id}, "
          f"domain={domain}, owner=koenvanwijk, shared within the project).")
    print("Existing bots were not changed.")

    # Live exchange + model list. Stop clearly on 401/403.
    with session() as s:
        ep = s.get(Endpoint, ep_id)
        try:
            sess = await copilot.exchange(config.decrypt(ep.api_key_enc), ep.github_domain)
        except llm.LLMError as err:
            print(f"Copilot token exchange failed: {err}")
            print("The GitHub token is stored encrypted, but the endpoint was not verified.")
            return 2
        print("Token exchange succeeded. "
              f"API base host set, session expires_at_ms={sess['expires_at_ms']}, "
              f"session token {describe(sess['token'])}.")
        try:
            models = await copilot.list_models(ep)
        except llm.LLMError as err:
            print(f"Model list failed: {err}")
            models = []
        ids = [m["id"] for m in models]
        if ids:
            print("Models available: " + ", ".join(ids))
            chosen = pick_model(ids)
            if chosen != ep.default_model or chosen not in ids:
                if chosen in ids:
                    ep.default_model = chosen
                    s.add(ep)
                    s.commit()
                    print(f"Default model set to {chosen}.")
                else:
                    print(f"Preferred model claude-sonnet-5 is not in the catalog; "
                          f"left default_model={ep.default_model}.")
            else:
                print(f"Default model {chosen} is available.")
        else:
            print("No model catalog returned; leaving default_model=claude-sonnet-5.")

        tool = {"type": "function", "function": {"name": "ping", "description": "Call this tool.",
                                                 "parameters": {"type": "object",
                                                                "properties": {"ok": {"type": "boolean"}},
                                                                "required": ["ok"]}}}
        try:
            reply = await copilot.chat(
                ep, ep.default_model,
                [{"role": "user", "content": "Call the ping tool with ok=true."}],
                [tool],
            )
        except llm.LLMError as err:
            print(f"Tool-calling verification failed: {err}")
            return 3
        if reply.get("tool_calls"):
            names = [tc.get("function", {}).get("name") for tc in reply["tool_calls"]]
            print(f"Tool-calling verification succeeded: {names}")
            return 0
        content = (reply.get("content") or "")[:200]
        print("Tool-calling verification: model answered in text instead of a tool call: "
              + content)
        return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
