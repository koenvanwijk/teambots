"""End-to-end test of the MVP against a scripted model endpoint. Run: pytest -q"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest
import uvicorn

TMP = tempfile.mkdtemp(prefix="teambots-test-")
os.environ.update(TB_DATA_DIR=TMP, TB_SANDBOX="local", TB_SECRET_KEY="test-secret", TB_HTTP_ALLOW_PRIVATE="1")
try:  # a Chromium for the screen test; skipped when none is installed
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        os.environ["TB_BROWSER"] = _p.chromium.executable_path
except Exception:  # noqa: BLE001
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

import mock_llm  # noqa: E402
from app.main import app  # noqa: E402

MCP_URL = "http://127.0.0.1:18792/mcp"
LLM_PORT = 0  # actual bound port, set by fixture c
LLM_URL = ""


def _requested_mock_llm_port() -> int:
    raw = os.environ.get("TB_MOCK_LLM_PORT")
    return int(raw) if raw not in (None, "") else 0


def _started_port(server: uvicorn.Server) -> int:
    for srv in server.servers:
        for sock in srv.sockets:
            return sock.getsockname()[1]
    raise RuntimeError("mock LLM did not bind a port")


@pytest.fixture(scope="module")
def c():
    global LLM_PORT, LLM_URL
    server = uvicorn.Server(uvicorn.Config(
        mock_llm.app, host="127.0.0.1", port=_requested_mock_llm_port(), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    mcp_proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "mock_mcp.py")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 2.5
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        mcp_proc.terminate()
        raise RuntimeError("mock LLM failed to start")
    LLM_PORT = _started_port(server)
    LLM_URL = f"http://127.0.0.1:{LLM_PORT}/v1"
    leftover = deadline - time.time()
    if leftover > 0:
        time.sleep(leftover)
    with TestClient(app) as client:
        yield client
    server.should_exit = True
    mcp_proc.terminate()
    shutil.rmtree(TMP, ignore_errors=True)


S: dict = {}  # shared state between the ordered tests


def H(who):
    return {"Authorization": f"Bearer {S[who]['token']}"}


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


def say(c, who, conv, text):
    """Send a message and wait for the bot's turn to end (final answer or approval card)."""
    before = len(c.get(f"/api/conversations/{conv}/messages", headers=H(who)).json())
    assert c.post(f"/api/conversations/{conv}/messages", json={"content": text}, headers=H(who)).status_code == 200

    def done():
        msgs = c.get(f"/api/conversations/{conv}/messages", headers=H(who)).json()
        if c.get(f"/api/conversations/{conv}/approvals", headers=H(who)).json():
            return msgs
        new = msgs[before + 1:]
        return msgs if new and new[-1]["role"] == "assistant" and not new[-1]["tool_calls"] else None
    return wait_for(done)


def tool(tool_name, **arguments):
    return "TOOL:" + json.dumps({"name": tool_name, "arguments": arguments})


def approve(c, who, conv, allow=True, always=False, expect=200):
    a = c.get(f"/api/conversations/{conv}/approvals", headers=H(who)).json()[0]
    r = c.post(f"/api/approvals/{a['id']}", json={"allow": allow, "always": always}, headers=H(who))
    assert r.status_code == expect, r.text
    if expect != 200:
        return None

    def done():
        msgs = c.get(f"/api/conversations/{conv}/messages", headers=H(who)).json()
        return msgs if msgs[-1]["role"] == "assistant" and not msgs[-1]["tool_calls"] else None
    return wait_for(done)


def test_setup_team_and_endpoint(c):
    for name in ("alice", "bob", "carol"):
        r = c.post("/api/register", json={"username": name, "password": "password123", "display_name": name.title()})
        assert r.status_code == 200, r.text
        S[name] = r.json()
    team = c.post("/api/teams", json={"name": "Project X", "client": "Acme"}, headers=H("alice")).json()
    S["team"] = team["id"]
    for name in ("bob", "carol"):
        assert c.post("/api/teams/join", json={"invite_code": team["invite_code"]}, headers=H(name)).status_code == 200
    ep = c.post(f"/api/teams/{team['id']}/endpoints", headers=H("alice"), json={
        "name": "mock", "base_url": LLM_URL, "api_key": "sk-test-key", "default_model": "mock-1"}).json()
    S["ep"] = ep["id"]
    assert "sk-test-key" not in json.dumps(ep) and ep["has_key"]
    t = c.post(f"/api/endpoints/{ep['id']}/test", headers=H("alice")).json()
    assert t == {"reachable": True, "tool_calling": True, "detail": "ok"}
    # a private endpoint is invisible to and unusable by others
    assert c.get(f"/api/teams/{team['id']}/endpoints", headers=H("bob")).json() == []


def test_team_bot_draft_then_publish(c):
    bot = c.post(f"/api/teams/{S['team']}/bots", headers=H("alice"), json={
        "name": "Helper", "handle": "helper", "endpoint_id": S["ep"], "is_team": True,
        "instructions": "You help the project team."}).json()
    S["bot"] = bot["id"]
    assert bot["role"] == "owner"
    assert c.get(f"/api/teams/{S['team']}/bots", headers=H("bob")).json() == []  # draft: owner only
    assert c.post(f"/api/bots/{bot['id']}/dm", headers=H("bob")).status_code == 404
    r = c.put(f"/api/bots/{bot['id']}/share", headers=H("alice"), json={
        "published": True, "access": "selected", "members": [{"user_id": S["bob"]["user"]["id"]}]})
    assert r.status_code == 200
    assert c.get(f"/api/teams/{S['team']}/bots", headers=H("bob")).json()[0]["role"] == "collaborator"
    assert c.get(f"/api/teams/{S['team']}/bots", headers=H("carol")).json() == []  # not selected
    c.put(f"/api/bots/{bot['id']}/share", headers=H("alice"), json={"published": True, "access": "team"})
    assert c.get(f"/api/teams/{S['team']}/bots", headers=H("carol")).json()[0]["role"] == "collaborator"
    # within a project every member may change the bot (instructions, setup, secrets) ...
    r = c.patch(f"/api/bots/{bot['id']}", json={"instructions": "x"}, headers=H("bob"))
    assert r.status_code == 200 and r.json()["instructions"] == "x"
    assert c.patch(f"/api/bots/{bot['id']}", json={"instructions": bot["instructions"]},
                   headers=H("carol")).status_code == 200
    assert c.get(f"/api/bots/{bot['id']}/secrets", headers=H("bob")).status_code == 200
    # ... but who may use the bot stays with its owner and managers
    assert c.get(f"/api/bots/{bot['id']}/share", headers=H("bob")).status_code == 403
    assert c.put(f"/api/bots/{bot['id']}/share", headers=H("bob"), json={
        "published": True, "access": "selected", "members": []}).status_code == 403


def test_private_chats(c):
    for who in ("alice", "bob", "carol"):
        S[f"dm_{who}"] = c.post(f"/api/bots/{S['bot']}/dm", headers=H(who)).json()["id"]
    msgs = say(c, "bob", S["dm_bob"], "hello there")
    assert msgs[-1]["content"] == "ECHO[mock-1]: hello there"
    # nobody else can read Bob's chat, not even the bot's owner
    assert c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("alice")).status_code == 404
    assert c.post(f"/api/conversations/{S['dm_bob']}/messages", json={"content": "x"},
                  headers=H("alice")).status_code == 404


def kb_read(c, who, path):
    r = c.get(f"/api/bots/{S['bot']}/knowledge/file", params={"path": path}, headers=H(who))
    assert r.status_code == 200, r.text
    return r.json()


def kb_write(c, who, path, content, base_hash=None):
    return c.put(f"/api/bots/{S['bot']}/knowledge/file", headers=H(who),
                 json={"path": path, "content": content, "base_hash": base_hash})


