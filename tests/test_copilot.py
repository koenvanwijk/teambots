"""GitHub Copilot provider tests against a mock (no real tokens).

Does not import app at collection time, so test_flow can set TB_DATA_DIR first.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-copilot-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_HTTP_ALLOW_PRIVATE", "1")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

MOCK = "http://127.0.0.1:18793"
os.environ["TB_MOCK_COPILOT"] = MOCK

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_copilot  # noqa: E402


@pytest.fixture(scope="module")
def mock_server():
    server = uvicorn.Server(uvicorn.Config(mock_copilot.app, host="127.0.0.1", port=18793, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(MOCK + "/__stats", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("mock copilot did not start")
    yield
    server.should_exit = True


@pytest.fixture
def copilot(mock_server):
    from app import copilot as mod
    mod.clear_cache()
    orig = mod.token_url, mod.fallback_base, mod.login_base, mod.DEVICE_CLIENT_ID
    mod.token_url = lambda domain: f"{MOCK}/copilot_internal/v2/token"
    mod.fallback_base = lambda domain: MOCK
    mod.login_base = lambda domain: MOCK
    mod.DEVICE_CLIENT_ID = mock_copilot.CLIENT_ID
    httpx.post(MOCK + "/__reset", timeout=2)
    yield mod
    mod.token_url, mod.fallback_base, mod.login_base, mod.DEVICE_CLIENT_ID = orig
    mod.clear_cache()


def test_normalize_domain():
    from app import copilot as mod
    assert mod.normalize_domain("") == "github.com"
    assert mod.normalize_domain("https://ICTGROUP.GHE.COM/foo") == "ictgroup.ghe.com"
    assert mod.normalize_domain("github.com") == "github.com"
    with pytest.raises(ValueError):
        mod.normalize_domain("evil.example.com")
    with pytest.raises(ValueError):
        mod.normalize_domain("ictgroup.ghe.com.evil.com")


def test_redact_strips_secrets():
    from app import copilot as mod
    out = mod.redact('Authorization: Bearer ghu_ABCDEFG123456 and {"token":"tid=secretvalue"}')
    assert "ghu_ABCDEFG123456" not in out
    assert "tid=secretvalue" not in out
    assert "ghu_…" in out or "[redacted]" in out


def test_fallback_and_base_resolution():
    from app import copilot as mod
    assert mod.fallback_base("github.com") == "https://api.individual.githubcopilot.com"
    assert mod.fallback_base("ictgroup.ghe.com") == "https://copilot-api.ictgroup.ghe.com"
    assert mod.token_url("ictgroup.ghe.com") == "https://api.ictgroup.ghe.com/copilot_internal/v2/token"
    data = {"endpoints": {"api": "https://copilot-api.ictgroup.ghe.com"}}
    assert mod._base_from_response(data, "tid=x", "ictgroup.ghe.com") == "https://copilot-api.ictgroup.ghe.com"
    token = "tid=abc;proxy-ep=proxy.individual.githubcopilot.com;sess=1"
    assert mod._base_from_response({}, token, "github.com") == "https://api.individual.githubcopilot.com"
    assert mod._base_from_response({}, "tid=abc", "ictgroup.ghe.com") == "https://copilot-api.ictgroup.ghe.com"


def test_exchange_and_cache(copilot):
    async def go():
        sess = await copilot.exchange(mock_copilot.GITHUB_TOKEN, "ictgroup.ghe.com")
        assert sess["base_url"] == MOCK
        assert sess["token"] == mock_copilot.COPILOT_TOKEN
        assert sess["expires_at_ms"] > int(time.time() * 1000)
        assert httpx.get(MOCK + "/__stats").json()["exchange"] == 1
        sess2 = await copilot.exchange(mock_copilot.GITHUB_TOKEN, "ictgroup.ghe.com")
        assert sess2["token"] == sess["token"]
        assert httpx.get(MOCK + "/__stats").json()["exchange"] == 1
    asyncio.run(go())


def test_cache_refreshes_near_expiry(copilot):
    async def go():
        await copilot.exchange(mock_copilot.GITHUB_TOKEN, "ictgroup.ghe.com")
        fp = copilot._fingerprint(mock_copilot.GITHUB_TOKEN)
        with copilot._lock:
            copilot._cache[("ictgroup.ghe.com", fp)]["expires_at_ms"] = int(time.time() * 1000) + 10_000
        await copilot.exchange(mock_copilot.GITHUB_TOKEN, "ictgroup.ghe.com")
        assert httpx.get(MOCK + "/__stats").json()["exchange"] == 2
    asyncio.run(go())


def test_exchange_401_is_safe(copilot):
    async def go():
        with pytest.raises(copilot.LLMError) as ei:
            await copilot.exchange("ghu_WRONGTOKEN_should_not_leak_xxxxx", "ictgroup.ghe.com")
        msg = str(ei.value)
        assert "HTTP 401" in msg
        assert "ghu_WRONGTOKEN_should_not_leak_xxxxx" not in msg
        assert "ghu_testtoken" not in msg
    asyncio.run(go())


def test_chat_tool_calling_and_models(copilot):
    from app import config
    from app.db import Endpoint

    ep = Endpoint(
        id=99, team_id=1, owner_id=1, name="ICT Copilot",
        base_url=MOCK, api_key_enc=config.encrypt(mock_copilot.GITHUB_TOKEN),
        default_model="claude-sonnet-5", provider="github_copilot",
        github_domain="ictgroup.ghe.com",
    )

    async def go():
        models = await copilot.list_models(ep)
        assert "claude-sonnet-5" in [m["id"] for m in models]
        tool = {"type": "function", "function": {"name": "ping", "description": "Call this tool.",
                                                "parameters": {"type": "object",
                                                               "properties": {"ok": {"type": "boolean"}},
                                                               "required": ["ok"]}}}
        reply = await copilot.chat(
            ep, "claude-sonnet-5",
            [{"role": "user", "content": "Call the ping tool with ok=true."}], [tool])
        assert reply["tool_calls"]
        assert reply["tool_calls"][0]["function"]["name"] == "ping"
        assert httpx.get(MOCK + "/__stats").json()["exchange"] == 1
    asyncio.run(go())


def test_api_create_and_test_copilot_endpoint(copilot):
    """Uses a throwaway user so this can run before or after test_flow."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import config
    from app.db import session, Endpoint

    with TestClient(app) as c:
        r = c.post("/api/register", json={
            "username": "copilot_tester", "password": "password123", "display_name": "Copilot Tester"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        team_id = c.post("/api/teams", headers=h, json={"name": "Copilot Project"}).json()["id"]

        bad = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "Bad domain", "provider": "github_copilot", "github_domain": "evil.example.com",
            "api_key": mock_copilot.GITHUB_TOKEN, "default_model": "claude-sonnet-5"})
        assert bad.status_code == 400

        missing = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "No token", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "default_model": "claude-sonnet-5"})
        assert missing.status_code == 400

        r = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "Mock Copilot", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "api_key": mock_copilot.GITHUB_TOKEN, "default_model": "claude-sonnet-5"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "github_copilot"
        assert body["github_domain"] == "ictgroup.ghe.com"
        assert body["has_key"] is True
        assert "api_key" not in body
        dumped = r.text
        assert mock_copilot.GITHUB_TOKEN not in dumped
        assert mock_copilot.COPILOT_TOKEN not in dumped
        ep_id = body["id"]

        models = c.get(f"/api/endpoints/{ep_id}/models", headers=h)
        assert models.status_code == 200, models.text
        assert "claude-sonnet-5" in [m["id"] for m in models.json()["models"]]

        test = c.post(f"/api/endpoints/{ep_id}/test", headers=h)
        assert test.status_code == 200, test.text
        assert test.json()["reachable"] is True
        assert test.json()["tool_calling"] is True

        with session() as s:
            e = s.get(Endpoint, ep_id)
            assert config.decrypt(e.api_key_enc) == mock_copilot.GITHUB_TOKEN
            assert mock_copilot.COPILOT_TOKEN not in (e.api_key_enc or "")


