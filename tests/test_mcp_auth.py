"""Per-user and shared MCP auth header resolution (no real MCP server)."""
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-mcpauth-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, mcp_client  # noqa: E402
from app.db import McpCredential, McpServer, session  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _engine():
    # Own database, so collection order and other suites' engine rebinding cannot touch it.
    import app.db as db
    import app.home as home
    from sqlmodel import create_engine
    saved_db_engine, saved_home_engine = db.engine, home.engine
    test_engine = create_engine(f"sqlite:///{tempfile.mkdtemp(prefix='teambots-mcpauth-db-')}/t.db",
                                connect_args={"check_same_thread": False})
    db.engine = home.engine = test_engine
    db.init_db()
    yield
    db.engine = saved_db_engine
    home.engine = saved_home_engine


def _server(**kw):
    defaults = dict(bot_id=1, name="srv", url="http://example/mcp", auth_mode="none",
                    auth_scope="shared", auth_header="Authorization", auth_prefix="Bearer ", token_enc="")
    defaults.update(kw)
    with session() as s:
        srv = McpServer(**defaults)
        s.add(srv)
        s.commit()
        s.refresh(srv)
        return srv


def test_no_auth_returns_only_static_headers():
    srv = _server(headers_enc=config.encrypt('{"X-Trace": "1"}'))
    assert mcp_client.resolve_headers(srv) == {"X-Trace": "1"}


def test_shared_token():
    srv = _server(auth_mode="token", auth_scope="shared", token_enc=config.encrypt("abc"))
    assert mcp_client.resolve_headers(srv) == {"Authorization": "Bearer abc"}


def test_shared_token_missing_raises():
    srv = _server(auth_mode="token", auth_scope="shared", token_enc="")
    with pytest.raises(mcp_client.McpAuthError):
        mcp_client.resolve_headers(srv)


def test_custom_header_and_empty_prefix():
    srv = _server(auth_mode="token", auth_scope="shared", auth_header="X-API-Key",
                  auth_prefix="", token_enc=config.encrypt("k-123"))
    assert mcp_client.resolve_headers(srv) == {"X-API-Key": "k-123"}


def test_per_user_not_connected_raises():
    srv = _server(auth_mode="token", auth_scope="user")
    with pytest.raises(mcp_client.McpAuthError):
        mcp_client.resolve_headers(srv, user_id=42)


def test_per_user_without_user_id_raises():
    srv = _server(auth_mode="token", auth_scope="user")
    with pytest.raises(mcp_client.McpAuthError):
        mcp_client.resolve_headers(srv)  # no user context at all


def test_per_user_connected_uses_that_users_token():
    srv = _server(auth_mode="token", auth_scope="user")
    with session() as s:
        s.add(McpCredential(mcp_server_id=srv.id, user_id=7, value_enc=config.encrypt("tok-7")))
        s.add(McpCredential(mcp_server_id=srv.id, user_id=8, value_enc=config.encrypt("tok-8")))
        s.commit()
    assert mcp_client.resolve_headers(srv, user_id=7) == {"Authorization": "Bearer tok-7"}
    assert mcp_client.resolve_headers(srv, user_id=8) == {"Authorization": "Bearer tok-8"}
    # a different user who hasn't connected is still blocked
    with pytest.raises(mcp_client.McpAuthError):
        mcp_client.resolve_headers(srv, user_id=9)
