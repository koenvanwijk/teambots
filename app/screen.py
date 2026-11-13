"""A screen per bot: a virtual X display inside the project's sandbox, with a browser.

The model looks with `screen_screenshot` and acts with mouse and keyboard tools. People watch
the same screen in the web UI. Needs Xvfb, xdotool, ImageMagick and a Chromium in the sandbox
image (sandbox/Dockerfile installs them) and a model that accepts image input.
"""
import asyncio

from . import config
from .sandbox import sb_exec, sb_spawn

WIDTH, HEIGHT = 1280, 800


def _display(bot_id: int) -> int:
    return 100 + bot_id


async def _env(key: str, bot_id: int) -> dict:
    """Make sure this bot's display is up and return the environment that points at it."""
    n = _display(bot_id)
    env = {"DISPLAY": f":{n}"}
    code, _ = await sb_exec(key, ["xdotool", "getdisplaygeometry"], env=env, timeout=10)
    if code == 0:
        return env
    await sb_exec(key, ["sh", "-c", f"rm -f /tmp/.X{n}-lock /tmp/.X11-unix/X{n}"])  # stale after a restart
    await sb_spawn(key, ["Xvfb", f":{n}", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"])
    for _ in range(40):
        await asyncio.sleep(0.25)
        code, _ = await sb_exec(key, ["xdotool", "getdisplaygeometry"], env=env, timeout=10)
        if code == 0:
            return env
    raise RuntimeError("the screen did not start; does the sandbox image contain Xvfb and xdotool?")


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
