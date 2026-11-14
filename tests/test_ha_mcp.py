"""Home Assistant MCP: lock-entity approval guard, and unauthenticated probe (401 only)."""
import os
import sys
import tempfile
import types

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-ha-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.agent import is_ha_lock_action, needs_approval, Ctx  # noqa: E402


class _Srv(types.SimpleNamespace):
    pass


def test_is_ha_lock_action_detects_entity_and_service():
    ha = _Srv(name="home-assistant", url="http://127.0.0.1:8123/api/mcp")
    other = _Srv(name="homey", url="https://mcp.athom.com")
    assert is_ha_lock_action(ha, "HassTurnOn", {"entity_id": "lock.front_door"})
    assert is_ha_lock_action(ha, "HassCallService", {"domain": "lock", "service": "unlock",
                                                    "entity_id": "lock.bold_voordeur"})
    assert is_ha_lock_action(ha, "hass_call_service", {"service": "lock.unlock"})
    assert not is_ha_lock_action(ha, "HassTurnOn", {"entity_id": "light.kitchen"})
    assert not is_ha_lock_action(other, "HassTurnOn", {"entity_id": "lock.front_door"})
    assert not is_ha_lock_action(ha, "HassGetState", {"entity_id": "sensor.temperature"})


def test_ha_lock_requires_approval_even_if_trusted_and_auto():
    ha = _Srv(name="home-assistant", url="http://127.0.0.1:8123/api/mcp", trusted=True)
    bot = types.SimpleNamespace(id=1, approval_mode="auto")
    ctx = Ctx(bot_id=1, requester_id=1, conv_id=1, is_channel=False, team_id=1)
    mcp_map = {"HassTurnOn": (ha, "HassTurnOn")}
    assert needs_approval(bot, ctx, "HassTurnOn", mcp_map, {"entity_id": "lock.voordeur"}) is True
    assert needs_approval(bot, ctx, "HassTurnOn", mcp_map, {"entity_id": "light.kitchen"}) is False


def test_unauthenticated_ha_mcp_returns_401():
    """No token is sent; we only check that HA's MCP endpoint rejects anonymous POSTs."""
    import httpx
    try:
        r = httpx.post("http://127.0.0.1:8123/api/mcp",
                       headers={"Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "teambots-test", "version": "0"}}},
                       timeout=2)
    except httpx.HTTPError as e:
        pytest.skip(f"Home Assistant not reachable: {e.__class__.__name__}")
    assert r.status_code == 401, r.text[:200]
    # A GET is not part of the streamable-HTTP request path (the SDK tolerates 405).
    g = httpx.get("http://127.0.0.1:8123/api/mcp", timeout=2)
    assert g.status_code == 405