def test_memory_is_project_wide_and_budgeted(c):
    # Any member may let the bot write team memory: no review step, everyone sees it from the next turn.
    msgs = say(c, "bob", S["dm_bob"], tool("memory", target="team", operations=[
        {"action": "add", "content": "deploys happen on tuesdays"}]))
    assert "memory/notes.md updated: 1 added" in msgs[-1]["content"]
    assert say(c, "carol", S["dm_carol"], "RECALL deploys happen on tuesdays")[-1]["content"] == "yes"
    assert say(c, "carol", S["dm_carol"], "RECALL TEAM MEMORY (knowledge base memory/")[-1]["content"] == "yes"

    # The older tool name still works and writes the person's profile (users/bob.md), shown only to Bob.
    say(c, "bob", S["dm_bob"], tool("remember", content="bob prefers short answers", scope="personal"))
    assert say(c, "bob", S["dm_bob"], "RECALL bob prefers short answers")[-1]["content"] == "yes"
    assert say(c, "alice", S["dm_alice"], "RECALL bob prefers short answers")[-1]["content"] == "no"
    assert "bob prefers short answers" in kb_read(c, "bob", "users/bob.md")["content"]

    # patch by old_text, then a batch that would overflow the budget is refused as a whole
    say(c, "carol", S["dm_carol"], tool("memory", target="team", operations=[
        {"action": "patch", "old_text": "tuesdays", "content": "wednesdays"}]))
    assert say(c, "alice", S["dm_alice"], "RECALL deploys happen on wednesdays")[-1]["content"] == "yes"
    big = say(c, "carol", S["dm_carol"], tool("memory", target="team", operations=[
        {"action": "add", "content": "x" * 3100}]))[-1]["content"]
    assert "over its budget" in big and "deploys happen on wednesdays" in big

    # People edit the same file in the UI: any member, with the hash they loaded.
    f = kb_read(c, "carol", "memory/notes.md")
    r = kb_write(c, "carol", "memory/notes.md", f["content"].replace("wednesdays", "tuesdays"), f["hash"])
    assert r.status_code == 200, r.text
    assert r.json()["hash"] == kb_read(c, "bob", "memory/notes.md")["hash"] != f["hash"]
    # git knows who wrote what: the bot (on behalf of a person) and the people themselves
    hist = c.get(f"/api/bots/{S['bot']}/knowledge/history", params={"path": "memory/notes.md"},
                 headers=H("bob")).json()
    assert hist[0]["author"] == "Carol" and hist[0]["email"] == "carol@users.teambots.local"
    assert any(h["author"] == "Helper (bot)" and "(for Bob)" in h["message"] for h in hist)
    assert any(h["author"] == "Helper (bot)" and "(for Carol)" in h["message"] for h in hist)
    # files may hold more than the prompt budget; the prompt then points at what it left out
    assert kb_write(c, "carol", "memory/clients.md", "# Clients\n" + "acme details. " * 300).status_code == 200
    assert say(c, "bob", S["dm_bob"], "RECALL - memory/clients.md (")[-1]["content"] == "yes"
    cur = kb_read(c, "carol", "memory/clients.md")
    assert c.delete(f"/api/bots/{S['bot']}/knowledge/file", params={"path": "memory/clients.md",
                    "base_hash": cur["hash"]}, headers=H("bob")).status_code == 200


def test_knowledge_base_hashes_conflicts_and_history(c):
    bot_url = f"/api/bots/{S['bot']}/knowledge"
    # a bot creates a file (and its directories) without a base hash
    out = say(c, "bob", S["dm_bob"], tool("knowledge_write", path="clients/acme/notes.md",
                                         content="# ACME\n- contact: Dana\n"))[-1]["content"]
    assert out.startswith("RESULT: created clients/acme/notes.md, new hash ")
    seen = out.rsplit(" ", 1)[1]
    tree = c.get(bot_url, headers=H("carol")).json()
    assert "clients/acme" in tree["dirs"] and any(f["path"] == "clients/acme/notes.md" for f in tree["files"])
    read = say(c, "bob", S["dm_bob"], tool("knowledge_read", path="clients/acme/notes.md"))[-1]["content"]
    assert f"hash: {seen}" in read and "contact: Dana" in read
    # creating it again is refused: it exists already
    again = say(c, "bob", S["dm_bob"], tool("knowledge_write", path="clients/acme/notes.md",
                                           content="# ACME\n"))[-1]["content"]
    assert "conflict: clients/acme/notes.md already exists" in again

    # Alice edits it in the UI; the bot still holds the old hash, so its write is refused with who and a diff
    r = kb_write(c, "alice", "clients/acme/notes.md", "# ACME\n- contact: Dana (CFO)\n", seen)
    assert r.status_code == 200, r.text
    stale = say(c, "bob", S["dm_bob"], tool("knowledge_write", path="clients/acme/notes.md",
                                           content="# ACME\n- contact: Dan\n", base_hash=seen))[-1]["content"]
    assert "conflict: clients/acme/notes.md changed since you read it by Alice" in stale
    assert "+- contact: Dana (CFO)" in stale and "Nothing was written" in stale
    assert "Dana (CFO)" in kb_read(c, "bob", "clients/acme/notes.md")["content"]
    patch = say(c, "bob", S["dm_bob"], tool("knowledge_patch", path="clients/acme/notes.md", old_text="Dana",
                                           new_text="Dan", base_hash=seen))[-1]["content"]
    assert patch.startswith("RESULT: conflict:")
    gone = say(c, "bob", S["dm_bob"], tool("knowledge_delete", path="clients/acme/notes.md",
                                          base_hash=seen))[-1]["content"]
    assert gone.startswith("RESULT: conflict:")
    # the same rule for people: a stale save in the UI gets 409 with the last change and a diff
    r = kb_write(c, "carol", "clients/acme/notes.md", "# ACME\n", seen)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "conflict" and d["last_change"]["author"] == "Alice" and "Dana (CFO)" in d["diff"]
    # with the current hash the bot's patch goes through
    cur = kb_read(c, "bob", "clients/acme/notes.md")
    ok = say(c, "bob", S["dm_bob"], tool("knowledge_patch", path="clients/acme/notes.md", old_text="(CFO)",
                                        new_text="(CFO, prefers mail)", base_hash=cur["hash"]))[-1]["content"]
    assert ok.startswith("RESULT: saved clients/acme/notes.md")

    # history per file (who and when), view a version and put it back
    hist = c.get(f"{bot_url}/history", params={"path": "clients/acme/notes.md"}, headers=H("carol")).json()
    assert [h["author"] for h in hist] == ["Helper (bot)", "Alice", "Helper (bot)"]
    first = hist[-1]["commit"]
    v = c.get(f"{bot_url}/version", params={"path": "clients/acme/notes.md", "commit": first}, headers=H("carol"))
    assert v.json()["content"] == "# ACME\n- contact: Dana\n"
    cur = kb_read(c, "carol", "clients/acme/notes.md")
    r = c.post(f"{bot_url}/revert", json={"path": "clients/acme/notes.md", "commit": first, "base_hash": cur["hash"]},
               headers=H("carol"))
    assert r.status_code == 200, r.text
    assert kb_read(c, "bob", "clients/acme/notes.md")["content"] == "# ACME\n- contact: Dana\n"
    hist = c.get(f"{bot_url}/history", params={"path": "clients/acme/notes.md"}, headers=H("bob")).json()
    assert hist[0]["author"] == "Carol" and hist[0]["message"].startswith("Revert clients/acme/notes.md")
    assert "Alice" in say(c, "bob", S["dm_bob"], tool("knowledge_history", path="clients/acme/notes.md"))[-1]["content"]

    # folders, bad paths and people outside the project
    assert c.post(f"{bot_url}/dir", json={"path": "drafts"}, headers=H("bob")).status_code == 200
    assert "drafts" in c.get(bot_url, headers=H("bob")).json()["dirs"]
    assert c.delete(f"{bot_url}/file", params={"path": "drafts"}, headers=H("bob")).status_code == 200
    assert kb_write(c, "bob", "../escape.md", "x").status_code == 400
    assert kb_write(c, "bob", ".git/config", "x").status_code == 400
    assert "error" in say(c, "bob", S["dm_bob"], tool("knowledge_read", path="../../etc/passwd"))[-1]["content"]


