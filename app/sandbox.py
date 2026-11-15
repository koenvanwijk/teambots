"""The bot's computer: a persistent workspace with a shell.

Sandbox key: "p<project id>" for a project computer, or "home-u<user id>" for a person's
overview bot. Project computers are shared by that project's people and bots; the overview
sandbox is private to that person and is never a project volume.

Backends:
  docker - one long-lived container plus a named volume per key (use this in production)
  local  - a plain subprocess in a directory under DATA_DIR (development only, NOT isolated)
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass

from . import config

MAX_OUTPUT = 8000
_started: set[str] = set()
_lock = asyncio.Lock()
_key_locks: dict[str, asyncio.Lock] = {}

ANDROID_UNAVAILABLE = "geen KVM, emulator niet beschikbaar"
ANDROID_MEMORY_MIN, ANDROID_MEMORY_MAX = 2048, 16384
ANDROID_CPUS_MIN, ANDROID_CPUS_MAX = 1, 8


class AndroidUnavailable(ValueError):
    """Raised when the Android emulator cannot be enabled on this host."""


@dataclass(frozen=True)
class SandboxSpec:
    """How a sandbox container should be created."""
    android_enabled: bool = False
    android_software: bool = False
    memory_mb: int = 2048
    cpus: float = 2.0
    kvm_device: bool = False
    kvm_gid: int | None = None


def container_name(key: str) -> str:
    return f"{config.SANDBOX_PREFIX}-{_safe_key(key)}"


def workspace_volume(key: str) -> str:
    """Named Docker volume for this sandbox (same as the container name)."""
    return container_name(key)


def kvm_available() -> bool:
    path = "/dev/kvm"
    return os.path.exists(path) and os.access(path, os.R_OK | os.W_OK)


def kvm_gid() -> int | None:
    try:
        return os.stat("/dev/kvm").st_gid
    except OSError:
        return None


def android_image_present() -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", config.SANDBOX_ANDROID_IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def host_android_status() -> dict:
    """What this machine can do, independent of a project setting."""
    docker = config.SANDBOX_BACKEND == "docker"
    kvm = kvm_available()
    image = android_image_present() if docker else False
    if not docker:
        message = "De Android-emulator werkt alleen met de Docker-sandbox (TB_SANDBOX=docker)."
    elif not kvm:
        message = ANDROID_UNAVAILABLE
    elif not image:
        message = (
            f"Het image {config.SANDBOX_ANDROID_IMAGE} ontbreekt. "
            "Bouw het met: docker build -t teambots-sandbox-android:latest "
            "-f sandbox/Dockerfile.android sandbox"
        )
    else:
        message = None
    return {
        "docker": docker,
        "kvm_available": kvm,
        "android_image_present": image,
        "android_image": config.SANDBOX_ANDROID_IMAGE,
        "message": message,
    }


def validate_android_limits(memory_mb: int | None, cpus: float | int | None) -> tuple[int, int]:
    mem = int(memory_mb) if memory_mb is not None else config.SANDBOX_ANDROID_MEMORY_MB
    cpu = int(cpus) if cpus is not None else int(config.SANDBOX_ANDROID_CPUS)
    if mem < ANDROID_MEMORY_MIN or mem > ANDROID_MEMORY_MAX:
        raise ValueError(f"android_memory_mb must be between {ANDROID_MEMORY_MIN} and {ANDROID_MEMORY_MAX}")
    if cpu < ANDROID_CPUS_MIN or cpu > ANDROID_CPUS_MAX:
        raise ValueError(f"android_cpus must be between {ANDROID_CPUS_MIN} and {ANDROID_CPUS_MAX}")
    return mem, cpu


def validate_android_enable(*, enabled: bool, software: bool) -> None:
    """Refuse to turn the emulator on when this host cannot run it.

    On the local (non-Docker) backend the setting may still be saved; the
    emulator only starts in a Docker sandbox, and apply_spec is then a no-op.
    """
    if not enabled:
        return
    if config.SANDBOX_BACKEND != "docker":
        return
    if not software and not kvm_available():
        raise AndroidUnavailable(ANDROID_UNAVAILABLE)
    if not android_image_present():
        raise AndroidUnavailable(
            f"Het image {config.SANDBOX_ANDROID_IMAGE} ontbreekt. "
            "Bouw het met: docker build -t teambots-sandbox-android:latest "
            "-f sandbox/Dockerfile.android sandbox")


def docker_run_argv(name: str, spec: SandboxSpec) -> list[str]:
    """Build the `docker run` argv for a sandbox container. Pure; safe to unit-test."""
    image = config.SANDBOX_ANDROID_IMAGE if spec.android_enabled else config.SANDBOX_IMAGE
    pids = "1024" if spec.android_enabled else "512"
    shm = "1g" if spec.android_enabled else "512m"
    argv = [
        "docker", "run", "-d", "--name", name,
        "--memory", f"{int(spec.memory_mb)}m",
        "--cpus", str(spec.cpus),
        "--pids-limit", pids,
        "--shm-size", shm,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--network", config.SANDBOX_NETWORK,
        "-v", f"{name}:/workspace",
        "-w", "/workspace",
        "--label", f"teambots.android={'1' if spec.android_enabled else '0'}",
        "--label", f"teambots.android.software={'1' if spec.android_software else '0'}",
    ]
    if spec.android_enabled:
        sdk = config.SANDBOX_ANDROID_SDK_VOLUME
        path = (
            "/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:"
            "/opt/android-sdk/cmdline-tools/latest/bin:"
            "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        argv += [
            "-v", f"{sdk}:/opt/android-sdk",
            "-e", "ANDROID_SDK_ROOT=/opt/android-sdk",
            "-e", "ANDROID_HOME=/opt/android-sdk",
            "-e", "ANDROID_AVD_HOME=/workspace/.android/avd",
            "-e", "ANDROID_EMULATOR_HOME=/workspace/.android",
            "-e", "TB_ANDROID=1",
            "-e", f"TB_ANDROID_ACCEL={'off' if (spec.android_software or not spec.kvm_device) else 'on'}",
            "-e", f"PATH={path}",
        ]
        if spec.kvm_device:
            argv += ["--device", "/dev/kvm"]
            if spec.kvm_gid is not None:
                argv += ["--group-add", str(spec.kvm_gid)]
        elif not spec.android_software:
            raise AndroidUnavailable(ANDROID_UNAVAILABLE)
    argv += [image, "sleep", "infinity"]
    return argv


def _settings_from_row(enabled: bool, software: bool, memory_mb: int | None, cpus: int | float | None) -> SandboxSpec:
    if enabled:
        mem, cpu = validate_android_limits(memory_mb, cpus)
        use_kvm = kvm_available() and not software
        return SandboxSpec(
            android_enabled=True,
            android_software=bool(software),
            memory_mb=mem,
            cpus=float(cpu),
            kvm_device=use_kvm,
            kvm_gid=kvm_gid() if use_kvm else None,
        )
    return SandboxSpec(
        android_enabled=False,
        android_software=False,
        memory_mb=config.SANDBOX_MEMORY_MB,
        cpus=float(config.SANDBOX_CPUS),
        kvm_device=False,
        kvm_gid=None,
    )


def spec_for(key: str) -> SandboxSpec:
    """Look up android/resource settings for a sandbox key (p<id> or home-u<id>)."""
    key = _safe_key(key)
    kind, ident = _parse_key(key)
    from .db import Team, User, session
    with session() as s:
        if kind == "project":
            row = s.get(Team, ident)
            if not row:
                return _settings_from_row(False, False, None, None)
            return _settings_from_row(
                bool(getattr(row, "android_enabled", False)),
                bool(getattr(row, "android_software", False)),
                getattr(row, "android_memory_mb", None),
                getattr(row, "android_cpus", None),
            )
        row = s.get(User, ident)
        if not row:
            return _settings_from_row(False, False, None, None)
        return _settings_from_row(
            bool(getattr(row, "home_android_enabled", False)),
            bool(getattr(row, "home_android_software", False)),
            getattr(row, "home_android_memory_mb", None),
            getattr(row, "home_android_cpus", None),
        )


def settings_dict(spec: SandboxSpec | None = None, *, key: str | None = None) -> dict:
    if spec is None and key is not None:
        spec = spec_for(key)
    if spec is None:
        spec = SandboxSpec()
    return {
        "android_enabled": spec.android_enabled,
        "android_software": spec.android_software,
        "android_memory_mb": int(spec.memory_mb) if spec.android_enabled else config.SANDBOX_ANDROID_MEMORY_MB,
        "android_cpus": int(spec.cpus) if spec.android_enabled else int(config.SANDBOX_ANDROID_CPUS),
    }


def _parse_key(key: str) -> tuple[str, int]:
    if key.startswith("home-u") and key[6:].isdigit():
        return "home", int(key[6:])
    if key.startswith("p") and key[1:].isdigit():
        return "project", int(key[1:])
    raise ValueError("bad sandbox key")


def _safe_key(key: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key):
        raise ValueError("bad sandbox key")
    return key


async def _run(argv: list[str], stdin: bytes | None, timeout: float, cwd: str | None = None,
               env: dict | None = None, raw: bool = False):
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, (b"" if raw else f"[timed out after {int(timeout)}s]")
    except asyncio.CancelledError:  # the turn was interrupted: do not leave the command running
        proc.kill()
        raise
    if raw:
        return proc.returncode or 0, out
    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT // 2] + "\n[... output truncated ...]\n" + text[-MAX_OUTPUT // 2:]
    return proc.returncode or 0, text


async def _create_container(name: str, spec: SandboxSpec) -> None:
    argv = docker_run_argv(name, spec)
    code, out = await _run(argv, None, 120)
    if code != 0:
        raise RuntimeError(f"could not start sandbox: {out.strip()[:300]}")


async def _ensure_container(name: str, *, key: str | None = None) -> None:
    """Start the container if it exists; create it only when it is absent.

    Existing containers are never recreated here, so a code deploy does not
    restart production sandboxes. Toggling Android or resource limits calls
    apply_spec(), which recreates explicitly.
    """
    async with _lock:
        if name in _started:
            return
        code, out = await _run(["docker", "inspect", "-f", "{{.State.Running}}", name], None, 20)
        if code != 0:
            spec = spec_for(key) if key else SandboxSpec()
            if spec.android_enabled:
                validate_android_enable(enabled=True, software=spec.android_software)
            await _create_container(name, spec)
        elif "true" not in out:
            await _run(["docker", "start", name], None, 60)
        _started.add(name)


async def apply_spec(key: str) -> dict:
    """Recreate the Docker container so new Android/resource settings take effect.

    The named workspace volume is kept. No-op for the local backend, or when the
    container does not exist yet (it will be created with the new spec later).
    """
    key = _safe_key(key)
    if config.SANDBOX_BACKEND != "docker":
        return {"restarted": False, "reason": "local"}
    spec = spec_for(key)
    if spec.android_enabled:
        validate_android_enable(enabled=True, software=spec.android_software)
    name = container_name(key)
    async with _key_lock(key), _lock:
        code, _ = await _run(["docker", "inspect", "-f", "{{.Id}}", name], None, 20)
        if code != 0:
            _started.discard(name)
            return {"restarted": False, "reason": "absent"}
        await _run(["docker", "stop", "-t", "15", name], None, 60)
        await _run(["docker", "rm", "-f", name], None, 30)
        _started.discard(name)
        await _create_container(name, spec)
        _started.add(name)
    return {"restarted": True, "reason": "recreated"}


def _command(key: str, argv: list[str], env: dict, detach: bool = False) -> tuple[list[str], str | None, dict | None]:
    """(argv, cwd, env) to run `argv` inside the sandbox `key`."""
    if config.SANDBOX_BACKEND == "docker":
        cmd = ["docker", "exec", "-d" if detach else "-i", "-w", "/workspace"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        return cmd + [container_name(key)] + argv, None, None
    workdir = os.path.abspath(os.path.join(config.DATA_DIR, "workspaces", key))
    os.makedirs(workdir, exist_ok=True)
    return argv, workdir, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": workdir, **env}


def _key_lock(key: str) -> asyncio.Lock:
    """One lock per sandbox, so two bot runs on the same project computer do not interleave."""
    lock = _key_locks.get(key)
    if lock is None:
        lock = _key_locks[key] = asyncio.Lock()
    return lock


async def sb_exec(key: str, argv: list[str], stdin: bytes | None = None, env: dict | None = None,
                  timeout: float = 60, raw: bool = False):
    """Run a command in the sandbox. Returns (exit code, output); output is bytes when raw=True."""
    key = _safe_key(key)
    async with _key_lock(key):
        if config.SANDBOX_BACKEND == "docker":
            await _ensure_container(container_name(key), key=key)
        cmd, cwd, full_env = _command(key, argv, env or {})
        return await _run(cmd, stdin, timeout, cwd=cwd, env=full_env, raw=raw)


async def sb_spawn(key: str, argv: list[str], env: dict | None = None) -> None:
    """Start a long-running process in the sandbox (a virtual screen, a browser) and return at once."""
    key = _safe_key(key)
    async with _key_lock(key):
        if config.SANDBOX_BACKEND == "docker":
            await _ensure_container(container_name(key), key=key)
            cmd, _, _ = _command(key, argv, env or {}, detach=True)
            await _run(cmd, None, 30)
            return
        cmd, cwd, full_env = _command(key, argv, env or {})
        await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=full_env, stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                             start_new_session=True)


def _check_path(path: str) -> str:
    if path.startswith("/workspace/"):
        path = path[len("/workspace/"):]
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("path must be relative to the workspace")
    return path


async def run_command(key: str, command: str, env: dict | None = None, timeout: float = 60) -> str:
    code, out = await sb_exec(key, ["sh", "-c", command], env=env, timeout=min(max(timeout, 1), 300))
    return f"exit code {code}\n{out}"


async def write_file(key: str, path: str, content: str) -> str:
    path = _check_path(path)
    code, out = await sb_exec(
        key, ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], stdin=content.encode())
    return f"wrote {len(content)} characters to {path}" if code == 0 else f"error: {out}"


async def read_file(key: str, path: str) -> str:
    path = _check_path(path)
    code, out = await sb_exec(key, ["cat", "--", path])
    return out if code == 0 else f"error: {out}"


async def sandbox_addr(key: str) -> str:
    """Address of the sandbox from the TeamBots host (container IP, or 127.0.0.1 when local).

    Used to reach noVNC/websockify inside a Docker sandbox without publishing ports on the LAN.
    Requires TB_SANDBOX_NETWORK to give the container an address (the default `bridge` does).
    """
    key = _safe_key(key)
    if config.SANDBOX_BACKEND != "docker":
        return "127.0.0.1"
    name = container_name(key)
    await _ensure_container(name, key=key)
    code, out = await _run([
        "docker", "inspect", "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name], None, 20)
    ip = (out or "").split()[0] if code == 0 else ""
    if not ip:
        raise RuntimeError(
            "sandbox has no network address; interactive screen needs TB_SANDBOX_NETWORK=bridge (the default)")
    return ip


SKIP_DIRS = {"node_modules", "__pycache__"}


async def list_files(key: str, limit: int = 300) -> list[str]:
    """Workspace files as relative paths, for the '@' menu in the UI. Hidden folders are left out."""
    key = _safe_key(key)
    if config.SANDBOX_BACKEND != "docker":
        root = os.path.abspath(os.path.join(config.DATA_DIR, "workspaces", key))
        found: list[str] = []
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for n in sorted(names):
                if not n.startswith("."):
                    found.append(os.path.relpath(os.path.join(folder, n), root).replace(os.sep, "/"))
                    if len(found) >= limit:
                        return found
        return found
    prune = " -o ".join(f"-name {d}" for d in sorted(SKIP_DIRS))
    code, out = await sb_exec(key, ["sh", "-c", f"find . \\( -name '.?*' -o {prune} \\) -prune -o -type f -print "
                                                 f"| head -n {int(limit)}"], timeout=10)
    if code != 0:
        return []
    return sorted(line[2:] for line in out.splitlines() if line.startswith("./"))
