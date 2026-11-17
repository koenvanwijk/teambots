"""sandboxd: the only process with access to the Docker socket.

The app asks sandboxd, over a Unix socket on a volume shared by just these two containers, to run a
command in a sandbox, to look up a sandbox's address, to remove a sandbox, or to use its fixed
one-shot Git-push worker. Nothing else: no arbitrary `docker run`, no host mounts, no image or flag
choice. The container settings (image, limits, network, runtime such as gVisor) come from sandboxd's
own environment. So a compromised app can still reach the sandboxes it already controls, but no
longer the host.

Run: uvicorn app.sandboxd:app --uds /run/teambots/sandboxd.sock
"""
import asyncio
import base64
import re

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import sandbox

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_NAME = re.compile(r"tb-[a-z0-9]+(?:-[a-z0-9]+)*")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_TIMEOUT = 600


def _name(name: str) -> str:
    if not _NAME.fullmatch(name) or len(name) > 64:
        raise HTTPException(400, "bad sandbox name")
    return name


class ExecReq(BaseModel):
    name: str
    argv: list[str]
    env: dict[str, str] = {}
    stdin: str | None = None  # base64
    timeout: float = 60
    detach: bool = False


class NameReq(BaseModel):
    name: str


class GitPushReq(BaseModel):
    """The only credential-bearing sandboxd operation.

    `token` is base64 for transport over the private Unix socket.  It is
    decoded into the worker's stdin; it never becomes a Docker argument or
    environment variable.
    """
    name: str
    remote_url: str
    branch: str
    path: str = "."
    username: str = "x-access-token"
    token: str  # base64
    timeout: float = 180


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/exec")
async def exec_(req: ExecReq, request: Request):
    name = _name(req.name)
    if not req.argv or any("\x00" in a for a in req.argv):
        raise HTTPException(400, "bad argv")
    if any(not _ENV_KEY.fullmatch(k) or "\x00" in v for k, v in req.env.items()):
        raise HTTPException(400, "bad env")
    timeout = min(max(float(req.timeout), 1), MAX_TIMEOUT)
    stdin = base64.b64decode(req.stdin) if req.stdin is not None else None
    task = asyncio.create_task(sandbox.docker_exec_direct(name, req.argv, req.env, stdin, timeout, req.detach))
    # The app hangs up when a bot turn is interrupted: stop the command instead of letting it run on.
    while True:
        done, _ = await asyncio.wait({task}, timeout=1)
        if done:
            break
        if await request.is_disconnected():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise HTTPException(499, "client went away")
    try:
        code, out, timed_out = task.result()
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:300])
    return {"code": code, "output": base64.b64encode(out).decode(), "timed_out": timed_out}


@app.post("/git-push")
async def git_push(req: GitPushReq, request: Request):
    """Start sandbox's fixed one-shot Git worker, not an arbitrary container command."""
    name = _name(req.name)
    try:
        remote_url, _ = sandbox.git_push_remote(req.remote_url)
        branch = sandbox.git_push_branch(req.branch)
        path = sandbox.git_push_path(req.path)
        username = sandbox.git_push_username(req.username)
        token = base64.b64decode(req.token, validate=True).decode("utf-8")
        sandbox._git_push_token(token)
    except (UnicodeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    timeout = min(max(float(req.timeout), 1), MAX_TIMEOUT)
    task = asyncio.create_task(sandbox.docker_git_push_direct(
        name, remote_url, branch, path, username, token, timeout))
    # Match /exec: abandoning a tool call must not leave a credentialed worker
    # running in the background.
    while True:
        done, _ = await asyncio.wait({task}, timeout=1)
        if done:
            break
        if await request.is_disconnected():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise HTTPException(499, "client went away")
    try:
        code, out, timed_out = task.result()
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:300])
    return {"code": code, "output": base64.b64encode(out).decode(), "timed_out": timed_out}


@app.post("/addr")
async def addr(req: NameReq):
    try:
        return {"ip": await sandbox.docker_addr_direct(_name(req.name))}
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:300])


@app.post("/destroy")
def destroy(req: NameReq):
    sandbox.docker_destroy_direct(_name(req.name))
    return {"ok": True}