def test_computer_with_approval_gate(c):
    say(c, "bob", S["dm_bob"], tool("run_command", command="echo hi-from-bob > note.txt && cat note.txt"))
    pend = c.get(f"/api/conversations/{S['dm_bob']}/approvals", headers=H("bob")).json()
    assert pend[0]["tool_name"] == "run_command" and pend[0]["mine"]
    msgs = approve(c, "bob", S["dm_bob"])
    assert "exit code 0" in msgs[-1]["content"] and "hi-from-bob" in msgs[-1]["content"]
    # deny path
    say(c, "bob", S["dm_bob"], tool("run_command", command="rm note.txt"))
    assert "denied" in approve(c, "bob", S["dm_bob"], allow=False)[-1]["content"]
    # one computer per project: Alice sees the file Bob's chat created
    say(c, "alice", S["dm_alice"], tool("run_command", command="ls"))
    assert "note.txt" in approve(c, "alice", S["dm_alice"], always=True)[-1]["content"]
    # "always allow" skips the card next time, for this person only
    assert "exit code 0" in say(c, "alice", S["dm_alice"], tool("run_command", command="true"))[-1]["content"]
    # read-only tools never ask
    assert "hi-from-bob" in say(c, "bob", S["dm_bob"], tool("read_file", path="note.txt"))[-1]["content"]
    assert "error" in say(c, "bob", S["dm_bob"], tool("read_file", path="../../etc/passwd"))[-1]["content"]


def test_channel_shared_computer_and_requester_only_approval(c):
    ch = c.post(f"/api/teams/{S['team']}/channels", headers=H("alice"), json={
        "name": "general", "user_ids": [S["bob"]["user"]["id"]]}).json()  # no bot_ids: all project bots still join
    S["ch"] = ch["id"]
    mem = c.get(f"/api/channels/{ch['id']}/members", headers=H("alice")).json()
    assert any(b["id"] == S["bot"] and b["mention_only"] is False for b in mem["bots"])
    assert c.get(f"/api/conversations/{ch['id']}/messages", headers=H("carol")).status_code == 404
    # bots reply to every message by default, even without an @mention
    assert say(c, "bob", ch["id"], "morning all")[-1]["content"] == "ECHO[mock-1]: Bob: morning all"
    assert say(c, "bob", ch["id"], "@helper RECALL bob prefers short answers")[-1]["content"] == "no"  # no personal memory
    assert say(c, "bob", ch["id"], "@helper RECALL deploys happen on tuesdays")[-1]["content"] == "yes"
    say(c, "bob", ch["id"], "@helper " + tool("run_command", command="cat note.txt"))
    approve(c, "alice", ch["id"], expect=403)  # only the requester can answer the card
    assert "hi-from-bob" in approve(c, "bob", ch["id"])[-1]["content"]  # same project computer


def test_channel_new_bot_joins_remove_sticks_and_mention_only(c):
    # a newly created bot is added to existing project channels
    extra = c.post(f"/api/teams/{S['team']}/bots", headers=H("alice"), json={
        "name": "Joiner", "handle": "joiner", "endpoint_id": S["ep"], "is_team": True}).json()
    S["joiner"] = extra["id"]
    mem = c.get(f"/api/channels/{S['ch']}/members", headers=H("alice")).json()
    assert any(b["handle"] == "joiner" and b["mention_only"] is False for b in mem["bots"])
    # mention of a member bot addresses only that bot (joiner stays quiet)
    before = len(c.get(f"/api/conversations/{S['ch']}/messages", headers=H("alice")).json())
    msgs = say(c, "alice", S["ch"], "@helper ping only helper")
    new = msgs[before:]
    bot_msgs = [m for m in new if m.get("is_bot") and m["content"]]
    assert len(bot_msgs) == 1 and bot_msgs[0]["content"] == "ECHO[mock-1]: Alice: @helper ping only helper"
    # mention-only: no reply unless @mentioned
    assert c.patch(f"/api/channels/{S['ch']}/members/bots/{S['bot']}",
                   json={"mention_only": True}, headers=H("alice")).status_code == 200
    assert c.patch(f"/api/channels/{S['ch']}/members/bots/{S['joiner']}",
                   json={"mention_only": True}, headers=H("alice")).status_code == 200
    c.post(f"/api/conversations/{S['ch']}/messages", json={"content": "nobody should reply"}, headers=H("bob"))
    time.sleep(0.5)
    after = c.get(f"/api/conversations/{S['ch']}/messages", headers=H("bob")).json()
    assert after[-1]["content"] == "nobody should reply" and after[-1]["role"] == "user"
    assert say(c, "bob", S["ch"], "@helper you may speak")[-1]["content"] == "ECHO[mock-1]: Bob: @helper you may speak"
    # restore Helper to reply-all so later tests that might post to the channel stay simple
    assert c.patch(f"/api/channels/{S['ch']}/members/bots/{S['bot']}",
                   json={"mention_only": False}, headers=H("alice")).status_code == 200
    # removing a bot sticks: a later migrate does not add it back
    assert c.delete(f"/api/channels/{S['ch']}/members/bots/{S['joiner']}", headers=H("alice")).status_code == 200
    mem = c.get(f"/api/channels/{S['ch']}/members", headers=H("alice")).json()
    assert not any(b["handle"] == "joiner" for b in mem["bots"])
    from app.db import migrate_channel_bots
    assert migrate_channel_bots() == {"added": 0, "already": True}
    mem = c.get(f"/api/channels/{S['ch']}/members", headers=H("alice")).json()
    assert not any(b["handle"] == "joiner" for b in mem["bots"])
    assert c.delete(f"/api/bots/{S['joiner']}", headers=H("alice")).status_code == 200


def test_skills(c):
    args = dict(action="create", name="weekly-report", description="Compile the weekly report",
                instructions="1. collect 2. write")
    # every person who can use the bot may let it save and fix skills
    assert "saved in skills/weekly-report.md" in say(c, "bob", S["dm_bob"], tool("skill_manage", **args))[-1]["content"]
    assert say(c, "bob", S["dm_bob"], "RECALL weekly-report: Compile the weekly report")[-1]["content"] == "yes"
    assert say(c, "bob", S["dm_bob"], "RECALL <available_skills>")[-1]["content"] == "yes"
    assert "updated" in say(c, "carol", S["dm_carol"], tool("skill_manage", action="patch", name="weekly-report",
                                                            old_text="2. write", new_text="2. write 3. send"))[-1]["content"]
    assert "3. send" in say(c, "bob", S["dm_bob"], tool("use_skill", name="weekly-report"))[-1]["content"]
    # the older tool name still works
    assert "saved" in say(c, "alice", S["dm_alice"], tool("save_skill", name="standup", description="d",
                                                          instructions="say hi"))[-1]["content"]
    k = next(x for x in c.get(f"/api/bots/{S['bot']}/skills", headers=H("carol")).json() if x["name"] == "standup")
    assert k["path"] == "skills/standup.md"
    f = kb_read(c, "carol", k["path"])
    assert kb_write(c, "carol", k["path"], f["content"].replace("say hi", "say hello"), f["hash"]).status_code == 200
    assert "say hello" in say(c, "bob", S["dm_bob"], tool("use_skill", name="standup"))[-1]["content"]
    f = kb_read(c, "carol", k["path"])
    assert c.delete(f"/api/bots/{S['bot']}/knowledge/file", params={"path": k["path"], "base_hash": f["hash"]},
                    headers=H("carol")).status_code == 200
    assert "no such skill" in say(c, "bob", S["dm_bob"], tool("use_skill", name="standup"))[-1]["content"]


