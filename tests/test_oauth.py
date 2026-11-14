"""Sign in with GitHub / Google, with the provider mocked. Focus: invite-link account creation,
one-person-one-account linking, the signup gate, and linking a second provider. Run: pytest -q"""
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest

TMP = tempfile.mkdtemp(prefix="teambots-oauth-")
os.environ.setdefault("TB_DATA_DIR", TMP)
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, oauth  # noqa: E402
from app.main import app  # noqa: E402

# Identities the mocked provider hands back, keyed by the "code" we pass to the callback.
IDENTS: dict = {}


@pytest.fixture(scope="module")
def c():
    import app.db as db  # own database, so collection order and other suites' teardown cannot touch it
    import app.home as home  # home.py imported `engine` by value, so it needs rebinding too
    from sqlmodel import create_engine
    saved_engine = db.engine
    eng = create_engine(f"sqlite:///{TMP}/oauth.db", connect_args={"check_same_thread": False})
    db.engine = home.engine = eng
    db.init_db()
    saved = {k: getattr(config, k) for k in
             ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
              "PUBLIC_URL", "OAUTH_ALLOWLIST")}
    config.GITHUB_CLIENT_ID = config.GITHUB_CLIENT_SECRET = "gh-test"
    config.GOOGLE_CLIENT_ID = config.GOOGLE_CLIENT_SECRET = "go-test"
    config.PUBLIC_URL = "https://bots.example.com"
    config.OAUTH_ALLOWLIST = []
    real_exchange = oauth.exchange_code
    oauth.exchange_code = lambda provider, code: dict(IDENTS[code])
    with TestClient(app) as client:
        yield client
    oauth.exchange_code = real_exchange
    for k, v in saved.items():
        setattr(config, k, v)
    db.engine = home.engine = saved_engine


