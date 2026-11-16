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


async def _proc(argv: list[str], stdin: bytes | None, timeout: float, cwd: str | None = None,
                env: dict | None = None) -> tuple[int, bytes, bool]:
    """Run argv; (exit code, combined output, timed out). Killed on timeout or cancellation."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", True
    except asyncio.CancelledError:  # the turn was interrupted: do not leave the command running
        proc.kill()
        raise
    return proc.returncode or 0, out, False


def _format(code: int, out: bytes, timed_out: bool, timeout: float, raw: bool):
    if timed_out:
        return 124, (b"" if raw else f"[timed out after {int(timeout)}s]")
    if raw:
        return code, out
    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT // 2] + "\n[... output truncated ...]\n" + text[-MAX_OUTPUT // 2:]
    return code, text


async def _run(argv: list[str], stdin: bytes | None, timeout: float, cwd: str | None = None,
               env: dict | None = None, raw: bool = False):
    code, out, timed_out = await _proc(argv, stdin, timeout, cwd=cwd, env=env)
    return _format(code, out, timed_out, timeout, raw)


# ---------------------------------------------------------------- docker, direct or through sandboxd
# With TB_SANDBOXD_SOCKET set, the app has no Docker access at all: every container operation
# goes to sandboxd (app/sandboxd.py) over a Unix socket, and only sandboxd holds the Docker socket.
# sandboxd itself calls the *_direct functions below.

def _container_args(name: str) -> list[str]:
    args = ["docker", "run", "-d", "--name", name, "--memory", "2g", "--cpus", "2",
            "--pids-limit", "512", "--shm-size", "512m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--network", config.SANDBOX_NETWORK, "-v", f"{name}:/workspace", "-w", "/workspace"]
    if config.SANDBOX_RUNTIME:
        args += ["--runtime", config.SANDBOX_RUNTIME]
    if config.SANDBOX_RESOLV_CONF:
        args += ["-v", f"{config.SANDBOX_RESOLV_CONF}:/etc/resolv.conf:ro"]
    return args + [config.SANDBOX_IMAGE, "sleep", "infinity"]


async def _ensure_container(name: str) -> None:
    async with _lock:
        if name in _started:
            return
        code, out = await _run(["docker", "inspect", "-f", "{{.State.Running}} {{.HostConfig.Runtime}}", name],
                               None, 20)
        running, _, runtime = out.strip().partition(" ") if code == 0 else ("", "", "")
        if code == 0 and config.SANDBOX_RUNTIME and runtime != config.SANDBOX_RUNTIME:
            # Created under another runtime (e.g. before gVisor): recreate it. The named volume,
            # and so the workspace, is kept; only running processes are lost.
            await _run(["docker", "rm", "-f", name], None, 60)
            code = 1
        if code != 0:
            code, out = await _run(_container_args(name), None, 120)
            if code != 0:
                raise RuntimeError(f"could not start sandbox: {out.strip()[:300]}")
        elif running != "true":
            await _run(["docker", "start", name], None, 60)
        _started.add(name)


async def docker_exec_direct(name: str, argv: list[str], env: dict, stdin: bytes | None, timeout: float,
                             detach: bool = False) -> tuple[int, bytes, bool]:
    await _ensure_container(name)
    cmd = ["docker", "exec", "-d" if detach else "-i", "-w", "/workspace"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    return await _proc(cmd + [name] + argv, stdin, timeout)


async def docker_addr_direct(name: str) -> str:
    await _ensure_container(name)
    code, out = await _run([
        "docker", "inspect", "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name], None, 20)
    return (out or "").split()[0] if code == 0 and (out or "").split() else ""


def docker_destroy_direct(name: str) -> None:
    """Remove the container and its volume. Blocking; never raises when Docker is missing."""
    import subprocess
    _started.discard(name)
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", name], capture_output=True, timeout=30)
    except FileNotFoundError:
        pass  # no docker here


def _sandboxd_transport(sync: bool = False):
    import httpx
    cls = httpx.HTTPTransport if sync else httpx.AsyncHTTPTransport
    return cls(uds=config.SANDBOXD_SOCKET)


async def _sandboxd(path: str, body: dict, timeout: float) -> dict:
    import httpx
    async with httpx.AsyncClient(transport=_sandboxd_transport(), base_url="http://sandboxd",
                                 timeout=timeout) as client:
        r = await client.post(path, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"sandboxd {path}: {r.status_code} {r.text[:300]}")
    return r.json()


async def _docker_exec(name: str, argv: list[str], env: dict, stdin: bytes | None, timeout: float,
                       detach: bool = False) -> tuple[int, bytes, bool]:
    if not config.SANDBOXD_SOCKET:
        return await docker_exec_direct(name, argv, env, stdin, timeout, detach)
    import base64
    res = await _sandboxd("/exec", {
        "name": name, "argv": argv, "env": env, "timeout": timeout, "detach": detach,
        "stdin": base64.b64encode(stdin).decode() if stdin is not None else None,
    }, timeout + 150)  # room for a first container start
    return int(res["code"]), base64.b64decode(res["output"]), bool(res["timed_out"])


async def _docker_addr(name: str) -> str:
    if not config.SANDBOXD_SOCKET:
        return await docker_addr_direct(name)
    return (await _sandboxd("/addr", {"name": name}, 150))["ip"]


def destroy(key: str) -> None:
    """Remove the sandbox `key` and its workspace for good (container, volume, local folder). Blocking."""
    import shutil
    key = _safe_key(key)
    name = f"tb-{key}"
    if config.SANDBOXD_SOCKET:
        import httpx
        with httpx.Client(transport=_sandboxd_transport(sync=True), base_url="http://sandboxd", timeout=90) as c:
            r = c.post("/destroy", json={"name": name})
            if r.status_code != 200:
                raise RuntimeError(f"sandboxd /destroy: {r.status_code} {r.text[:300]}")
    else:
        docker_destroy_direct(name)
    local = os.path.join(config.DATA_DIR, "workspaces", key)
    if os.path.isdir(local):
        shutil.rmtree(local, ignore_errors=True)


def _local_command(key: str, argv: list[str], env: dict) -> tuple[list[str], str, dict]:
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
            code, out, timed_out = await _docker_exec(f"tb-{key}", argv, env or {}, stdin, timeout)
            return _format(code, out, timed_out, timeout, raw)
        cmd, cwd, full_env = _local_command(key, argv, env or {})
        return await _run(cmd, stdin, timeout, cwd=cwd, env=full_env, raw=raw)


async def sb_spawn(key: str, argv: list[str], env: dict | None = None) -> None:
    """Start a long-running process in the sandbox (a virtual screen, a browser) and return at once."""
    key = _safe_key(key)
    async with _key_lock(key):
        if config.SANDBOX_BACKEND == "docker":
            await _docker_exec(f"tb-{key}", argv, env or {}, None, 30, detach=True)
            return
        cmd, cwd, full_env = _local_command(key, argv, env or {})
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
    ip = await _docker_addr(f"tb-{key}")
    if not ip:
        raise RuntimeError(
            "sandbox has no network address; interactive screen needs TB_SANDBOX_NETWORK=bridge (the default)")
    return ip


SKIP_DIRS = {"node_modules", "__pycache__"}


async def list_files(key: str, limit: int = 300) -> list[str]:
    """Workspace files as relative paths, for the '@' menu in the UI. Hidden folders are left out."""
    return [f["path"] for f in await file_info(key, limit)]


# ---------------------------------------------------------------- project files from the web UI
# The workspace is the project's file store: what people upload lands next to what bots write.
# The local backend uses Python directly (it also works without a Unix shell); docker goes
# through the container, so the files live in the project's volume.

def _local_root(key: str) -> str:
    root = os.path.realpath(os.path.join(config.DATA_DIR, "workspaces", _safe_key(key)))
    os.makedirs(root, exist_ok=True)
    return root


def _local_path(key: str, path: str) -> str:
    root = _local_root(key)
    full = os.path.realpath(os.path.join(root, _check_path(path)))
    if not full.startswith(root + os.sep):  # a symlink in the workspace must not lead outside it
        raise ValueError("path must be relative to the workspace")
    return full


async def file_info(key: str, limit: int = 1000) -> list[dict]:
    """[{path, size, modified}] of every workspace file, hidden folders left out, sorted by path."""
    key = _safe_key(key)
    found: list[dict] = []
    if config.SANDBOX_BACKEND != "docker":
        root = _local_root(key)
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for n in sorted(names):
                if n.startswith("."):
                    continue
                full = os.path.join(folder, n)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found.append({"path": os.path.relpath(full, root).replace(os.sep, "/"), "size": st.st_size,
                              "modified": st.st_mtime})
                if len(found) >= limit:
                    return found
        return found
    prune = " -o ".join(f"-name {d}" for d in sorted(SKIP_DIRS))
    code, out = await sb_exec(key, ["sh", "-c", f"find . \\( -name '.?*' -o {prune} \\) -prune -o -type f "
                                                 f"-printf '%s\\t%T@\\t%P\\n' | head -n {int(limit)}"], timeout=15)
    if code != 0:
        return []
    for line in out.splitlines():
        size, _, rest = line.partition("\t")
        mtime, _, path = rest.partition("\t")
        if path and size.isdigit():
            found.append({"path": path, "size": int(size), "modified": float(mtime or 0)})
    return sorted(found, key=lambda f: f["path"])


async def put_bytes(key: str, path: str, data: bytes) -> None:
    if config.SANDBOX_BACKEND != "docker":
        full = _local_path(key, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return
    path = _check_path(path)
    code, out = await sb_exec(key, ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
                              stdin=data, timeout=300)
    if code != 0:
        raise RuntimeError(f"could not store the file: {out.strip()[:200]}")


async def get_bytes(key: str, path: str) -> bytes | None:
    """The file's content, or None when there is no such file."""
    if config.SANDBOX_BACKEND != "docker":
        full = _local_path(key, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as f:
            return f.read()
    code, data = await sb_exec(key, ["sh", "-c", 'test -f "$1" && cat -- "$1"', "sh", _check_path(path)],
                               timeout=300, raw=True)
    return data if code == 0 else None


async def remove(key: str, path: str) -> bool:
    if config.SANDBOX_BACKEND != "docker":
        full = _local_path(key, path)
        if not os.path.isfile(full):
            return False
        os.remove(full)
        return True
    code, _ = await sb_exec(key, ["sh", "-c", 'test -f "$1" && rm -f -- "$1"', "sh", _check_path(path)], timeout=30)
    return code == 0