def test_project_context_prompt_preview_and_stable_prompt(c):
    # AGENTS.md lives in the knowledge base; every member may edit it and every bot gets it.
    r = kb_write(c, "carol", "AGENTS.md", "# Project\nWe ship on Fridays.")
    assert r.status_code == 200, r.text
    assert kb_read(c, "bob", "AGENTS.md")["content"].endswith("We ship on Fridays.")
    assert say(c, "bob", S["dm_bob"], "RECALL We ship on Fridays.")[-1]["content"] == "yes"
    assert say(c, "bob", S["dm_bob"], f"RECALL AGENTS.md (hash {r.json()['hash']})")[-1]["content"] == "yes"
    # a bot that fixes AGENTS.md with the hash from its prompt is seen on the next turn
    out = say(c, "bob", S["dm_bob"], tool("knowledge_patch", path="AGENTS.md", old_text="Fridays",
                                         new_text="Mondays", base_hash=r.json()["hash"]))[-1]["content"]
    assert out.startswith("RESULT: saved AGENTS.md")
    assert say(c, "bob", S["dm_bob"], "RECALL We ship on Mondays.")[-1]["content"] == "yes"
    # a secret pasted into a knowledge file is held back
    leak = kb_write(c, "carol", "notes/keys.md", "token ghp_" + "a" * 36)
    assert leak.status_code == 409 and leak.json()["detail"]["code"] == "secret"
    # the prompt preview is open to every member and shows the sections with their sizes
    prev = c.get(f"/api/bots/{S['bot']}/prompt-preview", headers=H("carol"))
    assert prev.status_code == 200, prev.text
    names = {x["name"] for x in prev.json()["sections"]}
    assert {"identity", "project_context", "team_memory", "user_profile", "knowledge_index", "setup"} <= names
    assert prev.json()["chars"] <= prev.json()["limit"]
    # one prompt per turn: the tool step and the answer after it see the same system prompt
    import mock_llm
    mock_llm.SEEN["systems"] = []
    say(c, "bob", S["dm_bob"], tool("use_skill", name="weekly-report"))
    seen = [x for x in mock_llm.SEEN["systems"] if x.startswith("You are Helper")]
    assert len(seen) == 2 and seen[0] == seen[1]
    assert mock_llm.SEEN["stamps"] > 0  # the current time travels with the newest message, not the prompt



def test_cron_in_plain_words():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.routines import describe_cron, next_run_at
    assert describe_cron("30 8 * * 1-5") == "08:30 on weekdays"
    assert describe_cron("0 16 * * 5") == "16:00 on Friday"
    assert describe_cron("0 9 1 * *") == "09:00 on day 1"
    assert describe_cron("0 0 1 1 *") == "00:00 on day 1 in January"
    assert describe_cron("*/15 * * * *") == "every 15 minutes"
    assert describe_cron("0 0 * * *") == "every day at 00:00"
    assert describe_cron("nope") == "nope"
    tz = ZoneInfo("Europe/Amsterdam")
    after = datetime(2026, 11, 13, 18, 0, tzinfo=tz)  # Friday evening
    nxt = next_run_at("30 8 * * 1-5", after=after)
    assert nxt is not None and nxt.strftime("%Y-%m-%d %H:%M %a") == "2026-11-16 08:30 Mon"


