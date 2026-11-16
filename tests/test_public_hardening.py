"""Closed registration, invite-link join, docs off, and auth rate limiting."""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlmodel import create_engine
from starlette.requests import Request

TMP = tempfile.mkdtemp(prefix="teambots-hardening-")
os.environ.setdefault("TB_DATA_DIR", TMP)
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, ratelimit  # noqa: E402
from app.main import app  # noqa: E402


def _request(client_host: str, headers: dict | None = None, connecting_ip: str | None = None) -> Request:
    header_list = []
    if headers:
        header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": header_list,
        "client": (client_host, 12345),
        "server": ("testserver", 80),
    }
    req = Request(scope)
    if connecting_ip is not None:
        req.state.connecting_ip = connecting_ip
    return req


@pytest.fixture
def c(tmp_path, monkeypatch):
    import app.db as db
    import app.home as home

    saved = db.engine, home.engine
    eng = create_engine(f"sqlite:///{tmp_path}/hardening.db", connect_args={"check_same_thread": False})
    db.engine = home.engine = eng
    db.init_db()
    monkeypatch.setattr(config, "OPEN_REGISTRATION", False)
    monkeypatch.setattr(config, "ENABLE_DOCS", False)
    monkeypatch.setattr(config, "AUTH_RATE_LIMIT", 0)
    ratelimit.AUTH_LIMITER.reset()
    with TestClient(app) as client:
        yield client
    db.engine, home.engine = saved
    ratelimit.AUTH_LIMITER.reset()


def _register(c, username, password="password123", invite="", display=""):
    body = {"username": username, "password": password, "display_name": display or username}
    if invite:
        body["invite_code"] = invite
    return c.post("/api/register", json=body)


def test_first_user_can_register_without_invite(c):
    r = _register(c, "koen")
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "koen"


def test_second_user_without_invite_is_refused(c):
    assert _register(c, "koen").status_code == 200
    r = _register(c, "ruud")
    assert r.status_code == 403, r.text
    assert "invite" in r.json()["detail"].lower()


def test_register_with_invite_joins_the_project(c):
    admin = _register(c, "koen").json()
    headers = {"Authorization": f"Bearer {admin['token']}"}
    team = c.post("/api/teams", json={"name": "Family"}, headers=headers).json()
    code = team["invite_code"]
    r = _register(c, "ruud", invite=code, display="Ruud")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    teams = c.get("/api/teams", headers={"Authorization": f"Bearer {tok}"}).json()
    assert [t["name"] for t in teams] == ["Family"]
    assert teams[0]["role"] == "member"


def test_invalid_invite_is_refused_when_registration_is_closed(c):
    assert _register(c, "koen").status_code == 200
    r = _register(c, "ruud", invite="not-a-real-code")
    assert r.status_code == 403, r.text


def test_open_registration_allows_signup_without_invite(c, monkeypatch):
    assert _register(c, "koen").status_code == 200
    monkeypatch.setattr(config, "OPEN_REGISTRATION", True)
    r = _register(c, "ruud")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    teams = c.get("/api/teams", headers={"Authorization": f"Bearer {tok}"}).json()
    assert teams == []


def test_open_registration_with_invite_still_joins(c, monkeypatch):
    admin = _register(c, "koen").json()
    headers = {"Authorization": f"Bearer {admin['token']}"}
    team = c.post("/api/teams", json={"name": "Family"}, headers=headers).json()
    monkeypatch.setattr(config, "OPEN_REGISTRATION", True)
    r = _register(c, "ruud", invite=team["invite_code"])
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    teams = c.get("/api/teams", headers={"Authorization": f"Bearer {tok}"}).json()
    assert [t["name"] for t in teams] == ["Family"]


def test_docs_are_off_by_default(c):
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = c.get(path)
        assert r.status_code == 404, path


def test_docs_can_be_enabled(c, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_DOCS", True)
    spec = c.get("/openapi.json")
    assert spec.status_code == 200, spec.text
    assert "openapi" in spec.json()
    assert c.get("/docs").status_code == 200
    assert c.get("/redoc").status_code == 200


def test_login_and_register_are_rate_limited(c, monkeypatch):
    monkeypatch.setattr(config, "AUTH_RATE_LIMIT", 10)
    ratelimit.AUTH_LIMITER.reset()
    assert _register(c, "koen").status_code == 200
    ratelimit.AUTH_LIMITER.reset()
    # 10 failed logins, then 429
    for i in range(10):
        r = c.post("/api/login", json={"username": "koen", "password": "wrong-password"})
        assert r.status_code == 401, i
    r = c.post("/api/login", json={"username": "koen", "password": "wrong-password"})
    assert r.status_code == 429, r.text
    assert r.json()["detail"] == "too many attempts, try again later"
    assert "Retry-After" in r.headers
    # Register shares the same per-IP bucket.
    r = _register(c, "ruud", invite="anything")
    assert r.status_code == 429


def test_client_ip_trusts_forwarded_headers_only_from_a_local_proxy():
    # Use globally routed addresses: Python 3.13 treats TEST-NET (203.0.113.0/24,
    # 198.51.100.0/24) as is_private, which would wrongly count as a trusted proxy.
    spoof = {"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "4.5.6.7"}
    # Immediate public peer: ignore spoofed headers.
    public = _request("8.8.8.8", spoof, connecting_ip="8.8.8.8")
    assert ratelimit.client_ip(public) == "8.8.8.8"
    # TestClient-style host is not a trusted proxy either.
    testhost = _request("testclient", spoof, connecting_ip="testclient")
    assert ratelimit.client_ip(testhost) == "testclient"
    # Local proxy: CF-Connecting-IP wins over X-Forwarded-For.
    local = _request("127.0.0.1", spoof, connecting_ip="127.0.0.1")
    assert ratelimit.client_ip(local) == "4.5.6.7"
    # Private Docker/Caddy peer, only XFF.
    docker = _request("10.0.0.2", {"x-forwarded-for": "1.2.3.4, 10.0.0.2"}, connecting_ip="10.0.0.2")
    assert ratelimit.client_ip(docker) == "1.2.3.4"
    # Left-most XFF hop is the original client.
    chain = _request("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 10.0.0.2"}, connecting_ip="127.0.0.1")
    assert ratelimit.client_ip(chain) == "9.9.9.9"


def test_rate_limit_uses_cf_connecting_ip_behind_a_local_proxy(c, monkeypatch):
    monkeypatch.setattr(config, "AUTH_RATE_LIMIT", 10)
    ratelimit.AUTH_LIMITER.reset()
    assert _register(c, "koen").status_code == 200
    ratelimit.AUTH_LIMITER.reset()
    headers = {"CF-Connecting-IP": "203.0.113.80", "X-Forwarded-For": "203.0.113.80"}
    # TestClient's peer is "testclient", so forwarded headers must not let an
    # attacker pick a fresh bucket. All 11 attempts share the testclient key.
    for i in range(10):
        r = c.post("/api/login", json={"username": "koen", "password": "wrong-password"}, headers=headers)
        assert r.status_code == 401, i
    r = c.post("/api/login", json={"username": "koen", "password": "wrong-password"},
               headers={"CF-Connecting-IP": "198.51.100.99"})
    assert r.status_code == 429