def test_vision_header_required_for_images(copilot):
    from app import config
    from app.copilot import completions_headers
    from app.db import Endpoint

    text_msgs = [{"role": "user", "content": "hello"}]
    assert "Copilot-Vision-Request" not in completions_headers(text_msgs)
    img_msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
    ]}]
    h = completions_headers(img_msgs)
    assert h["Copilot-Vision-Request"] == "true"

    ep = Endpoint(
        id=99, team_id=1, owner_id=1, name="ICT Copilot",
        base_url=MOCK, api_key_enc=config.encrypt(mock_copilot.GITHUB_TOKEN),
        default_model="claude-sonnet-5", provider="github_copilot",
        github_domain="ictgroup.ghe.com",
    )

    async def go():
        # The provider sets the vision header, so the mock accepts the image.
        reply = await copilot.chat(ep, "claude-sonnet-5", img_msgs)
        assert "image" in reply["content"].lower()
        # Direct request without the header is what used to happen, and is a 400.
        import httpx as hx
        sess = await copilot._session(ep, hx.AsyncClient(timeout=10))
        async with hx.AsyncClient(timeout=10) as client:
            r = await client.post(MOCK + "/chat/completions", json={"model": "claude-sonnet-5", "messages": img_msgs},
                                  headers={"Authorization": f"Bearer {sess['token']}",
                                           "Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.text.strip() == "Bad Request"
        from app.llm import format_http_error
        err = format_http_error(r, "ICT Copilot")
        assert "HTTP 400" in err and "Bad Request" in err
        assert sess["token"] not in err
    asyncio.run(go())


def _register(c, username):
    r = c.post("/api/register", json={"username": username, "password": "password123", "display_name": username})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _device_login(c, h):
    start = c.post("/api/copilot/device/start", headers=h, json={"github_domain": "ictgroup.ghe.com"})
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["user_code"] == mock_copilot.USER_CODE
    assert body["verification_uri"] == "https://ictgroup.ghe.com/login/device"
    assert mock_copilot.DEVICE_CODE not in start.text
    login = body["login_id"]
    first = c.post("/api/copilot/device/poll", headers=h, json={"login_id": login})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    done = c.post("/api/copilot/device/poll", headers=h, json={"login_id": login})
    assert done.json()["status"] == "done"
    assert mock_copilot.GITHUB_TOKEN not in done.text
    return login


def test_device_login_creates_copilot_endpoint(copilot):
    """Sign in with a device code instead of pasting a token; the token never reaches the browser."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import config
    from app.db import session, Endpoint

    with TestClient(app) as c:
        h = _register(c, "copilot_device_tester")
        team_id = c.post("/api/teams", headers=h, json={"name": "Device Project"}).json()["id"]
        login = _device_login(c, h)

        r = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "My Copilot", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "device_login": login, "default_model": "claude-sonnet-5"})
        assert r.status_code == 200, r.text
        assert r.json()["has_key"] is True
        assert mock_copilot.GITHUB_TOKEN not in r.text
        ep_id = r.json()["id"]
        with session() as s:
            assert config.decrypt(s.get(Endpoint, ep_id).api_key_enc) == mock_copilot.GITHUB_TOKEN
        assert c.post(f"/api/endpoints/{ep_id}/test", headers=h).json()["reachable"] is True

        again = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "Twice", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "device_login": login, "default_model": "claude-sonnet-5"})
        assert again.status_code == 400


def test_device_login_is_bound_to_its_user(copilot):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = _register(c, "copilot_device_owner")
        login = _device_login(c, h)
        other = _register(c, "copilot_device_other")
        team_id = c.post("/api/teams", headers=other, json={"name": "Other Project"}).json()["id"]
        assert c.post("/api/copilot/device/poll", headers=other, json={"login_id": login}).status_code == 404
        r = c.post(f"/api/teams/{team_id}/endpoints", headers=other, json={
            "name": "Stolen", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "device_login": login, "default_model": "claude-sonnet-5"})
        assert r.status_code == 400


def test_device_login_replaces_token_on_edit(copilot):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import config
    from app.db import session, Endpoint

    with TestClient(app) as c:
        h = _register(c, "copilot_device_editor")
        team_id = c.post("/api/teams", headers=h, json={"name": "Edit Project"}).json()["id"]
        ep_id = c.post(f"/api/teams/{team_id}/endpoints", headers=h, json={
            "name": "Old token", "provider": "github_copilot", "github_domain": "ictgroup.ghe.com",
            "api_key": "ghu_oldtoken_notreal_000000000000", "default_model": "claude-sonnet-5"}).json()["id"]
        login = _device_login(c, h)
        r = c.patch(f"/api/endpoints/{ep_id}", headers=h, json={"device_login": login})
        assert r.status_code == 200, r.text
        with session() as s:
            assert config.decrypt(s.get(Endpoint, ep_id).api_key_enc) == mock_copilot.GITHUB_TOKEN