def test_routines(c):
    r = c.post(f"/api/bots/{S['bot']}/routines", headers=H("bob"), json={
        "name": "standup", "cron": "30 8 * * 1-5", "prompt": "summarise yesterday", "conversation_id": S["dm_bob"]})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/bots/{S['bot']}/routines", headers=H("bob"), json={
        "name": "bad", "cron": "nope", "prompt": "x", "conversation_id": S["dm_bob"]}).status_code == 400
    from app.routines import scheduler
    rid = r.json()["id"]
    assert scheduler.get_job(f"routine-{rid}") is not None
    assert c.post(f"/api/routines/{rid}/run", headers=H("alice")).status_code == 404
    assert c.post(f"/api/routines/{rid}/run", headers=H("bob")).status_code == 200

    def fired():
        msgs = c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("bob")).json()
        return msgs[-1]["content"] == "ECHO[mock-1]: [Routine: standup] summarise yesterday"
    wait_for(fired)
    # the bot can schedule one itself, behind the approval gate
    say(c, "bob", S["dm_bob"], tool("create_routine", name="friday", cron="0 16 * * 5", prompt="wrap up"))
    assert "scheduled" in approve(c, "bob", S["dm_bob"])[-1]["content"]
    assert len(c.get(f"/api/bots/{S['bot']}/routines", headers=H("bob")).json()) == 2
    assert c.get(f"/api/bots/{S['bot']}/routines", headers=H("alice")).json() == []

    # Cross-project list enriches the row; only the owner sees it; name/cron/prompt can be edited
    rows = c.get("/api/routines", headers=H("bob")).json()
    assert {x["name"] for x in rows} == {"standup", "friday"}
    standup = next(x for x in rows if x["name"] == "standup")
    assert standup["id"] == rid
    assert standup["cron_text"] == "08:30 on weekdays" and standup["cron"] == "30 8 * * 1-5"
    assert standup["timezone"] == "Europe/Amsterdam"
    assert standup["next_run"] and "08:30" in (standup["next_run_text"] or "")
    assert standup["bot_handle"] == "helper" and "Project" in standup["project"]
    assert "private chat" in standup["conversation_name"] and "@helper" in standup["conversation_name"]
    assert c.get("/api/routines", headers=H("alice")).json() == []
    assert c.patch(f"/api/routines/{rid}", headers=H("alice"), json={"name": "x"}).status_code == 404
    edited = c.patch(f"/api/routines/{rid}", headers=H("bob"),
                     json={"name": "morning standup", "cron": "0 9 * * 1-5", "prompt": "hello"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "morning standup" and edited.json()["cron_text"] == "09:00 on weekdays"
    assert c.patch(f"/api/routines/{rid}", headers=H("bob"), json={"cron": "nope"}).status_code == 400
    paused = c.patch(f"/api/routines/{rid}", headers=H("bob"), json={"enabled": False})
    assert paused.status_code == 200 and paused.json()["enabled"] is False and paused.json()["next_run"] is None
    resumed = c.patch(f"/api/routines/{rid}", headers=H("bob"), json={"enabled": True})
    assert resumed.status_code == 200 and resumed.json()["enabled"] is True and resumed.json()["next_run"]


def test_bot_to_bot_handoff(c):
    w = c.post(f"/api/teams/{S['team']}/bots", headers=H("alice"), json={
        "name": "Writer", "handle": "writer", "endpoint_id": S["ep"], "model": "writer-model", "is_team": True}).json()
    c.put(f"/api/bots/{w['id']}/share", headers=H("alice"), json={"published": True, "access": "selected", "members": []})
    out = say(c, "alice", S["dm_alice"], tool("ask_bot", handle="writer", message="draft a title"))[-1]["content"]
    assert "ECHO[writer-model]" in out and "draft a title" in out
    # Bob has no access to Writer, so Helper cannot reach it on his behalf
    assert "no access" in say(c, "bob", S["dm_bob"], tool("ask_bot", handle="writer", message="x"))[-1]["content"]


def test_mcp_tools(c):
    r = c.post(f"/api/bots/{S['bot']}/mcp", headers=H("alice"), json={"name": "calc", "url": MCP_URL, "trusted": True})
    assert r.json()["tools"] == ["add"], r.text
    assert "5" in say(c, "bob", S["dm_bob"], tool("mcp_calc_add", a=2, b=3))[-1]["content"]


def test_secrets_stay_outside_sandbox_and_model(c):
    r = c.post(f"/api/bots/{S['bot']}/secrets", headers=H("alice"),
               json={"name": "API_TOKEN", "value": "s3cret-value", "hosts": "127.0.0.1"})
    assert r.json() == {"id": 1, "name": "API_TOKEN", "hosts": "127.0.0.1"}
    assert "s3cret" not in c.get(f"/api/bots/{S['bot']}/secrets", headers=H("alice")).text
    # the model knows the name and the bound host, never the value
    assert say(c, "bob", S["dm_bob"], "RECALL API_TOKEN (hosts: 127.0.0.1)")[-1]["content"] == "yes"
    assert "s3cret-value" not in c.get(f"/api/bots/{S['bot']}/prompt-preview", headers=H("bob")).text
    # every member may change the bot, so typing a stored value is caught as a leak for Bob too
    r = c.post(f"/api/conversations/{S['dm_bob']}/messages", json={"content": "it is s3cret-value"}, headers=H("bob"))
    assert r.status_code == 409 and r.json()["detail"]["found"][0]["name"] == "API_TOKEN"
    # not in the sandbox
    out = say(c, "alice", S["dm_alice"], tool("run_command", command="echo token=[$API_TOKEN]"))[-1]["content"]
    assert "token=[]" in out
    # filled in outside the sandbox for the bound host; the echoed value is masked on the way back
    call = tool("http_request", method="POST", url=f"http://127.0.0.1:{LLM_PORT}/echo",
                headers={"Authorization": "Bearer {{secret:API_TOKEN}}"}, body="x={{secret:API_TOKEN}}")
    say(c, "bob", S["dm_bob"], call)
    out = approve(c, "bob", S["dm_bob"])[-1]["content"]
    assert "HTTP 200" in out and "Bearer [secret:API_TOKEN]" in out and "x=[secret:API_TOKEN]" in out
    stored = c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("bob")).json()
    assert "s3cret" not in json.dumps([m for m in stored if m["is_bot"]])  # nothing the bot wrote or received
    # another host: refused before anything is sent
    say(c, "bob", S["dm_bob"], tool("http_request", method="GET", url=f"http://localhost:{LLM_PORT}/echo",
                                    headers={"Authorization": "{{secret:API_TOKEN}}"}))
    assert "not bound to host localhost; nothing was sent" in approve(c, "bob", S["dm_bob"])[-1]["content"]
    from app import secrets_http
    assert not secrets_http._is_public("127.0.0.1") and not secrets_http._is_public("169.254.169.254")


def test_activity_feed_and_overview(c):
    feed = c.get(f"/api/teams/{S['team']}/feed", headers=H("carol")).json()
    fid = feed["id"]
    assert feed["bot_id"] is None
    assert c.get(f"/api/teams/{S['team']}/feed", headers=H("carol")).json()["id"] == fid
    # The old per-bot URL is only a compatibility alias for this project's one feed.
    assert c.get(f"/api/bots/{S['bot']}/feed", headers=H("carol")).json()["id"] == fid
    entries = c.get(f"/api/conversations/{fid}/messages", headers=H("carol")).json()
    # Carol never saw Bob's chat, but reads what the bot did for him, as a summary
    assert any(e["author_bot_id"] == S["bot"] and "for Bob: did run_command" in e["content"] for e in entries)
    assert all("hi-from-bob" not in e["content"] for e in entries)
    assert c.post(f"/api/conversations/{fid}/messages", json={"content": "x"}, headers=H("carol")).status_code == 400
    # plain chat without tools is not activity
    before = len(entries)
    say(c, "bob", S["dm_bob"], "just chatting")
    # a person can keep a chat out of the feed
    assert c.patch(f"/api/conversations/{S['dm_bob']}", json={"share_activity": False}, headers=H("alice")).status_code == 404
    assert c.patch(f"/api/conversations/{S['dm_bob']}", json={"share_activity": False}, headers=H("bob")).status_code == 200
    say(c, "bob", S["dm_bob"], tool("read_file", path="note.txt"))
    time.sleep(0.5)
    assert len(c.get(f"/api/conversations/{fid}/messages", headers=H("carol")).json()) == before
    c.patch(f"/api/conversations/{S['dm_bob']}", json={"share_activity": True}, headers=H("bob"))
    say(c, "bob", S["dm_bob"], tool("read_file", path="note.txt"))
    wait_for(lambda: len(c.get(f"/api/conversations/{fid}/messages", headers=H("carol")).json()) == before + 1)
    ov = c.get("/api/overview", headers=H("carol")).json()
    assert ov[0]["client"] == "Acme" and ov[0]["name"] == "Project X" and ov[0]["people"] == 3
    assert "for Bob: did read_file" in ov[0]["activity"][0]["content"]


def test_own_screen_and_computer_use(c):
    if shutil.which("Xvfb") is None or shutil.which("xdotool") is None or "TB_BROWSER" not in os.environ:
        pytest.skip("needs Xvfb, xdotool and a Chromium")
    b = c.post(f"/api/teams/{S['team']}/bots", headers=H("alice"), json={
        "name": "Clicker", "handle": "clicker", "endpoint_id": S["ep"], "computer_use": True}).json()
    dm = c.post(f"/api/bots/{b['id']}/dm", headers=H("alice")).json()["id"]
    try:
        # looking needs no approval; the screenshot reaches the model as an image
        msgs = say(c, "alice", dm, tool("screen_screenshot"))
        assert msgs[-1]["content"].startswith("SAW_IMAGE data:image/jpeg;base64,")
        shot = next(m for m in msgs if m["has_image"])
        img = c.get(f"/api/messages/{shot['id']}/image", headers=H("alice"))
        assert img.content[:2] == b"\xff\xd8"
        blank = len(img.content)
        # acting asks first; one "always allow" covers the whole screen group
        say(c, "alice", dm, tool("screen_open", url=f"http://127.0.0.1:{LLM_PORT}/docs"))
        assert "opened" in approve(c, "alice", dm, always=True)[-1]["content"]
        assert "done" in say(c, "alice", dm, tool("screen_click", x=640, y=400))[-1]["content"]
        assert "done" in say(c, "alice", dm, tool("screen_key", keys="Tab"))[-1]["content"]
        # people can watch the bot's screen; a page is now on it
        live = wait_for(lambda: (r := c.get(f"/api/bots/{b['id']}/screen.jpg", headers=H("alice"))).status_code == 200
                        and len(r.content) > blank * 2 and r)
        assert live.content[:2] == b"\xff\xd8"
        assert c.get(f"/api/bots/{S['bot']}/screen.jpg", headers=H("alice")).status_code == 400  # switched off
    finally:
        subprocess.run(["pkill", "-f", f"Xvfb :{100 + b['id']} "], check=False)
        subprocess.run(["pkill", "-f", f"user-data-dir=.screens/bot{b['id']}"], check=False)


def test_messages_while_the_bot_is_busy(c):
    dm = S["dm_alice"]  # Alice has "always allow" for run_command, so a slow command keeps the bot busy
    msgs = lambda: c.get(f"/api/conversations/{dm}/messages", headers=H("alice")).json()  # noqa: E731
    state = lambda: c.get(f"/api/conversations/{dm}/state", headers=H("alice")).json()  # noqa: E731

    def post(text, mode="auto"):
        r = c.post(f"/api/conversations/{dm}/messages", json={"content": text, "mode": mode}, headers=H("alice"))
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def busy_with(marker, seconds):
        post(tool("run_command", command=f"sleep {seconds}; echo {marker}"))
        assert wait_for(lambda: state()["running"])[0]["mine"]

    def settled():
        m = msgs()
        return m if not state()["running"] and m[-1]["role"] == "assistant" and not m[-1]["tool_calls"] else None

    # queue (decided by the model): answered as its own turn, after the running one
    busy_with("one-done", 1.5)
    q = post("do this later please")
    assert q in state()["queued"]
    done = wait_for(lambda: (m := settled()) and m[-1]["content"].endswith("later please") and m)
    texts = [m["content"] for m in done]
    first = next(i for i, t in enumerate(texts) if t.startswith("RESULT:") and "one-done" in t)
    assert first < texts.index("ECHO[mock-1]: do this later please")
    assert state()["queued"] == [] and state()["delivery"][str(q)] == "queue"

    # steer: the running turn reads it at its next step and answers it, instead of a second turn
    busy_with("two-done", 1.5)
    before = len(msgs())
    st = post("use blue instead", "steer")
    new = wait_for(lambda: settled() and msgs()[before - 1:])
    assert new[-1]["content"] == "ECHO[mock-1]: use blue instead" and state()["delivery"][str(st)] == "steer"
    assert sum(1 for m in new if m["role"] == "assistant" and not m["tool_calls"]) == 1

    # interrupt: a stop word ends the turn at once, and the stop button does the same without a message
    busy_with("three-done", 20)
    started = time.time()
    post("stop")
    assert wait_for(settled)[-1]["content"] == "ECHO[mock-1]: stop" and time.time() - started < 10
    busy_with("four-done", 20)
    assert c.post(f"/api/conversations/{dm}/stop", headers=H("bob")).status_code == 404  # not Bob's chat
    assert c.post(f"/api/conversations/{dm}/stop", headers=H("alice")).json() == {"stopped": 1}
    assert state()["running"] == [] and not any(m["role"] == "tool" and "four-done" in m["content"] for m in msgs())
    assert c.post(f"/api/conversations/{dm}/messages", json={"content": "x", "mode": "nope"},
                  headers=H("alice")).status_code == 400


def test_at_menu_sources(c):
    files = c.get(f"/api/teams/{S['team']}/files", headers=H("bob")).json()
    assert "note.txt" in files and all(not f.startswith(".") for f in files)
    people = c.get(f"/api/teams/{S['team']}/members", headers=H("bob")).json()
    assert {"alice", "bob", "carol"} == {p["username"] for p in people}
    outsider = c.post("/api/register", json={"username": "dave", "password": "password123"}).json()
    assert c.get(f"/api/teams/{S['team']}/files", headers={"Authorization": f"Bearer {outsider['token']}"}).status_code == 403


def test_secret_in_chat_is_held_back(c):
    dm, before = S["dm_bob"], len(c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("bob")).json())
    send = lambda who, conv, text, **kw: c.post(f"/api/conversations/{conv}/messages",  # noqa: E731
                                                json={"content": text, **kw}, headers=H(who))
    token = "ghp_" + "a1B2" * 9
    r = send("bob", dm, f"push with {token} please")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "secret"
    hit = r.json()["detail"]["found"][0]
    assert (hit["kind"], hit["value"], hit["suggest"], hit["hosts"]) == ("GitHub token", token, "GITHUB_TOKEN", "github.com,api.github.com")
    assert send("bob", dm, "my password is Hunter22!").json()["detail"]["found"][0]["value"] == "Hunter22!"
    assert len(c.get(f"/api/conversations/{dm}/messages", headers=H("bob")).json()) == before  # nothing was stored
    # ordinary text, placeholders and commit hashes pass
    for ok in ("the token is required for this", "use {{secret:API_TOKEN}} here", "see commit 4ceeefdedfb5e28842f2987bc0fe13105904c72b"):
        assert send("bob", dm, ok).status_code == 200, ok
    # a stored secret's value is recognised by name for everyone who may change the bot (any member using it)
    c.post(f"/api/bots/{S['bot']}/secrets", headers=H("alice"), json={"name": "DOOR_CODE", "value": "open-sesame", "hosts": "x.test"})
    assert send("alice", S["dm_alice"], "it is open-sesame").json()["detail"]["found"][0]["name"] == "DOOR_CODE"
    assert send("bob", dm, "it is open-sesame").json()["detail"]["found"][0]["name"] == "DOOR_CODE"
    # the person can still decide to send it
    assert send("bob", dm, f"dummy {token}", allow_secret=True).status_code == 200
    agent_idle = lambda: not c.get(f"/api/conversations/{dm}/state", headers=H("bob")).json()["running"]  # noqa: E731
    wait_for(agent_idle)


