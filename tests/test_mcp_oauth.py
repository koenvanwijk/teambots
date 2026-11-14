"""MCP OAuth: pre-registered client, scopes, add_mcp_server tool, PATCH; secrets never leak."""
import asyncio
import json
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import create_engine, select

TMP = tempfile.mkdtemp(prefix="teambots-mcpoauth-")
os.environ.setdefault("TB_DATA_DIR", TMP)
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, mcp_oauth  # noqa: E402
from app.agent import BUILTIN_TOOLS, Ctx, execute_tool  # noqa: E402
from app.db import McpServer, session  # noqa: E402
from app.main import app  # noqa: E402


SECRET = "super-secret-mcp-client"


@pytest.fixture(scope="module")
def c():
    import app.db as db
    import app.home as home
    saved_db, saved_home = db.engine, home.engine
    eng = create_engine(f"sqlite:///{TMP}/oauth.db", connect_args={"check_same_thread": False})
    db.engine = home.engine = eng
    db.init_db()
    saved_url = config.PUBLIC_URL
    config.PUBLIC_URL = "https://bots.example.com"
    with TestClient(app) as client:
        yield client
    config.PUBLIC_URL = saved_url
    db.engine = saved_db
    home.engine = saved_home


@pytest.fixture(scope="module")
def auth(c):
    r = c.post("/api/register", json={"username": "mcpowner", "password": "password123",
                                      "display_name": "Owner"})
    assert r.status_code == 200, r.text
    user = r.json()
    headers = {"Authorization": f"Bearer {user['token']}"}
    team = c.post("/api/teams", json={"name": "OAuth Project"}, headers=headers).json()
    ep = c.post(f"/api/teams/{team['id']}/endpoints", headers=headers, json={
        "name": "mock", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-x",
        "default_model": "mock-1"}).json()
    bot = c.post(f"/api/teams/{team['id']}/bots", headers=headers, json={
        "name": "Helper", "handle": "helper", "endpoint_id": ep["id"], "is_team": True,
        "instructions": "help"}).json()
    me = c.get("/api/me", headers=headers).json()
    return {"headers": headers, "team_id": team["id"], "bot_id": bot["id"], "user_id": me["id"]}


def test_normalize_scopes_list_and_string():
    assert mcp_oauth.normalize_scopes(["homey", "offline"]) == "homey offline"
    assert mcp_oauth.normalize_scopes("homey offline") == "homey offline"
    assert mcp_oauth.normalize_scopes("homey, offline,  extra") == "homey offline extra"
    assert mcp_oauth.normalize_scopes("") == ""
    assert mcp_oauth.normalize_scopes(None) == ""
    assert mcp_oauth.normalize_scopes(["", " a ", "b"]) == "a b"


def test_add_mcp_server_tool_schema_has_oauth_fields():
    tool = next(t for t in BUILTIN_TOOLS if t["function"]["name"] == "add_mcp_server")
    props = tool["function"]["parameters"]["properties"]
    for key in ("client_id", "client_secret", "scopes", "authorize_url", "token_url"):
        assert key in props, key
    required = tool["function"]["parameters"]["required"]
    assert "name" in required and "url" in required
    for key in ("client_id", "client_secret", "scopes", "authorize_url", "token_url"):
        assert key not in required


