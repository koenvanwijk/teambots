"""MCP server card: bot-level events after add/update/delete, and the list CSS contract."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from sqlmodel import create_engine

TMP = tempfile.mkdtemp(prefix="teambots-mcpevents-")
os.environ.setdefault("TB_DATA_DIR", TMP)
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import Ctx, execute_tool  # noqa: E402
from app.events import publish_bot, subscribe_bot, unsubscribe_bot  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def c():
    import app.db as db
    import app.home as home
    saved_db, saved_home = db.engine, home.engine
    eng = create_engine(f"sqlite:///{TMP}/events.db", connect_args={"check_same_thread": False})
    db.engine = home.engine = eng
    db.init_db()
    with TestClient(app) as client:
        yield client
    db.engine = saved_db
    home.engine = saved_home
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(scope="module")
def auth(c):
    r = c.post("/api/register", json={"username": "mcpcard", "password": "password123",
                                      "display_name": "Owner"})
    assert r.status_code == 200, r.text
    user = r.json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    team = c.post("/api/teams", json={"name": "Card Project"}, headers=headers).json()
    ep = c.post(f"/api/teams/{team['id']}/endpoints", headers=headers, json={
        "name": "mock", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-x",
        "default_model": "mock-1"}).json()
    bot = c.post(f"/api/teams/{team['id']}/bots", headers=headers, json={
        "name": "Helper", "handle": "helper", "endpoint_id": ep["id"], "is_team": True,
        "instructions": "help"}).json()
    me = c.get("/api/me", headers=headers).json()
    return {"headers": headers, "team_id": team["id"], "bot_id": bot["id"], "user_id": me["id"]}


def _wait(q, timeout=2):
    return asyncio.run(asyncio.wait_for(q.get(), timeout))


def test_publish_bot_reaches_subscriber():
    q = subscribe_bot(4242)
    try:
        publish_bot(4242, "mcp", action="add", id=7)
        ev = _wait(q)
        assert ev == {"kind": "mcp", "action": "add", "id": 7}
    finally:
        unsubscribe_bot(4242, q)


def test_publish_bot_is_isolated_per_bot():
    qa, qb = subscribe_bot(1), subscribe_bot(2)
    try:
        publish_bot(1, "mcp", action="add", id=11)
        ev = _wait(qa)
        assert ev["id"] == 11
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(qb.get(), 0.2))
    finally:
        unsubscribe_bot(1, qa)
        unsubscribe_bot(2, qb)


def test_add_patch_delete_mcp_publish_bot_events(c, auth):
    bot_id = auth["bot_id"]
    q = subscribe_bot(bot_id)
    try:
        r = c.post(f"/api/bots/{bot_id}/mcp", headers=auth["headers"], json={
            "name": "homey", "url": "https://mcp.athom.com", "trusted": False})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        ev = _wait(q)
        assert ev["kind"] == "mcp" and ev["action"] == "add" and ev["id"] == sid

        r = c.patch(f"/api/mcp/{sid}", headers=auth["headers"], json={"trusted": True})
        assert r.status_code == 200, r.text
        ev = _wait(q)
        assert ev["kind"] == "mcp" and ev["action"] == "update" and ev["id"] == sid

        r = c.delete(f"/api/mcp/{sid}", headers=auth["headers"])
        assert r.status_code == 200, r.text
        ev = _wait(q)
        assert ev["kind"] == "mcp" and ev["action"] == "delete" and ev["id"] == sid
    finally:
        unsubscribe_bot(bot_id, q)


def test_add_mcp_server_tool_publishes_bot_event(c, auth):
    bot_id = auth["bot_id"]
    q = subscribe_bot(bot_id)
    try:
        ctx = Ctx(bot_id=bot_id, requester_id=auth["user_id"], conv_id=0,
                  is_channel=False, team_id=auth["team_id"])
        out = asyncio.run(execute_tool(ctx, "add_mcp_server", {
            "name": "via-tool", "url": "https://example.com/mcp",
        }, {}))
        assert "error:" not in out, out
        ev = _wait(q)
        assert ev["kind"] == "mcp" and ev["action"] == "add"
        listing = c.get(f"/api/bots/{bot_id}/mcp", headers=auth["headers"])
        assert listing.status_code == 200
        names = [s["name"] for s in listing.json()]
        assert "via-tool" in names
        assert ev["id"] in {s["id"] for s in listing.json()}
    finally:
        unsubscribe_bot(bot_id, q)


def test_bot_events_endpoint_requires_access(c, auth):
    r = c.get(f"/api/bots/{auth['bot_id']}/events")
    assert r.status_code in (401, 403)
    # The body is an infinite SSE stream; only read the headers.
    with c.stream("GET", f"/api/bots/{auth['bot_id']}/events", headers=auth["headers"]) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def test_li_css_wraps_by_word_and_keeps_a_readable_min_width():
    css = (ROOT / "app/static/app.css").read_text()
    start = css.index(".li {")
    chunk = css[start:css.index(".none {", start)]
    assert "flex-wrap:wrap" in chunk
    assert "overflow-wrap:break-word" in chunk
    assert "overflow-wrap:anywhere" not in chunk
    assert ".li-act" in chunk
    assert "min-width:min(12em, 100%)" in chunk


def test_drawer_subscribes_to_bot_events_and_sets_title_tooltips():
    js = (ROOT / "app/static/app.js").read_text()
    assert "function listenBotEvents" in js
    assert "/bots/${bot.id}/events" in js
    assert "data.kind !== 'mcp'" in js
    assert "className: 'li-act'" in js
    assert "title: typeof text === 'string' ? text : undefined" in js
