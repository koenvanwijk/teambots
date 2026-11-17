"""The protected Git-push tool keeps a stored token outside the shared sandbox."""
import asyncio
import os
import sys
import tempfile

import pytest

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-gitpush-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent, config, sandbox, secrets_http  # noqa: E402
from app.db import Bot, Secret, init_db, session  # noqa: E402


@pytest.fixture()
def secret_store(tmp_path):
    """Give this file its own database, independent from API/end-to-end tests."""
    import app.db as db
    import app.home as home
    from sqlmodel import create_engine

    saved_db_engine, saved_home_engine = db.engine, home.engine
    db.engine = home.engine = create_engine(
        f"sqlite:///{tmp_path}/git-push.db", connect_args={"check_same_thread": False})
    init_db()
    with session() as s:
        s.add(Secret(bot_id=77, name="GITHUB_TOKEN", value_enc=config.encrypt("very-secret-token"),
                     hosts="github.com,api.github.com"))
        s.commit()
    yield 77
    db.engine = saved_db_engine
    home.engine = saved_home_engine


def test_git_push_resolves_an_exact_placeholder_and_masks_output(secret_store, monkeypatch):
    called = {}

    async def fake_push(key, remote_url, branch, token, path, username):
        called.update(key=key, remote_url=remote_url, branch=branch, token=token, path=path, username=username)
        return "remote replied with very-secret-token"

    monkeypatch.setattr(config, "SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(config, "GIT_PUSH_ALLOW_PRIVATE", False)
    monkeypatch.setattr(secrets_http, "_is_public", lambda host: True)
    monkeypatch.setattr(sandbox, "git_push", fake_push)
    out = asyncio.run(secrets_http.git_push(
        secret_store, "p12", "https://GITHUB.com/acme/repo.git", "main", "{{secret:GITHUB_TOKEN}}",
        "repos/app", "x-access-token"))

    assert called == {
        "key": "p12", "remote_url": "https://github.com/acme/repo.git", "branch": "main",
        "token": "very-secret-token", "path": "repos/app", "username": "x-access-token",
    }
    assert out == "remote replied with [secret:GITHUB_TOKEN]"


def test_git_push_rejects_raw_tokens_unbound_hosts_and_unsafe_inputs(secret_store, monkeypatch):
    seen = []

    async def fake_push(*args):
        seen.append(args)
        return "should not run"

    monkeypatch.setattr(config, "SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(secrets_http, "_is_public", lambda host: True)
    monkeypatch.setattr(sandbox, "git_push", fake_push)

    raw = asyncio.run(secrets_http.git_push(
        secret_store, "p12", "https://github.com/acme/repo.git", "main", "very-secret-token"))
    wrong_host = asyncio.run(secrets_http.git_push(
        secret_store, "p12", "https://uploads.github.com/acme/repo.git", "main", "{{secret:GITHUB_TOKEN}}"))
    unsafe_url = asyncio.run(secrets_http.git_push(
        secret_store, "p12", "https://user:password@github.com/acme/repo.git", "main",
        "{{secret:GITHUB_TOKEN}}"))
    unsafe_branch = asyncio.run(secrets_http.git_push(
        secret_store, "p12", "https://github.com/acme/repo.git", "main:other", "{{secret:GITHUB_TOKEN}}"))

    assert "exactly one" in raw
    assert "bound exactly" in wrong_host
    assert "plain https" in unsafe_url
    assert "simple branch" in unsafe_branch
    assert seen == []


def test_git_push_schema_and_approval_are_not_bypassed_by_auto_mode():
    spec = next(t["function"] for t in agent.BUILTIN_TOOLS if t["function"]["name"] == "git_push")
    assert set(spec["parameters"]["required"]) == {"remote_url", "branch", "token"}
    assert "git_push" in agent.RISKY_TOOLS
    bot = Bot(team_id=1, owner_id=1, name="Bot", handle="bot", endpoint_id=1, approval_mode="auto")
    ctx = agent.Ctx(bot_id=1, requester_id=1, conv_id=1, is_channel=False, team_id=1)
    assert agent.needs_approval(bot, ctx, "git_push", {}, {})


def test_git_push_validators_are_deliberately_narrow():
    assert sandbox.git_push_remote("https://github.com/acme/repo.git") == (
        "https://github.com/acme/repo.git", "github.com")
    assert sandbox.git_push_branch("release/2026.11") == "release/2026.11"
    assert sandbox.git_push_path("repos/app") == "repos/app"
    for remote in ("http://github.com/acme/repo.git", "git@github.com:acme/repo.git",
                   "https://github.com:8443/acme/repo.git", "https://github.com/acme/repo.git?x=1"):
        with pytest.raises(ValueError):
            sandbox.git_push_remote(remote)
    for branch in ("refs/heads/main", "main:other", "main..other", "main.lock"):
        with pytest.raises(ValueError):
            sandbox.git_push_branch(branch)
    for path in ("../repo", "/repo", "a//b"):
        with pytest.raises(ValueError):
            sandbox.git_push_path(path)
