"""A screen per bot: a virtual X display inside the project's sandbox, with a browser.

The model looks with `screen_screenshot` and acts with mouse and keyboard tools. People watch
the same screen in the web UI, and can take over with mouse and keyboard through noVNC
(x11vnc attached to this Xvfb, proxied by TeamBots). Needs Xvfb, xdotool, ImageMagick and a
Chromium in the sandbox image (sandbox/Dockerfile installs them, plus x11vnc/noVNC) and a
model that accepts image input.
"""
import asyncio
import logging

from . import config
from .sandbox import sandbox_addr, sb_exec, sb_spawn

log = logging.getLogger("teambots.screen")

WIDTH, HEIGHT = 1280, 800


def _display(bot_id: int) -> int:
    return 100 + bot_id


def vnc_port(bot_id: int) -> int:
    return 5900 + bot_id


def ws_port(bot_id: int) -> int:
    return 6080 + bot_id


async def _port_open(key: str, port: int) -> bool:
    code, out = await sb_exec(
        key,
        ["python3", "-c",
         "import socket; s=socket.socket(); s.settimeout(0.4); "
         f"print(s.connect_ex(('127.0.0.1',{int(port)}))==0)"],
        timeout=10,
    )
    return code == 0 and "True" in (out or "")


async def _has_vnc_tools(key: str) -> bool:
    code, _ = await sb_exec(key, ["sh", "-c", "command -v x11vnc && command -v websockify"], timeout=10)
    return code == 0


async def _ensure_vnc(key: str, bot_id: int, env: dict) -> None:
    """Attach x11vnc + websockify to this bot's existing Xvfb. No-op if the tools are absent."""
    if not await _has_vnc_tools(key):
        return
    rfb, ws = vnc_port(bot_id), ws_port(bot_id)
    n = _display(bot_id)
    if not await _port_open(key, rfb):
        await sb_spawn(key, [
            "x11vnc", "-display", f":{n}", "-rfbport", str(rfb),
            "-localhost", "-forever", "-shared", "-nopw", "-noxdamage", "-noshm",
            "-ncache", "0", "-quiet",
        ], env=env)
        for _ in range(40):
            await asyncio.sleep(0.15)
            if await _port_open(key, rfb):
                break
        else:
            raise RuntimeError("x11vnc did not start; is it installed in the sandbox image?")
    if not await _port_open(key, ws):
        await sb_spawn(key, [
            "websockify", "--heartbeat", "30", f"0.0.0.0:{ws}", f"127.0.0.1:{rfb}",
            "--web=/usr/share/novnc",
        ], env=env)
        for _ in range(40):
            await asyncio.sleep(0.15)
            if await _port_open(key, ws):
                break
        else:
            raise RuntimeError("noVNC/websockify did not start; is it installed in the sandbox image?")


async def _env(key: str, bot_id: int, *, start_vnc: bool = True) -> dict:
    """Make sure this bot's display is up and return the environment that points at it."""
    n = _display(bot_id)
    env = {"DISPLAY": f":{n}"}
    code, _ = await sb_exec(key, ["xdotool", "getdisplaygeometry"], env=env, timeout=10)
    if code != 0:
        await sb_exec(key, ["sh", "-c", f"rm -f /tmp/.X{n}-lock /tmp/.X11-unix/X{n}"])  # stale after a restart
        await sb_spawn(key, ["Xvfb", f":{n}", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"])
        for _ in range(40):
            await asyncio.sleep(0.25)
            code, _ = await sb_exec(key, ["xdotool", "getdisplaygeometry"], env=env, timeout=10)
            if code == 0:
                break
        else:
            raise RuntimeError("the screen did not start; does the sandbox image contain Xvfb and xdotool?")
    if start_vnc:
        try:
            await _ensure_vnc(key, bot_id, env)
        except Exception:
            log.exception("interactive VNC did not start for bot %s (screenshot still works)", bot_id)
    return env


async def ensure_vnc(key: str, bot_id: int) -> tuple[str, int]:
    """Start the screen and VNC if needed; return (host, websockify port) reachable from TeamBots."""
    env = await _env(key, bot_id, start_vnc=False)
    await _ensure_vnc(key, bot_id, env)
    return await sandbox_addr(key), ws_port(bot_id)


async def screenshot(key: str, bot_id: int) -> bytes:
    env = await _env(key, bot_id)
    code, data = await sb_exec(key, ["import", "-window", "root", "-quality", "70", "jpg:-"], env=env,
                               timeout=30, raw=True)
    if code != 0 or not data:
        raise RuntimeError("could not take a screenshot")
    return data


async def open_url(key: str, bot_id: int, url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    env = await _env(key, bot_id)
    # A second start with the same profile opens the URL as a new tab in the running browser.
    # --no-sandbox is required: the sandbox container runs as unprivileged `bot` with all
    # capabilities dropped. The profile directory lives on the project's Docker volume.
    await sb_spawn(key, [config.SANDBOX_BROWSER, "--no-sandbox", "--no-first-run", "--disable-gpu",
                         "--disable-dev-shm-usage", f"--user-data-dir=.screens/bot{bot_id}",
                         f"--window-size={WIDTH},{HEIGHT}", "--window-position=0,0", url], env=env)
    await asyncio.sleep(3)
    return f"opened {url}; take a screenshot to see the page"


async def _xdo(key: str, bot_id: int, *args: str) -> str:
    env = await _env(key, bot_id)
    code, out = await sb_exec(key, ["xdotool", *args], env=env, timeout=30)
    return "done" if code == 0 else f"error: {out.strip()[:300]}"


async def click(key: str, bot_id: int, x: int, y: int, button: str = "left") -> str:
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return f"error: coordinates must be inside {WIDTH}x{HEIGHT}"
    num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
    return await _xdo(key, bot_id, "mousemove", str(x), str(y), "click", num)


async def type_text(key: str, bot_id: int, text: str) -> str:
    return await _xdo(key, bot_id, "type", "--delay", "15", "--", text)


async def press_key(key: str, bot_id: int, keys: str) -> str:
    return await _xdo(key, bot_id, "key", "--", *keys.split())


async def scroll(key: str, bot_id: int, amount: int) -> str:
    button = "5" if amount > 0 else "4"  # 5 = down
    return await _xdo(key, bot_id, "click", "--repeat", str(min(abs(amount), 30) or 1), button)
