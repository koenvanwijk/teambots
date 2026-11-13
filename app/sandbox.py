"""The bot's computer: a persistent workspace with a shell.

Sandbox key: "p<project id>". Every project (team) has exactly one computer, shared by all of
its people and bots; nothing is shared between projects.

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


def _safe_key(key: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+", key):
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


async def sb_exec(key: str, argv: list[str], stdin: bytes | None = None, env: dict | None = None,
                  timeout: float = 60, raw: bool = False):
    """Run a command in the sandbox. Returns (exit code, output); output is bytes when raw=True."""
    key = _safe_key(key)
    if config.SANDBOX_BACKEND == "docker":
        await _ensure_container(f"tb-{key}")
    cmd, cwd, full_env = _command(key, argv, env or {})
    return await _run(cmd, stdin, timeout, cwd=cwd, env=full_env, raw=raw)


async def sb_spawn(key: str, argv: list[str], env: dict | None = None) -> None:
    """Start a long-running process in the sandbox (a virtual screen, a browser) and return at once."""
    key = _safe_key(key)
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


async def read_file(key: str, path: str) -> str:
    path = _check_path(path)
    code, out = await sb_exec(key, ["cat", "--", path])
    return out if code == 0 else f"error: {out}"
