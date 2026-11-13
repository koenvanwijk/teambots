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

LLM_URL = "http://127.0.0.1:8791/v1"
MCP_URL = "http://127.0.0.1:8792/mcp"


@pytest.fixture(scope="module")
def c():
    server = uvicorn.Server(uvicorn.Config(mock_llm.app, host="127.0.0.1", port=8791, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    mcp_proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "mock_mcp.py")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
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
    # collaborators cannot change the bot or see its secrets
    assert c.patch(f"/api/bots/{bot['id']}", json={"instructions": "x"}, headers=H("bob")).status_code == 403
    assert c.get(f"/api/bots/{bot['id']}/secrets", headers=H("bob")).status_code == 403


def test_private_chats(c):
    for who in ("alice", "bob", "carol"):
        S[f"dm_{who}"] = c.post(f"/api/bots/{S['bot']}/dm", headers=H(who)).json()["id"]
    msgs = say(c, "bob", S["dm_bob"], "hello there")
    assert msgs[-1]["content"] == "ECHO[mock-1]: hello there"
    # nobody else can read Bob's chat, not even the bot's owner
    assert c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("alice")).status_code == 404
    assert c.post(f"/api/conversations/{S['dm_bob']}/messages", json={"content": "x"},
                  headers=H("alice")).status_code == 404


def test_memory_personal_and_team_learning_review(c):
    msgs = say(c, "bob", S["dm_bob"], tool("remember", content="deploys happen on tuesdays", scope="team"))
    assert "proposed as a team learning" in msgs[-1]["content"]
    assert say(c, "carol", S["dm_carol"], "RECALL deploys happen on tuesdays")[-1]["content"] == "no"
    pending = [m for m in c.get(f"/api/bots/{S['bot']}/memories", headers=H("alice")).json() if m["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["proposed_by"] == "Bob"
    assert c.post(f"/api/memories/{pending[0]['id']}/review", json={"approve": True}, headers=H("carol")).status_code == 403
    assert c.post(f"/api/memories/{pending[0]['id']}/review", json={"approve": True}, headers=H("alice")).status_code == 200
    assert say(c, "carol", S["dm_carol"], "RECALL deploys happen on tuesdays")[-1]["content"] == "yes"

    say(c, "bob", S["dm_bob"], tool("remember", content="bob prefers short answers", scope="personal"))
    assert say(c, "bob", S["dm_bob"], "RECALL bob prefers short answers")[-1]["content"] == "yes"
    assert say(c, "alice", S["dm_alice"], "RECALL bob prefers short answers")[-1]["content"] == "no"
    assert all("bob prefers" not in m["content"]
               for m in c.get(f"/api/bots/{S['bot']}/memories", headers=H("alice")).json())


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
    args = dict(name="weekly-report", description="Compile the weekly report", instructions="1. collect 2. write")
    assert "only the owner or a manager" in say(c, "bob", S["dm_bob"], tool("save_skill", **args))[-1]["content"]
    assert "saved" in say(c, "alice", S["dm_alice"], tool("save_skill", **args))[-1]["content"]
    assert say(c, "bob", S["dm_bob"], "RECALL weekly-report: Compile the weekly report")[-1]["content"] == "yes"
    assert "1. collect" in say(c, "bob", S["dm_bob"], tool("use_skill", name="weekly-report"))[-1]["content"]


def test_routines(c):
    r = c.post(f"/api/bots/{S['bot']}/routines", headers=H("bob"), json={
        "name": "standup", "cron": "30 8 * * 1-5", "prompt": "summarise yesterday", "conversation_id": S["dm_bob"]})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/bots/{S['bot']}/routines", headers=H("bob"), json={
        "name": "bad", "cron": "nope", "prompt": "x", "conversation_id": S["dm_bob"]}).status_code == 400
    from app.routines import scheduler
    assert scheduler.get_job(f"routine-{r.json()['id']}") is not None
    assert c.post(f"/api/routines/{r.json()['id']}/run", headers=H("alice")).status_code == 404
    assert c.post(f"/api/routines/{r.json()['id']}/run", headers=H("bob")).status_code == 200

    def fired():
        msgs = c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("bob")).json()
        return msgs[-1]["content"] == "ECHO[mock-1]: [Routine: standup] summarise yesterday"
    wait_for(fired)
    # the bot can schedule one itself, behind the approval gate
    say(c, "bob", S["dm_bob"], tool("create_routine", name="friday", cron="0 16 * * 5", prompt="wrap up"))
    assert "scheduled" in approve(c, "bob", S["dm_bob"])[-1]["content"]
    assert len(c.get(f"/api/bots/{S['bot']}/routines", headers=H("bob")).json()) == 2
    assert c.get(f"/api/bots/{S['bot']}/routines", headers=H("alice")).json() == []


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
    assert say(c, "bob", S["dm_bob"], "RECALL s3cret-value")[-1]["content"] == "no"
    # not in the sandbox
    out = say(c, "alice", S["dm_alice"], tool("run_command", command="echo token=[$API_TOKEN]"))[-1]["content"]
    assert "token=[]" in out
    # filled in outside the sandbox for the bound host; the echoed value is masked on the way back
    call = tool("http_request", method="POST", url="http://127.0.0.1:8791/echo",
                headers={"Authorization": "Bearer {{secret:API_TOKEN}}"}, body="x={{secret:API_TOKEN}}")
    say(c, "bob", S["dm_bob"], call)
    out = approve(c, "bob", S["dm_bob"])[-1]["content"]
    assert "HTTP 200" in out and "Bearer [secret:API_TOKEN]" in out and "x=[secret:API_TOKEN]" in out
    stored = c.get(f"/api/conversations/{S['dm_bob']}/messages", headers=H("bob")).json()
    assert "s3cret" not in json.dumps([m for m in stored if m["is_bot"]])  # nothing the bot wrote or received
    # another host: refused before anything is sent
    say(c, "bob", S["dm_bob"], tool("http_request", method="GET", url="http://localhost:8791/echo",
                                    headers={"Authorization": "{{secret:API_TOKEN}}"}))
    assert "not bound to host localhost; nothing was sent" in approve(c, "bob", S["dm_bob"])[-1]["content"]
    from app import secrets_http
    assert not secrets_http._is_public("127.0.0.1") and not secrets_http._is_public("169.254.169.254")


def test_activity_feed_and_overview(c):
    fid = c.get(f"/api/bots/{S['bot']}/feed", headers=H("carol")).json()["id"]
    entries = c.get(f"/api/conversations/{fid}/messages", headers=H("carol")).json()
    # Carol never saw Bob's chat, but reads what the bot did for him, as a summary
    assert any(e["content"] == "For Bob: did run_command" for e in entries)
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
    assert ov[0]["activity"][0]["content"] == "For Bob: did read_file"


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
        say(c, "alice", dm, tool("screen_open", url="http://127.0.0.1:8791/docs"))
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