def test_project_files(c):
    S["erin"] = c.post("/api/register", json={"username": "erin", "password": "password123"}).json()
    team, other = S["team"], c.post("/api/teams", json={"name": "Other"}, headers=H("erin")).json()["id"]
    up = lambda who, t, name, data, **q: c.post(f"/api/teams/{t}/files", params={"name": name, **q},  # noqa: E731
                                                content=data, headers=H(who))
    r = up("bob", team, "Report Q3.pdf", b"%PDF-1.4 hello")
    assert r.status_code == 200 and r.json() == {"path": "uploads/Report Q3.pdf", "size": 14}
    assert up("alice", team, "Report Q3.pdf", b"v2").json()["path"] == "uploads/Report Q3 (2).pdf"  # never overwritten
    assert up("bob", team, "../../etc/passwd", b"x").json()["path"] == "uploads/passwd"  # only the name is used
    assert up("bob", team, "a.txt", b"x", folder="../x").status_code == 400
    # per project: erin is not in this project, and her own project has its own files
    assert up("erin", team, "a.txt", b"x").status_code == 403
    assert c.get(f"/api/teams/{team}/files/content", params={"path": "uploads/passwd"}, headers=H("erin")).status_code == 403
    assert up("erin", other, "own.txt", b"mine").status_code == 200
    assert [f["path"] for f in c.get(f"/api/teams/{other}/files?detail=1", headers=H("erin")).json()] == ["uploads/own.txt"]
    names = [f["path"] for f in c.get(f"/api/teams/{team}/files?detail=1", headers=H("alice")).json()]
    assert "uploads/Report Q3.pdf" in names and "uploads/own.txt" not in names
    assert c.get(f"/api/teams/{team}/files", headers=H("alice")).json() == sorted(names)
    # a bot of the project reads what was uploaded
    assert "hello" in say(c, "bob", S["dm_bob"], tool("read_file", path="uploads/Report Q3.pdf"))[-1]["content"]
    # download: the bytes as uploaded, and never rendered as a page of the app
    r = c.get(f"/api/teams/{team}/files/content", params={"path": "uploads/Report Q3.pdf"}, headers=H("alice"))
    assert r.content == b"%PDF-1.4 hello" and r.headers["content-disposition"].startswith("attachment")
    up("bob", team, "page.html", b"<script>alert(1)</script>")
    r = c.get(f"/api/teams/{team}/files/content", params={"path": "uploads/page.html", "download": "0"}, headers=H("bob"))
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-security-policy"] == "sandbox"
    assert c.get(f"/api/teams/{team}/files/content", params={"path": "../teambots.db"}, headers=H("bob")).status_code == 400
    # a file with a credential in it is held back, unless the person sends it anyway
    leak = b"GITHUB_TOKEN=ghp_" + b"z7Q1" * 9
    r = up("bob", team, ".env.txt", leak)
    assert r.status_code == 409 and r.json()["detail"]["found"][0]["kind"] == "GitHub token"
    assert up("bob", team, "env.txt", leak, allow_secret="true").status_code == 200
    # size limit, and delete by any project member
    assert up("bob", team, "big.bin", b"0" * (25 * 1024 * 1024 + 1)).status_code == 413
    assert c.delete(f"/api/teams/{team}/files", params={"path": "uploads/env.txt"}, headers=H("alice")).json() == {"ok": True}
    assert c.delete(f"/api/teams/{team}/files", params={"path": "uploads/env.txt"}, headers=H("alice")).status_code == 404


def test_renaming(c):
    team, bot = S["team"], S["bot"]
    assert c.patch(f"/api/conversations/{S['ch']}", json={"name": "lobby"}, headers=H("carol")).status_code == 404
    assert c.patch(f"/api/conversations/{S['ch']}", json={"name": "lobby"}, headers=H("bob")).json()["name"] == "lobby"
    assert c.patch(f"/api/conversations/{S['dm_bob']}", json={"name": "x"}, headers=H("bob")).status_code == 400
    assert c.patch(f"/api/teams/{team}", json={"name": "Project Y"}, headers=H("bob")).status_code == 403
    assert c.patch(f"/api/teams/{team}", json={"name": "Project Y", "client": "Acme BV"}, headers=H("alice")).status_code == 200
    assert [(t["name"], t["client"]) for t in c.get("/api/teams", headers=H("bob")).json()] == [("Project Y", "Acme BV")]
    assert c.patch(f"/api/bots/{bot}", json={"name": "Buddy", "handle": "Bad Handle"}, headers=H("alice")).status_code == 400
    assert c.patch(f"/api/bots/{bot}", json={"name": "Buddy", "handle": "buddy"}, headers=H("alice")).json()["handle"] == "buddy"
    assert c.post(f"/api/bots/{bot}/dm", headers=H("bob")).json()["name"] == "Buddy"  # chats follow the new name
    assert say(c, "bob", S["ch"], "@buddy hello")[-1]["content"].endswith("hello")
    assert c.patch(f"/api/endpoints/{S['ep']}", json={"name": "mock-2"}, headers=H("bob")).status_code == 404
    ep = c.patch(f"/api/endpoints/{S['ep']}", json={"name": "mock-2"}, headers=H("alice")).json()
    assert ep["name"] == "mock-2" and ep["has_key"]  # the stored key is kept
    assert c.post(f"/api/endpoints/{S['ep']}/test", headers=H("alice")).json()["tool_calling"]