def _begin(c, provider, invite="", link=False, headers=None):
    """Hit /start, return the opaque `state` the provider would echo back."""
    params = {}
    if invite:
        params["invite"] = invite
    if link:
        params["link"] = 1
    r = c.get(f"/api/auth/{provider}/start", params=params or None, headers=headers or {}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(("https://github.com/", "https://accounts.google.com/")), loc
    return parse_qs(urlparse(loc).query)["state"][0]


def _callback(c, provider, code, state):
    r = c.get(f"/api/auth/{provider}/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    frag = urlparse(r.headers["location"]).fragment
    return parse_qs(frag)  # {'tb_oauth': [token]} or {'tb_oauth_error': [msg]}


def _login(c, provider, code, invite="", headers=None):
    state = _begin(c, provider, invite=invite, headers=headers)
    return _callback(c, provider, code, state)


def _token(res):
    assert "tb_oauth" in res, f"expected a token, got {res}"
    return res["tb_oauth"][0]


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


S: dict = {}


def test_providers_reflect_config(c):
    body = c.get("/api/auth/providers").json()
    assert body["public_url_set"] is True
    assert {p["id"] for p in body["providers"]} == {"google", "github"}


def test_invite_link_creates_account_and_joins_project(c):
    # An admin exists (password account) and owns a project with an invite code.
    admin = c.post("/api/register", json={"username": "admin1", "password": "password123"}).json()
    team = c.post("/api/teams", json={"name": "Client A Project", "client": "Client A"}, headers=H(admin["token"])).json()
    S["invite"] = team["invite_code"]
    S["team"] = team["id"]

    # Tobias, a freelancer, signs in with his private GitHub via the invite link. No prior account.
    IDENTS["gh-tobias"] = {"subject": "gh-1001", "email": "tobias@gmail.com", "name": "Tobias"}
    tok = _token(_login(c, "github", "gh-tobias", invite=S["invite"]))
    me = c.get("/api/me", headers=H(tok)).json()
    assert me["username"] and me["display_name"] == "Tobias"
    S["tobias_id"] = me["id"]
    # He is a member of the project he was invited to, and of nothing else.
    teams = c.get("/api/teams", headers=H(tok)).json()
    assert [t["id"] for t in teams] == [S["team"]]
    assert teams[0]["role"] == "member"


def test_second_provider_links_to_same_person_by_email(c):
    # Same Tobias signs in later with his private Google (same verified email). One person, one account.
    IDENTS["go-tobias"] = {"subject": "go-2001", "email": "tobias@gmail.com", "name": "Tobias G"}
    tok = _token(_login(c, "google", "go-tobias"))  # no invite needed; email already owns an account
    me = c.get("/api/me", headers=H(tok)).json()
    assert me["id"] == S["tobias_id"], "a second provider with the same verified email must not fork a new user"
    ids = c.get("/api/auth/identities", headers=H(tok)).json()
    assert {i["provider"] for i in ids["identities"]} == {"github", "google"}
    assert ids["has_password"] is False  # OAuth-only account cannot sign in with a password


def test_returning_identity_logs_in_not_duplicate(c):
    IDENTS["gh-tobias"] = {"subject": "gh-1001", "email": "tobias@gmail.com", "name": "Tobias"}
    tok = _token(_login(c, "github", "gh-tobias"))  # same subject again, no invite
    assert c.get("/api/me", headers=H(tok)).json()["id"] == S["tobias_id"]


def test_no_invite_and_not_allowlisted_is_refused(c):
    IDENTS["gh-stranger"] = {"subject": "gh-9999", "email": "stranger@example.com", "name": "Nobody"}
    res = _login(c, "github", "gh-stranger")  # no invite, email not allowlisted
    assert "tb_oauth_error" in res
    assert "invite" in res["tb_oauth_error"][0].lower()
    assert "tb_oauth" not in res


def test_invalid_invite_is_treated_as_no_invite(c):
    IDENTS["gh-badinvite"] = {"subject": "gh-8888", "email": "x@example.com", "name": "X"}
    res = _login(c, "github", "gh-badinvite", invite="not-a-real-code")
    assert "tb_oauth_error" in res and "tb_oauth" not in res


def test_allowlist_lets_an_ict_email_in_without_invite(c):
    config.OAUTH_ALLOWLIST = ["@ict.nl"]
    try:
        IDENTS["go-koen"] = {"subject": "go-3001", "email": "koen@ict.nl", "name": "Koen"}
        tok = _token(_login(c, "google", "go-koen"))  # allowlisted domain, no invite
        assert c.get("/api/me", headers=H(tok)).json()["display_name"] == "Koen"
    finally:
        config.OAUTH_ALLOWLIST = []


def test_signed_in_user_links_a_provider_to_their_account(c):
    # A password account links a GitHub identity by starting the flow while signed in.
    user = c.post("/api/register", json={"username": "carla", "password": "password123"}).json()
    IDENTS["gh-carla"] = {"subject": "gh-4001", "email": "carla.personal@gmail.com", "name": "Carla P"}
    state = _begin(c, "github", link=True, headers=H(user["token"]))  # explicit link => attach to this account
    tok = _token(_callback(c, "github", "gh-carla", state))
    assert c.get("/api/me", headers=H(tok)).json()["id"] == user["user"]["id"]
    ids = c.get("/api/auth/identities", headers=H(user["token"])).json()
    assert ids["has_password"] is True and [i["provider"] for i in ids["identities"]] == ["github"]


def test_linking_an_identity_owned_by_someone_else_is_refused(c):
    # Tobias' GitHub identity cannot be grabbed by a different signed-in account.
    other = c.post("/api/register", json={"username": "mallory", "password": "password123"}).json()
    IDENTS["gh-tobias"] = {"subject": "gh-1001", "email": "tobias@gmail.com", "name": "Tobias"}
    state = _begin(c, "github", link=True, headers=H(other["token"]))
    res = _callback(c, "github", "gh-tobias", state)
    assert "tb_oauth_error" in res and "another" in res["tb_oauth_error"][0].lower()


def test_link_requires_being_signed_in(c):
    with TestClient(app) as fresh:  # a fresh browser: no session cookie
        r = fresh.get("/api/auth/github/start", params={"link": 1}, follow_redirects=False)
    frag = parse_qs(urlparse(r.headers["location"]).fragment)
    assert "tb_oauth_error" in frag and "sign in" in frag["tb_oauth_error"][0].lower()


def test_ambient_session_does_not_silently_link(c):
    # A signed-in browser (the callback sets a session cookie). A plain sign-in with a brand-new,
    # un-invited identity must be refused, not quietly attached to the ambient account.
    with TestClient(app) as fresh:
        config.OAUTH_ALLOWLIST = ["@ict.nl"]
        try:
            IDENTS["gh-owner"] = {"subject": "gh-owner-1", "email": "owner@ict.nl", "name": "Owner"}
            st = _begin(fresh, "github")
            tok = _token(_callback(fresh, "github", "gh-owner", st))  # cookie now set on `fresh`
        finally:
            config.OAUTH_ALLOWLIST = []
        assert fresh.cookies.get("tb_token"), "the sign-in should establish an ambient session cookie"
        IDENTS["gh-drifter"] = {"subject": "gh-7777", "email": "drifter@example.com", "name": "Drifter"}
        res = _login(fresh, "github", "gh-drifter")  # no link=1, no invite, but the cookie is present
        assert "tb_oauth_error" in res and "tb_oauth" not in res
        ids = fresh.get("/api/auth/identities", headers=H(tok)).json()
    assert len(ids["identities"]) == 1, "a surprise identity must not attach to the ambient session"


def test_state_is_one_time_and_expires(c):
    IDENTS["gh-once"] = {"subject": "gh-5001", "email": "once@ict.nl", "name": "Once"}
    config.OAUTH_ALLOWLIST = ["@ict.nl"]
    try:
        state = _begin(c, "github")
        assert "tb_oauth" in _callback(c, "github", "gh-once", state)  # first use works
        reused = _callback(c, "github", "gh-once", state)  # same state again
        assert "tb_oauth_error" in reused and "expired" in reused["tb_oauth_error"][0].lower()
    finally:
        config.OAUTH_ALLOWLIST = []


def test_expired_state_is_rejected(c):
    from datetime import timedelta
    from app.db import OAuthState, now, session, select
    config.OAUTH_ALLOWLIST = ["@ict.nl"]
    try:
        state = _begin(c, "github")
        with session() as s:
            row = s.get(OAuthState, state)
            row.created_at = now() - timedelta(hours=1)
            s.add(row)
            s.commit()
        IDENTS["gh-late"] = {"subject": "gh-6001", "email": "late@ict.nl", "name": "Late"}
        res = _callback(c, "github", "gh-late", state)
        assert "tb_oauth_error" in res and "expired" in res["tb_oauth_error"][0].lower()
        assert not s.exec(select(OAuthState).where(OAuthState.state == state)).all()  # consumed either way
    finally:
        config.OAUTH_ALLOWLIST = []


def test_cancelled_or_misconfigured_start_redirects_with_error(c):
    # Provider reports the user declined consent.
    res = c.get("/api/auth/github/callback", params={"error": "access_denied", "state": "x"},
                follow_redirects=False)
    frag = parse_qs(urlparse(res.headers["location"]).fragment)
    assert "tb_oauth_error" in frag
    # An unconfigured provider cannot start.
    saved = config.GOOGLE_CLIENT_ID
    config.GOOGLE_CLIENT_ID = ""
    try:
        r = c.get("/api/auth/google/start", follow_redirects=False)
        assert "tb_oauth_error" in parse_qs(urlparse(r.headers["location"]).fragment)
    finally:
        config.GOOGLE_CLIENT_ID = saved
