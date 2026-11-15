"""Android sandbox option: container args, settings API, and KVM guards.

Does not import app at collection time, so test_flow can set TB_DATA_DIR first.
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_HTTP_ALLOW_PRIVATE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_docker_run_argv_default_is_lean_without_kvm():
    from app.sandbox import SandboxSpec, docker_run_argv
    argv = docker_run_argv("tb-p9", SandboxSpec())
    assert "teambots-sandbox:latest" in argv
    assert "teambots-sandbox-android:latest" not in argv
    assert "--device" not in argv
    assert "/dev/kvm" not in argv
    assert "--memory" in argv and "2048m" in argv
    assert "--cpus" in argv and "2.0" in argv
    assert "teambots.android=0" in argv
    assert "sleep" in argv and "infinity" in argv


def test_docker_run_argv_android_with_kvm():
    from app.sandbox import SandboxSpec, docker_run_argv
    argv = docker_run_argv("tb-p9", SandboxSpec(
        android_enabled=True, memory_mb=6144, cpus=4, kvm_device=True, kvm_gid=993))
    assert "teambots-sandbox-android:latest" in argv
    assert argv[argv.index("--device") + 1] == "/dev/kvm"
    assert argv[argv.index("--group-add") + 1] == "993"
    assert "6144m" in argv
    assert "4" in argv or "4.0" in argv
    assert "tb-android-sdk:/opt/android-sdk" in argv
    assert "teambots.android=1" in argv
    assert "TB_ANDROID_ACCEL=on" in " ".join(argv)
    assert "--cap-drop" in argv and "ALL" in argv


def test_docker_run_argv_software_mode_has_no_kvm_device():
    from app.sandbox import SandboxSpec, docker_run_argv
    argv = docker_run_argv("tb-p9", SandboxSpec(
        android_enabled=True, android_software=True, memory_mb=4096, cpus=2, kvm_device=False))
    assert "/dev/kvm" not in argv
    assert "--device" not in argv
    assert "TB_ANDROID_ACCEL=off" in " ".join(argv)
    assert "teambots-sandbox-android:latest" in argv


def test_docker_run_argv_android_without_kvm_or_software_raises():
    from app.sandbox import AndroidUnavailable, SandboxSpec, docker_run_argv
    with pytest.raises(AndroidUnavailable, match="geen KVM"):
        docker_run_argv("tb-p9", SandboxSpec(android_enabled=True, android_software=False, kvm_device=False))


def test_validate_limits_and_prefix_defaults():
    from app import config
    from app.sandbox import validate_android_limits
    assert validate_android_limits(6144, 4) == (6144, 4)
    with pytest.raises(ValueError, match="android_memory_mb"):
        validate_android_limits(100, 4)
    with pytest.raises(ValueError, match="android_cpus"):
        validate_android_limits(4096, 32)
    assert config.SANDBOX_PREFIX == "tb"
    assert config.SANDBOX_ANDROID_IMAGE == "teambots-sandbox-android:latest"


def test_validate_android_enable_docker_without_kvm():
    from app.sandbox import AndroidUnavailable, validate_android_enable
    with patch("app.sandbox.config.SANDBOX_BACKEND", "docker"), \
         patch("app.sandbox.kvm_available", return_value=False), \
         patch("app.sandbox.android_image_present", return_value=True):
        with pytest.raises(AndroidUnavailable, match="geen KVM"):
            validate_android_enable(enabled=True, software=False)
        validate_android_enable(enabled=True, software=True)
        validate_android_enable(enabled=False, software=False)


def test_validate_android_enable_missing_image():
    from app.sandbox import AndroidUnavailable, validate_android_enable
    with patch("app.sandbox.config.SANDBOX_BACKEND", "docker"), \
         patch("app.sandbox.kvm_available", return_value=True), \
         patch("app.sandbox.android_image_present", return_value=False):
        with pytest.raises(AndroidUnavailable, match="ontbreekt"):
            validate_android_enable(enabled=True, software=False)


def test_container_name_honours_prefix():
    from app.sandbox import container_name, workspace_volume
    with patch("app.sandbox.config.SANDBOX_PREFIX", "tbtest"):
        assert container_name("p3") == "tbtest-p3"
        assert workspace_volume("home-u1") == "tbtest-home-u1"


def test_apply_spec_recreate_argv(monkeypatch):
    """Toggling Android recreates the container with the new args; volume name stays.

    Docker is fully mocked so this never touches real tb-* containers.
    """
    import app.sandbox as sb
    from app.sandbox import SandboxSpec
    calls = []

    async def fake_run(argv, stdin, timeout, cwd=None, env=None, raw=False):
        calls.append(argv)
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 0, "abc123"
        return 0, ""

    monkeypatch.setattr(sb, "_run", fake_run)
    monkeypatch.setattr(sb.config, "SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(sb, "spec_for", lambda key: SandboxSpec(
        android_enabled=True, memory_mb=6144, cpus=4, kvm_device=True, kvm_gid=993))
    monkeypatch.setattr(sb, "validate_android_enable", lambda **kw: None)

    result = asyncio.run(sb.apply_spec("p9"))
    assert result["restarted"] is True
    flat = [" ".join(a) for a in calls]
    assert any("docker stop" in x for x in flat)
    assert any("docker rm -f tb-p9" in x for x in flat)
    run = next(a for a in calls if a[:2] == ["docker", "run"])
    assert "--device" in run and "/dev/kvm" in run
    assert "teambots-sandbox-android:latest" in run
    assert "tb-p9:/workspace" in run


# --------------------------------------------------------------------------- API


@pytest.fixture()
def client():
    """Isolated SQLite + TestClient. Engine is swapped only for these tests and always restored."""
    from fastapi.testclient import TestClient
    from sqlmodel import create_engine

    import app.db as db
    import app.home as home
    from app.db import AuthToken, Endpoint, Team, TeamMember, User, init_db, session
    from app.main import app

    saved_db, saved_home = db.engine, home.engine
    db.engine = home.engine = create_engine(
        f"sqlite:///{tempfile.mkdtemp(prefix='teambots-android-db-')}/test.db",
        connect_args={"check_same_thread": False},
    )
    init_db()
    with session() as s:
        alice = User(username="alice", display_name="Alice", password_hash="x")
        bob = User(username="bob", display_name="Bob", password_hash="x")
        s.add(alice)
        s.add(bob)
        s.commit()
        s.refresh(alice)
        s.refresh(bob)
        team = Team(name="AndroidProj", invite_code="and-invite")
        s.add(team)
        s.commit()
        s.refresh(team)
        s.add(TeamMember(team_id=team.id, user_id=alice.id, role="admin"))
        s.add(TeamMember(team_id=team.id, user_id=bob.id, role="member"))
        ep = Endpoint(team_id=team.id, owner_id=alice.id, name="mock",
                      base_url="http://127.0.0.1:9/v1", api_key_enc="", default_model="m",
                      shared=True)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ids = {"alice": alice.id, "bob": bob.id, "team": team.id, "ep": ep.id}
        headers = {}
        for name in ("alice", "bob"):
            token = f"tok-android-{name}-{ids[name]}"
            s.add(AuthToken(token=token, user_id=ids[name]))
            headers[name] = {"Authorization": f"Bearer {token}"}
        s.commit()

    def login(name: str) -> dict:
        return headers[name]

    try:
        with TestClient(app) as c:
            yield c, ids, login
    finally:
        db.engine, home.engine = saved_db, saved_home


def test_android_status_and_default_off(client):
    c, ids, login = client
    st = c.get("/api/android-status", headers=login("alice"))
    assert st.status_code == 200, st.text
    body = st.json()
    assert body["docker"] is False  # tests run with TB_SANDBOX=local
    assert "kvm_available" in body and "android_image_present" in body
    teams = c.get("/api/teams", headers=login("alice")).json()
    assert teams[0]["android_enabled"] is False
    assert teams[0]["android_software"] is False
    assert teams[0]["android_memory_mb"] == 6144
    assert teams[0]["android_cpus"] == 4


def test_patch_team_android_roundtrip_and_member_forbidden(client):
    c, ids, login = client
    r = c.patch(f"/api/teams/{ids['team']}", headers=login("alice"), json={
        "android_enabled": True, "android_software": True,
        "android_memory_mb": 4096, "android_cpus": 2,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["android_enabled"] is True
    assert data["android_software"] is True
    assert data["android_memory_mb"] == 4096
    assert data["android_cpus"] == 2
    assert data["sandbox_restarted"] is False  # local backend, or no container yet
    teams = c.get("/api/teams", headers=login("bob")).json()
    assert teams[0]["android_enabled"] is True
    assert teams[0]["android_memory_mb"] == 4096
    denied = c.patch(f"/api/teams/{ids['team']}", headers=login("bob"), json={"android_enabled": False})
    assert denied.status_code == 403
    bad = c.patch(f"/api/teams/{ids['team']}", headers=login("alice"), json={"android_memory_mb": 100})
    assert bad.status_code == 400


def test_patch_team_docker_without_kvm_rejected(client):
    c, ids, login = client
    # apply_spec is mocked so a passing validation never talks to the real Docker daemon.
    with patch("app.sandbox.config.SANDBOX_BACKEND", "docker"), \
         patch("app.sandbox.kvm_available", return_value=False), \
         patch("app.sandbox.android_image_present", return_value=True), \
         patch("app.sandbox.apply_spec", new_callable=AsyncMock, return_value={"restarted": False, "reason": "mocked"}):
        r = c.patch(f"/api/teams/{ids['team']}", headers=login("alice"),
                    json={"android_enabled": True, "android_software": False})
        assert r.status_code == 400
        assert "geen KVM" in r.text
        ok = c.patch(f"/api/teams/{ids['team']}", headers=login("alice"),
                     json={"android_enabled": True, "android_software": True})
        assert ok.status_code == 200, ok.text
        assert ok.json()["android_software"] is True


def test_home_android_settings(client):
    c, ids, login = client
    h = c.get("/api/home", headers=login("alice"))
    assert h.status_code == 200, h.text
    body = h.json()
    assert body["android_enabled"] is False
    assert "android_status" in body
    r = c.patch("/api/home", headers=login("alice"), json={
        "android_enabled": True, "android_software": True, "android_memory_mb": 3072, "android_cpus": 2,
    })
    assert r.status_code == 200, r.text
    assert r.json()["android_enabled"] is True
    assert r.json()["android_software"] is True
    assert r.json()["android_memory_mb"] == 3072
    again = c.get("/api/home", headers=login("alice")).json()
    assert again["android_enabled"] is True
    assert again["android_cpus"] == 2
