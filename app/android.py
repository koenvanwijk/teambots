"""Optional Android emulator inside a TeamBots sandbox.

The lean `teambots-sandbox` image stays unchanged. When a project (or the overview
bot) turns the option on, the sandbox container is recreated from
`teambots-sandbox-android` with `/dev/kvm` if present. The emulator window is
shown on the bot's existing Xvfb / noVNC screen. `adb` is available via sandbox
exec. See docs/android-emulator.md.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import config
from . import sandbox
from .sandbox import AndroidUnavailable

log = logging.getLogger("teambots.android")

SETUP_TIMEOUT = 900
BOOT_POLL_S = 90
AVD = "teambots"


def android_env(spec: sandbox.SandboxSpec, display: str | None = None) -> dict[str, str]:
    env = {
        "ANDROID_SDK_ROOT": "/opt/android-sdk",
        "ANDROID_HOME": "/opt/android-sdk",
        "ANDROID_AVD_HOME": "/workspace/.android/avd",
        "ANDROID_EMULATOR_HOME": "/workspace/.android",
        "TB_ANDROID_AVD": AVD,
        "TB_ANDROID_ACCEL": "off" if spec.android_software or not spec.kvm_device else "on",
        "TB_ANDROID_GUEST_MEMORY": "2048",
        "TB_ANDROID_GUEST_CORES": "2",
        "LIBGL_ALWAYS_SOFTWARE": "1",
    }
    if display:
        env["DISPLAY"] = display
    return env


async def _path_env(key: str, extra: dict | None = None) -> dict[str, str]:
    env = dict(extra or {})
    # Keep the image PATH and put the SDK tools first once the volume is populated.
    code, out = await sandbox.sb_exec(
        key, ["sh", "-c", "printf %s \"$PATH\""], timeout=10)
    path = (out or "").strip() if code == 0 else ""
    prefix = "/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin"
    env["PATH"] = prefix + (":" + path if path else ":/usr/local/bin:/usr/bin:/bin")
    return env


async def emulator_running(key: str) -> bool:
    code, out = await sandbox.sb_exec(
        key, ["sh", "-c", "command -v adb >/dev/null && adb devices | grep -E 'emulator-[0-9]+\\s+device'"],
        timeout=15)
    return code == 0 and "emulator-" in (out or "")


async def boot_completed(key: str) -> bool:
    env = await _path_env(key)
    code, out = await sandbox.sb_exec(
        key, ["sh", "-c", "adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\\r'"],
        env=env, timeout=20)
    return code == 0 and (out or "").strip() == "1"


async def ensure_emulator(key: str, bot_id: int, display_env: dict) -> None:
    """Start the emulator on this bot's X display if the sandbox has Android on.

    No-op when the option is off. Failures are logged; the regular screen still works.
    """
    spec = sandbox.spec_for(key)
    if not spec.android_enabled:
        return
    if await emulator_running(key) or await boot_completed(key):
        return
    env = await _path_env(key, {**android_env(spec, display_env.get("DISPLAY")), **display_env})
    setup = "/usr/local/bin/teambots-android-setup"
    code, out = await sandbox.sb_exec(key, ["sh", "-c", f"command -v {setup} || test -x {setup}"], timeout=15)
    if code != 0:
        log.warning("android setup script missing in sandbox %s", key)
        return
    code, out = await sandbox.sb_exec(key, [setup], env=env, timeout=SETUP_TIMEOUT)
    if code != 0:
        log.error("android SDK setup failed in %s: %s", key, (out or "")[-500:])
        raise RuntimeError("Android SDK-setup in de sandbox is mislukt")
    if await emulator_running(key) or await boot_completed(key):
        return
    await sandbox.sb_spawn(key, ["/usr/local/bin/teambots-android-emulator"], env=env)
    for _ in range(BOOT_POLL_S // 2):
        await asyncio.sleep(2)
        if await boot_completed(key):
            return
    log.warning("android emulator in %s did not finish booting within %ss", key, BOOT_POLL_S)


async def screencap(key: str) -> bytes:
    """PNG bytes from `adb exec-out screencap -p`, or empty if adb is not ready."""
    env = await _path_env(key)
    code, data = await sandbox.sb_exec(
        key, ["sh", "-c", "adb exec-out screencap -p"], env=env, timeout=30, raw=True)
    if code != 0 or not data:
        return b""
    return data if isinstance(data, (bytes, bytearray)) else b""