def test_endpoint_failure_is_reported(c):
    ep = c.post(f"/api/teams/{S['team']}/endpoints", headers=H("alice"), json={
        "name": "down", "base_url": "http://127.0.0.1:9", "default_model": "m"}).json()
    b = c.post(f"/api/teams/{S['team']}/bots", headers=H("alice"), json={
        "name": "Broken", "handle": "broken", "endpoint_id": ep["id"]}).json()
    dm = c.post(f"/api/bots/{b['id']}/dm", headers=H("alice")).json()["id"]
    assert say(c, "alice", dm, "hi")[-1]["content"].startswith("Endpoint error:")
    assert c.delete(f"/api/bots/{b['id']}", headers=H("bob")).status_code == 404
    assert c.delete(f"/api/bots/{b['id']}", headers=H("alice")).status_code == 200


def test_home_overview_bot(c):
    """One overview bot per user, own sandbox, can list projects and ask a project bot."""
    # no endpoint yet? Alice already has one. Created on first GET /home.
    r = c.get("/api/home", headers=H("alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bot"]["is_home"] and body["bot"]["handle"] == "home" and body["conversation"]["kind"] == "home"
    S["home"] = body["conversation"]["id"]
    S["home_bot"] = body["bot"]["id"]
    assert body["endpoint_id"] == S["ep"]
    # not a project bot, and cannot be deleted
    assert all(b["id"] != S["home_bot"] for b in c.get(f"/api/teams/{S['team']}/bots", headers=H("alice")).json())
    assert c.delete(f"/api/bots/{S['home_bot']}", headers=H("alice")).status_code == 400
    # Bob has no endpoint of his own, so Overview is not created yet
    assert c.get("/api/home", headers=H("bob")).status_code == 400
    assert c.patch(f"/api/endpoints/{S['ep']}", json={"shared": True}, headers=H("alice")).status_code == 200
    bob_home = c.get("/api/home", headers=H("bob")).json()
    assert bob_home["conversation"]["id"] != S["home"] and bob_home["bot"]["id"] != S["home_bot"]
    assert c.get(f"/api/conversations/{S['home']}/messages", headers=H("bob")).status_code == 404
    # lists projects and the team bot (renamed to Buddy in an earlier test)
    msgs = say(c, "alice", S["home"], tool("list_workspace"))
    tools = " ".join(m["content"] for m in msgs if m["role"] == "tool")
    assert "Project Y" in tools and "@buddy" in tools
    # ask_bot reaches the project bot and keeps the one-level guard
    msgs = say(c, "alice", S["home"], tool("ask_bot", handle="buddy", message="draft a title from overview"))
    tools = " ".join(m["content"] for m in msgs if m["role"] == "tool")
    assert "ECHO[mock-1]" in tools and "draft a title from overview" in tools
    # own sandbox, not the project computer
    assert c.patch(f"/api/bots/{S['home_bot']}", json={"approval_mode": "auto"}, headers=H("alice")).status_code == 200
    say(c, "alice", S["home"], tool("run_command", command="echo home-only > marker-home.txt"))
    uid = S["alice"]["user"]["id"]
    home_file = os.path.join(TMP, "workspaces", f"home-u{uid}", "marker-home.txt")
    proj_file = os.path.join(TMP, "workspaces", f"p{S['team']}", "marker-home.txt")
    assert open(home_file).read().strip() == "home-only"
    assert not os.path.exists(proj_file)
    S["home_bob"] = bob_home["conversation"]["id"]


def test_home_manages_routines(c):
    """The overview bot can list and manage the caller's routines across projects; not anyone else's."""
    rows = c.get("/api/routines", headers=H("bob")).json()
    assert rows, "expected Bob's routines from the earlier test"
    standup = next(x for x in rows if "standup" in x["name"])
    friday = next(x for x in rows if x["name"] == "friday")
    tools = lambda msgs: " ".join(m["content"] for m in msgs if m["role"] == "tool")  # noqa: E731
    listed = say(c, "bob", S["home_bob"], tool("list_routines"))
    text = tools(listed)
    assert f"#{standup['id']}" in text and "morning standup" in text
    assert "09:00 on weekdays" in text
    assert "@helper" in text or "@buddy" in text
    paused = say(c, "bob", S["home_bob"], tool("update_routine", routine_id=standup["id"], enabled=False))
    assert "paused" in tools(paused).lower()
    assert next(x["enabled"] for x in c.get("/api/routines", headers=H("bob")).json() if x["id"] == standup["id"]) is False
    # Alice cannot see or change Bob's routine through her overview bot
    alice_list = tools(say(c, "alice", S["home"], tool("list_routines")))
    assert f"#{standup['id']}" not in alice_list and "You have no routines" in alice_list
    denied = say(c, "alice", S["home"], tool("delete_routine", routine_id=standup["id"]))
    assert "not found" in tools(denied).lower()
    assert any(x["id"] == standup["id"] for x in c.get("/api/routines", headers=H("bob")).json())
    deleted = say(c, "bob", S["home_bob"], tool("delete_routine", routine_id=friday["id"]))
    assert "Deleted" in tools(deleted) and "friday" in tools(deleted)
    left = c.get("/api/routines", headers=H("bob")).json()
    assert all(x["id"] != friday["id"] for x in left) and any(x["id"] == standup["id"] for x in left)
    ran = say(c, "bob", S["home_bob"], tool("run_routine", routine_id=standup["id"]))
    assert "Started" in tools(ran)


def test_behind_reverse_proxy(c):
    """X-Forwarded-Proto/Host is honoured so cookies get the Secure flag on HTTPS front doors."""
    from app.api import _cookie_flags
    from app.main import app
    from starlette.requests import Request
    assert any(getattr(m, "cls", type(None)).__name__ == "ProxyHeadersMiddleware" for m in app.user_middleware)

    def req(scheme, headers=None):
        hdrs = []
        for k, v in (headers or {}).items():
            hdrs.append((k.lower().encode(), v.encode()))
        return Request({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                        "scheme": scheme, "path": "/", "raw_path": b"/", "query_string": b"",
                        "headers": hdrs, "client": ("127.0.0.1", 1), "server": ("test", 80)})

    assert _cookie_flags(req("http"))["secure"] is False
    assert _cookie_flags(req("https"))["secure"] is True
    assert _cookie_flags(req("http", {"x-forwarded-proto": "https"}))["secure"] is True
    assert _cookie_flags(None)["secure"] is False

    # The real app, wrapped in ProxyHeadersMiddleware, reports https from the forwarded header.
    host = "teamsbot-extnode-3ez6v0lng26hyupqse5n0kb61n9.gobrev.dev"
    r = c.get("/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": host, "Host": host})
    assert r.status_code == 200
    # computer use is off on the team bot, so this stays a 400 — but auth and routing still work over the forwarded host
    vnc = c.get(f"/api/bots/{S['bot']}/screen/vnc", headers={**H("alice"), "X-Forwarded-Proto": "https",
                                                             "X-Forwarded-Host": host}, follow_redirects=False)
    assert vnc.status_code == 400


