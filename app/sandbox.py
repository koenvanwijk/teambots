"""The bot's computer: a persistent workspace with a shell.

Sandbox key: "p<project id>" for a project computer, or "home-u<user id>" for a person's
overview bot. Project computers are shared by that project's people and bots; the overview
sandbox is private to that person and is never a project volume.

Backends:
  docker - one long-lived container plus a named volume per key (use this in production)
  local  - a plain subprocess in a directory under DATA_DIR (development only, NOT isolated)
"""
import asyncio
import os
import re

from . import config

MAX_OUTPUT = 8000
_started: set[str] = set()
_lock = asyncio.Lock()
_key_locks: dict[str, asyncio.Lock] = {}


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


async def _ensure_container(name: str) -> None:
    async with _lock:
        if name in _started:
            return
        code, out = await _run(["docker", "inspect", "-f", "{{.State.Running}}", name], None, 20)
        if code != 0:
            code, out = await _run([
                "docker", "run", "-d", "--name", name, "--memory", "2g", "--cpus", "2",
                "--pids-limit", "512", "--shm-size", "512m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--network", config.SANDBOX_NETWORK, "-v", f"{name}:/workspace", "-w", "/workspace",
                config.SANDBOX_IMAGE, "sleep", "infinity"], None, 120)
            if code != 0:
                raise RuntimeError(f"could not start sandbox: {out.strip()[:300]}")
        elif "true" not in out:
            await _run(["docker", "start", name], None, 60)
        _started.add(name)


def _command(key: str, argv: list[str], env: dict, detach: bool = False) -> tuple[list[str], str | None, dict | None]:
    """(argv, cwd, env) to run `argv` inside the sandbox `key`."""
    if config.SANDBOX_BACKEND == "docker":
        cmd = ["docker", "exec", "-d" if detach else "-i", "-w", "/workspace"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        return cmd + [f"tb-{key}"] + argv, None, None
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
            await _ensure_container(f"tb-{key}")
        cmd, cwd, full_env = _command(key, argv, env or {})
        return await _run(cmd, stdin, timeout, cwd=cwd, env=full_env, raw=raw)


async def sb_spawn(key: str, argv: list[str], env: dict | None = None) -> None:
    """Start a long-running process in the sandbox (a virtual screen, a browser) and return at once."""
    key = _safe_key(key)
    async with _key_lock(key):
        if config.SANDBOX_BACKEND == "docker":
            await _ensure_container(f"tb-{key}")
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


_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF87, _GIF89 = b"GIF87a", b"GIF89a"
_WEBP = b"WEBP"


def describe_non_text_file(data: bytes, path: str) -> str | None:
    """If `data` is an image or other binary, return a short note instead of the bytes."""
    kind = ""
    extra = ""
    if data.startswith(_PNG):
        kind = "PNG image"
        if len(data) >= 24:
            import struct
            width, height = struct.unpack(">II", data[16:24])
            extra = f", {width}x{height}"
    elif data.startswith(_JPEG):
        kind = "JPEG image"
    elif data.startswith(_GIF87) or data.startswith(_GIF89):
        kind = "GIF image"
    elif data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == _WEBP:
        kind = "WEBP image"
    if kind:
        return (f"{path} is a {kind} ({len(data)} bytes{extra}). "
                "read_file only returns text; the image bytes were not included. "
                "If the user attached this image in the chat, it is sent separately.")
    head = data[:8192]
    if b"\x00" in head:
        return (f"{path} is a binary file ({len(data)} bytes), not text. "
                "The contents were not included.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return (f"{path} is not valid UTF-8 text ({len(data)} bytes). "
                "The contents were not included.")
    return None


async def read_file(key: str, path: str) -> str:
    path = _check_path(path)
    code, out = await sb_exec(key, ["cat", "--", path], raw=True)
    if code != 0:
        text = out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)
        return f"error: {text}"
    note = describe_non_text_file(out, path)
    if note:
        return note
    text = out.decode()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT // 2] + "\n[... output truncated ...]\n" + text[-MAX_OUTPUT // 2:]
    return text


async def sandbox_addr(key: str) -> str:
    """Address of the sandbox from the TeamBots host (container IP, or 127.0.0.1 when local).

    Used to reach noVNC/websockify inside a Docker sandbox without publishing ports on the LAN.
    Requires TB_SANDBOX_NETWORK to give the container an address (the default `bridge` does).
    """
    key = _safe_key(key)
    if config.SANDBOX_BACKEND != "docker":
        return "127.0.0.1"
    name = f"tb-{key}"
    await _ensure_container(name)
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
