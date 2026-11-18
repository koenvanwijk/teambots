"""One-time move of identical bot secrets and MCP servers to their project."""
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-projconn-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.db import AppSetting, Bot, McpCredential, McpServer, Secret, select, session  # noqa: E402


@pytest.fixture(autouse=True)
def _engine():
    # Own database per test, so other suites' engine rebinding cannot touch it.
    import app.db as db
    import app.home as home
    from sqlmodel import create_engine
    saved_db_engine, saved_home_engine = db.engine, home.engine
    test_engine = create_engine(f"sqlite:///{tempfile.mkdtemp(prefix='teambots-projconn-db-')}/t.db",
                                connect_args={"check_same_thread": False})
    db.engine = home.engine = test_engine
    db.init_db()
    with session() as s:  # init_db already ran the (empty) migration; let the test run it for real
        s.delete(s.get(AppSetting, "project_connections_v1"))
        s.commit()
    yield db
    db.engine = saved_db_engine
    home.engine = saved_home_engine


def _bots(*team_ids, home=False):
    with session() as s:
        out = []
        for i, team_id in enumerate(team_ids):
            b = Bot(team_id=team_id, owner_id=1, name=f"b{i}", handle=f"b{i}", endpoint_id=1, is_home=home)
            s.add(b)
            s.commit()
            out.append(b.id)
        return out


def _secret(bot_id, name="TOKEN", value="v-1", hosts="api.test"):
    with session() as s:
        s.add(Secret(bot_id=bot_id, name=name, value_enc=config.encrypt(value), hosts=hosts))
        s.commit()


def _project_secrets(team_id):
    with session() as s:
        return [(r.name, config.decrypt(r.value_enc), r.hosts)
                for r in s.exec(select(Secret).where(Secret.team_id == team_id, Secret.bot_id == 0)).all()]


def _bot_secret_names(bot_id):
    with session() as s:
        return sorted(r.name for r in s.exec(select(Secret).where(Secret.bot_id == bot_id)).all())


def test_identical_secrets_move_to_the_project(_engine):
    a, b, c = _bots(1, 1, 1)
    _secret(a, hosts="api.test,www.test")
    _secret(b, hosts="WWW.test, api.test")  # same hosts, written differently
    _secret(c, value="other")  # a different value stays with its bot and keeps overriding
    _secret(a, name="ONLY_A")  # held by one bot: not shared
    out = _engine.migrate_project_connections()
    assert out["secrets"] == 1 and not out["already"]
    assert _project_secrets(1) == [("TOKEN", "v-1", "api.test,www.test")]
    assert _bot_secret_names(a) == ["ONLY_A"] and _bot_secret_names(b) == []
    assert _bot_secret_names(c) == ["TOKEN"]
    from app import secrets_http
    assert secrets_http.load(c)["TOKEN"][0] == "other"
    assert secrets_http.load(b)["TOKEN"][0] == "v-1"
    # runs once only
    _secret(a, name="LATER")
    _secret(b, name="LATER")
    assert _engine.migrate_project_connections()["already"]
    assert _bot_secret_names(a) == ["LATER", "ONLY_A"]


def test_most_common_version_wins_and_projects_stay_apart(_engine):
    a, b, c, d = _bots(1, 1, 1, 2)
    _secret(a, value="x")
    _secret(b, value="y")
    _secret(c, value="y")
    _secret(d, value="y")  # another project: the same value does not make it shared across projects
    _engine.migrate_project_connections()
    assert _project_secrets(1) == [("TOKEN", "y", "api.test")]
    assert _project_secrets(2) == []
    assert _bot_secret_names(a) == ["TOKEN"] and _bot_secret_names(d) == ["TOKEN"]


def test_home_bots_are_left_alone(_engine):
    a, b = _bots(0, 0, home=True)
    _secret(a)
    _secret(b)
    _engine.migrate_project_connections()
    assert _bot_secret_names(a) == ["TOKEN"] and _bot_secret_names(b) == ["TOKEN"]
    assert _project_secrets(0) == []


def test_identical_mcp_servers_move_with_newest_credentials(_engine):
    from datetime import datetime, timedelta, timezone
    a, b, c = _bots(1, 1, 1)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    with session() as s:
        servers = []
        for bot_id in (a, b):
            srv = McpServer(bot_id=bot_id, name="crm", url="https://crm.test/mcp", auth_mode="token",
                            auth_scope="user", token_enc="")
            s.add(srv)
            s.commit()
            servers.append(srv.id)
        s.add(McpServer(bot_id=c, name="crm", url="https://other.test/mcp"))  # different URL: stays
        s.add(McpCredential(mcp_server_id=servers[0], user_id=7, value_enc=config.encrypt("old"), updated_at=old))
        s.add(McpCredential(mcp_server_id=servers[1], user_id=7, value_enc=config.encrypt("new"), updated_at=new))
        s.add(McpCredential(mcp_server_id=servers[1], user_id=8, value_enc=config.encrypt("eight"), updated_at=old))
        s.commit()
    assert _engine.migrate_project_connections()["mcp"] == 1
    with session() as s:
        shared = s.exec(select(McpServer).where(McpServer.team_id == 1, McpServer.bot_id == 0)).all()
        assert [(m.name, m.url, m.auth_scope) for m in shared] == [("crm", "https://crm.test/mcp", "user")]
        creds = {cr.user_id: config.decrypt(cr.value_enc) for cr in s.exec(select(McpCredential)).all()}
        assert creds == {7: "new", 8: "eight"}
        assert all(cr.mcp_server_id == shared[0].id for cr in s.exec(select(McpCredential)).all())
        assert [m.bot_id for m in s.exec(select(McpServer).where(McpServer.bot_id != 0)).all()] == [c]
        from app.agent import mcp_servers_for
        assert [m.url for m in mcp_servers_for(s, s.get(Bot, a))] == ["https://crm.test/mcp"]
        assert [m.url for m in mcp_servers_for(s, s.get(Bot, c))] == ["https://other.test/mcp"]