def test_home_voice_api_tokens(c):
    """Personal API tokens for voice: hashed, shown once, revoked tokens and session tokens are rejected."""
    assert c.post("/api/v1/home/message", json={"text": "hi"}).status_code == 401
    assert c.post("/api/v1/home/message", json={"text": "hi"}, headers=H("alice")).status_code == 401
    r = c.post("/api/tokens", headers=H("alice"), json={"name": "test-voice", "scope": "home-bot"})
    assert r.status_code == 200, r.text
    raw, tid, prefix = r.json()["token"], r.json()["id"], r.json()["prefix"]
    assert raw.startswith("tbh_") and prefix == raw[:11]
    listed = c.get("/api/tokens", headers=H("alice")).json()
    assert any(t["id"] == tid and "token" not in t and t["prefix"] == prefix for t in listed)
    Hapi = {"Authorization": f"Bearer {raw}"}
    r = c.post("/api/v1/home/message", json={"text": "hello from voice", "mode": "voice"}, headers=Hapi)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conversation_id"] == S["home"]
    assert "ECHO[mock-1]" in body["reply"] and "hello from voice" in body["reply"]
    # the same turn is visible in the Overview chat
    chat = c.get(f"/api/conversations/{S['home']}/messages", headers=H("alice")).json()
    assert any(m["role"] == "user" and m["content"] == "hello from voice" for m in chat)
    assert any(m["role"] == "assistant" and "hello from voice" in (m["content"] or "") for m in chat)
    st = c.get("/api/v1/home/status", headers=Hapi)
    assert st.status_code == 200 and "Project Y" in st.json()["overview"] and st.json()["conversation_id"] == S["home"]
    schema = c.get("/api/v1/home/openapi", headers=Hapi)
    assert schema.status_code == 200 and schema.json()["openai"]["model"] == "teambots-home"
    assert c.get("/api/v1/home/openapi").status_code == 401
    oa = c.post("/v1/chat/completions", headers=Hapi, json={
        "model": "teambots-home", "messages": [{"role": "user", "content": "ping the overview"}]})
    assert oa.status_code == 200, oa.text
    assert "ECHO[mock-1]" in oa.json()["choices"][0]["message"]["content"]
    assert oa.json()["conversation_id"] == S["home"]
    models = c.get("/v1/models", headers=Hapi)
    assert models.status_code == 200 and {m["id"] for m in models.json()["data"]} >= {"teambots-home", "teambots-home-voice"}
    assert c.delete(f"/api/tokens/{tid}", headers=H("alice")).status_code == 200
    assert c.post("/api/v1/home/message", json={"text": "again"}, headers=Hapi).status_code == 401
    # a already-revoked token stays rejected; a typo token is rejected
    assert c.post("/api/v1/home/message", json={"text": "x"}, headers={"Authorization": "Bearer tbh_not-a-real-token"}).status_code == 401


def test_cost_tracking_summary(c):
    """Mock usage is logged and totals/budget warnings show up. Does not touch a real model."""
    from datetime import datetime, timezone
    from app.db import LlmUsage, session

    h = H("alice")
    # Alice already admins a project; she can open Costs.
    me = c.get("/api/me", headers=h).json()
    assert me["can_view_costs"] is True
    with session() as s:
        s.add(LlmUsage(
            created_at=datetime.now(timezone.utc), user_id=S["alice"]["user"]["id"], team_id=S["team"],
            provider="github_copilot", model="claude-sonnet-5",
            prompt_tokens=1_000_000, completion_tokens=500_000, success=True, origin="chat",
            billing_mode="token", cost_usd=7.0, cost_unknown=False, notes="test"))
        s.add(LlmUsage(
            created_at=datetime.now(timezone.utc), user_id=S["alice"]["user"]["id"], team_id=S["team"],
            provider="github_copilot", model="gpt-4-o-preview",
            prompt_tokens=100, completion_tokens=20, success=True, origin="routine",
            billing_mode="token", cost_usd=None, cost_unknown=True, notes="price unknown"))
        s.commit()
    body = c.get("/api/costs?period=today", headers=h).json()
    assert body["totals"]["calls"] >= 2
    assert body["totals"]["cost_usd"] >= 7.0
    assert body["totals"]["unknown_calls"] >= 1
    origins = {r["origin"] for r in body["by_origin"]}
    assert "chat" in origins and "routine" in origins
    assert any(r["model"] == "claude-sonnet-5" for r in body["by_model"])
    prices = c.get("/api/costs/prices", headers=h)
    assert prices.status_code == 200
    assert any(p["model"] == "claude-sonnet-5" and p["provider"] == "github_copilot"
               for p in prices.json()["prices"])
    assert c.patch(f"/api/teams/{S['team']}", headers=h,
                   json={"monthly_budget": 1.0, "budget_currency": "USD"}).status_code == 200
    lab = next(b for b in c.get("/api/costs?period=today", headers=h).json()["budgets"] if b["team_id"] == S["team"])
    assert lab["level"] in ("warn", "over") and lab["block"] is False
    # Someone with no project and no bot cannot open Costs.
    outsider = c.post("/api/register", json={"username": "cost_outsider", "password": "password123",
                                             "display_name": "Cost Outsider"})
    assert outsider.status_code == 200, outsider.text
    assert c.get("/api/costs?period=today", headers={"Authorization": f"Bearer {outsider.json()['token']}"}).status_code == 403
    # A connection test against the mock endpoint leaves a usage row with origin=test.
    before = body["totals"]["calls"]
    assert c.post(f"/api/endpoints/{S['ep']}/test", headers=h).status_code == 200
    later = c.get("/api/costs?period=today", headers=h).json()
    assert later["totals"]["calls"] >= before
    assert any(r["origin"] == "test" for r in later["by_origin"])


def test_router_picks_local_and_heavy_and_labels_the_turn(c):
    """A Router endpoint on the mock LLM chooses models by rule and stores the route on the message."""
    h = H("alice")
    # An earlier test set a $1 budget and already spent more; raise it so routing is not forced cheap.
    assert c.patch(f"/api/teams/{S['team']}", headers=h, json={"monthly_budget": 100000, "budget_currency": "USD"}).status_code == 200
    rules = {
        "routes": [
            {"name": "local", "endpoint_id": S["ep"], "model": "spark-model"},
            {"name": "cheap", "endpoint_id": S["ep"], "model": "haiku-model"},
            {"name": "normal", "endpoint_id": S["ep"], "model": "sonnet-model"},
            {"name": "heavy", "endpoint_id": S["ep"], "model": "opus-model"},
        ],
        "policy": {
            "privacy": ["local"],
            "simple": ["local", "cheap"],
            "normal": ["normal", "cheap", "local"],
            "heavy": ["heavy", "normal", "cheap"],
            "budget_80": ["local", "cheap"],
            "budget_100_block": ["local"],
        },
        "signals": {"long_context_tokens": 32000, "short_chars": 80, "hard_keywords": ["think hard"]},
    }
    defaults = c.get(f"/api/teams/{S['team']}/router-defaults", headers=h)
    assert defaults.status_code == 200, defaults.text
    assert any(r["name"] == "local" for r in defaults.json()["routes"])
    ep = c.post(f"/api/teams/{S['team']}/endpoints", headers=h, json={
        "name": "Router", "provider": "router", "default_model": "auto", "shared": True,
        "rules_json": json.dumps(rules)}).json()
    S["router_ep"] = ep["id"]
    assert ep["provider"] == "router" and ep["rules"]["routes"][0]["name"] == "local"
    # Bad rules are rejected.
    bad = c.patch(f"/api/endpoints/{ep['id']}", headers=h, json={"rules_json": json.dumps({
        "routes": [{"name": "local", "endpoint_id": S["ep"], "model": "x"}],
        "policy": {"privacy": ["missing"], "simple": ["local"], "normal": ["local"],
                   "heavy": ["local"], "budget_80": ["local"], "budget_100_block": ["local"]}})})
    assert bad.status_code == 400
    bot = c.post(f"/api/teams/{S['team']}/bots", headers=h, json={
        "name": "Router Bot", "handle": "routerbot", "endpoint_id": ep["id"],
        "approval_mode": "auto", "instructions": "You are brief."}).json()
    dm = c.post(f"/api/bots/{bot['id']}/dm", headers=h).json()["id"]
    msgs = say(c, "alice", dm, "Hi")
    ans = [m for m in msgs if m["role"] == "assistant"][-1]
    assert "ECHO[spark-model]" in (ans["content"] or "")
    assert ans["route"].startswith("local")
    msgs = say(c, "alice", dm, "Please think hard about this design of a system.")
    ans = [m for m in msgs if m["role"] == "assistant"][-1]
    assert "ECHO[opus-model]" in (ans["content"] or "")
    assert ans["route"].startswith("heavy")
    # local-only wins over a heavy keyword
    assert c.patch(f"/api/bots/{bot['id']}", headers=h, json={"local_only": True}).status_code == 200
    msgs = say(c, "alice", dm, "Please think hard about this design of a system again.")
    ans = [m for m in msgs if m["role"] == "assistant"][-1]
    assert "ECHO[spark-model]" in (ans["content"] or "")
    assert ans["route"].startswith("local")
    costs = c.get("/api/costs?period=today", headers=h).json()
    routes = {r["route"] for r in costs.get("by_route") or []}
    assert "local" in routes and "heavy" in routes