def test_build_authorize_url_uses_preregistered_client_and_scopes(c, auth):
    headers, bot_id = auth["headers"], auth["bot_id"]
    r = c.post(f"/api/bots/{bot_id}/mcp", headers=headers, json={
        "name": "homeyauth", "url": "https://api.athom.com/mcp", "auth_mode": "oauth",
        "oauth_authorize_url": "https://accounts.athom.com/oauth2/authorise",
        "oauth_token_url": "https://api.athom.com/oauth2/token",
        "oauth_client_id": "pre-registered-id",
        "oauth_client_secret": SECRET,
        "oauth_scopes": ["homey.system", "offline"],
    })
    assert r.status_code == 200, r.text
    created = r.json()
    assert SECRET not in json.dumps(created)
    assert "oauth_client_secret" not in created
    sid = created["id"]
    listing = c.get(f"/api/bots/{bot_id}/mcp", headers=headers)
    assert listing.status_code == 200, listing.text
    body = next(x for x in listing.json() if x["id"] == sid)
    dump = json.dumps(body)
    assert SECRET not in dump
    assert "oauth_client_secret" not in body
    assert body["oauth_configured"] is True
    assert body["has_oauth_secret"] is True
    assert body["oauth_client_id"] == "pre-registered-id"
    assert body["oauth_scopes"] == "homey.system offline"
    assert body["auth_mode"] == "oauth" and body["auth_scope"] == "user"

    start = c.get(f"/api/mcp/{sid}/oauth/start", headers=headers, follow_redirects=False)
    assert start.status_code == 200, start.text
    url = start.json()["url"]
    assert url.startswith("https://accounts.athom.com/oauth2/authorise")
    qs = parse_qs(urlparse(url).query)
    assert qs["client_id"] == ["pre-registered-id"]
    assert qs["scope"] == ["homey.system offline"]
    assert qs["response_type"] == ["code"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["redirect_uri"] == ["https://bots.example.com/api/mcp/oauth/callback"]
    assert "client_secret" not in qs
    assert SECRET not in url


def test_patch_keeps_secret_when_empty_and_never_returns_it(c, auth):
    headers, bot_id = auth["headers"], auth["bot_id"]
    created = c.post(f"/api/bots/{bot_id}/mcp", headers=headers, json={
        "name": "patchme", "url": "https://example.com/mcp", "auth_mode": "oauth",
        "oauth_authorize_url": "https://auth.example.com/authorize",
        "oauth_token_url": "https://auth.example.com/token",
        "oauth_client_id": "id-1",
        "oauth_client_secret": SECRET,
        "oauth_scopes": "read",
    }).json()
    sid = created["id"]
    patched = c.patch(f"/api/mcp/{sid}", headers=headers, json={
        "oauth_client_id": "id-2",
        "oauth_scopes": "read write",
        "oauth_client_secret": "",
        "name": "patched",
    })
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "patched"
    assert body["oauth_client_id"] == "id-2"
    assert body["oauth_scopes"] == "read write"
    assert body["has_oauth_secret"] is True
    assert SECRET not in json.dumps(body)
    assert "oauth_client_secret" not in body
    with session() as s:
        row = s.get(McpServer, sid)
        assert config.decrypt(row.oauth_client_secret_enc) == SECRET


def test_tool_accepts_scope_list_and_does_not_leak_secret(c, auth):
    ctx = Ctx(bot_id=auth["bot_id"], requester_id=auth["user_id"], conv_id=0,
              is_channel=False, team_id=auth["team_id"])
    out = asyncio.run(execute_tool(ctx, "add_mcp_server", {
        "name": "tooloauth",
        "url": "https://api.athom.com/mcp",
        "client_id": "tool-client",
        "client_secret": SECRET,
        "scopes": ["homey", "offline"],
        "authorize_url": "https://accounts.athom.com/oauth2/authorise",
        "token_url": "https://api.athom.com/oauth2/token",
    }, {}))
    assert "error:" not in out, out
    assert "OAuth" in out
    assert SECRET not in out
    with session() as s:
        row = s.exec(select(McpServer).where(McpServer.name == "tooloauth",
                                             McpServer.bot_id == auth["bot_id"])).first()
        assert row is not None
        assert row.auth_mode == "oauth"
        assert row.oauth_client_id == "tool-client"
        assert row.oauth_scopes == "homey offline"
        assert config.decrypt(row.oauth_client_secret_enc) == SECRET
        assert SECRET not in (row.oauth_client_secret_enc or "")


def test_tool_accepts_space_separated_scopes(c, auth):
    ctx = Ctx(bot_id=auth["bot_id"], requester_id=auth["user_id"], conv_id=0,
              is_channel=False, team_id=auth["team_id"])
    out = asyncio.run(execute_tool(ctx, "add_mcp_server", {
        "name": "toolstr",
        "url": "https://example.com/mcp",
        "client_id": "cid",
        "scopes": "alpha beta",
        "authorize_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
    }, {}))
    assert "error:" not in out, out
    assert SECRET not in out
    with session() as s:
        row = s.exec(select(McpServer).where(McpServer.name == "toolstr")).first()
        assert row.oauth_scopes == "alpha beta"
        assert row.oauth_client_secret_enc == ""


def test_tool_requires_authorize_and_token_urls_with_client_id(c, auth):
    ctx = Ctx(bot_id=auth["bot_id"], requester_id=auth["user_id"], conv_id=0,
              is_channel=False, team_id=auth["team_id"])
    out = asyncio.run(execute_tool(ctx, "add_mcp_server", {
        "name": "incomplete",
        "url": "https://example.com/mcp",
        "client_id": "cid",
        "client_secret": SECRET,
        "scopes": "x",
    }, {}))
    assert out.startswith("error:")
    assert SECRET not in out
    with session() as s:
        assert s.exec(select(McpServer).where(McpServer.name == "incomplete")).first() is None
