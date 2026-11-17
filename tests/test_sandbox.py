"""Sandbox read_file must not dump raw image or binary bytes into the model prompt."""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-sandbox-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import base64  # noqa: E402

# 1x1 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_describe_non_text_file_detects_png_and_binary():
    from app.sandbox import describe_non_text_file
    note = describe_non_text_file(TINY_PNG, "shot.png")
    assert note is not None
    assert "PNG image" in note
    assert "1x1" in note
    assert "shot.png" in note
    assert TINY_PNG not in note.encode()
    assert "bytes were not included" in note or "not included" in note
    assert describe_non_text_file(b"hello text\n", "note.txt") is None
    bin_note = describe_non_text_file(b"\x00\x01\x02binary", "blob.bin")
    assert bin_note is not None and "binary" in bin_note


def test_read_file_returns_note_for_png_not_bytes():
    from app import config, sandbox
    root = tempfile.mkdtemp(prefix="teambots-ws-")
    config.DATA_DIR = root
    config.SANDBOX_BACKEND = "local"
    ws = os.path.join(root, "workspaces", "p1")
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "shot.png"), "wb") as f:
        f.write(TINY_PNG)
    with open(os.path.join(ws, "note.txt"), "w") as f:
        f.write("hello-from-file")
    png = asyncio.run(sandbox.read_file("p1", "shot.png"))
    assert "PNG image" in png
    assert "1x1" in png
    assert "hello-from-file" not in png
    assert "\x89PNG" not in png
    text = asyncio.run(sandbox.read_file("p1", "note.txt"))
    assert text == "hello-from-file"
    missing = asyncio.run(sandbox.read_file("p1", "nope.txt"))
    assert missing.startswith("error:")


def test_screen_starts_xfce_once_when_available(monkeypatch):
    import importlib

    screen = importlib.import_module("app.screen")

    screen_key = ("p1", 7)
    screen._desktop_started.discard(screen_key)
    calls = {"wm": 0, "spawn": []}

    async def fake_exec(key, argv, env=None, timeout=60, raw=False):
        if argv[0] == "sh":
            return 0, ""
        calls["wm"] += 1
        if calls["wm"] == 1:
            return 1, ""
        return 0, "_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x1"

    async def fake_spawn(key, argv, env=None):
        calls["spawn"].append((argv, env))

    monkeypatch.setattr(screen, "sb_exec", fake_exec)
    monkeypatch.setattr(screen, "sb_spawn", fake_spawn)

    async def check():
        await screen._ensure_desktop("p1", 7, {"DISPLAY": ":107"})
        await screen._ensure_desktop("p1", 7, {"DISPLAY": ":107"})

    asyncio.run(check())
    assert calls["spawn"] == [(["dbus-launch", "--exit-with-session", "xfce4-session"], {"DISPLAY": ":107"})]
    assert screen_key in screen._desktop_started
