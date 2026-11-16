"""sandboxd: the app reaches sandboxes only through sandboxd's Unix socket, and sandboxd builds every
docker command itself (fixed flags, gVisor runtime, validated names). Docker is faked."""
import asyncio
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-sandboxd-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

from app import config, sandbox  # noqa: E402


class FakeDocker:
    """Stands in for sandbox._proc: records argv, answers `docker inspect` from `containers`."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.stdin: list[bytes | None] = []
        self.containers: dict[str, str] = {}  # name -> runtime
        self.cancelled = threading.Event()

    async def __call__(self, argv, stdin, timeout, cwd=None, env=None):
        self.calls.append(list(argv))
        self.stdin.append(stdin)
        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1]
            if name not in self.containers:
                return 1, b"Error: No such object", False
            if "{{.IPAddress}}" in argv[3]:
                return 0, b"172.20.0.5 ", False
            return 0, f"true {self.containers[name]}\n".encode(), False
        if argv[:2] == ["docker", "run"]:
            name = argv[argv.index("--name") + 1]
            self.containers[name] = argv[argv.index("--runtime") + 1] if "--runtime" in argv else "runc"
            return 0, b"cid\n", False
        if argv[:3] == ["docker", "rm", "-f"]:
            self.containers.pop(argv[3], None)
            return 0, b"", False
        if argv[:2] == ["docker", "exec"]:
            if argv[-1] == "sleep-forever":
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            return 0, b"hello\n" + (stdin or b""), False
        return 0, b"", False


@pytest.fixture
def sandboxd(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "_proc", fake)
    monkeypatch.setattr(config, "SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(config, "SANDBOX_RUNTIME", "runsc")
    monkeypatch.setattr(config, "SANDBOX_RESOLV_CONF", "/etc/teambots/sandbox-resolv.conf")
    sandbox._started.clear()
    sock = os.path.join(tempfile.mkdtemp(prefix="tb-sd-"), "sandboxd.sock")
    from app.sandboxd import app
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    monkeypatch.setattr(config, "SANDBOXD_SOCKET", sock)
    yield fake, sock
    server.should_exit = True
    thread.join(5)
    sandbox._started.clear()


def test_exec_goes_through_sandboxd_with_gvisor(sandboxd):
    fake, _ = sandboxd
    code, out = asyncio.run(sandbox.sb_exec("p1", ["cat"], stdin=b"in\x00put", env={"DISPLAY": ":1"}))
    assert code == 0 and out == "hello\nin\x00put"
    run = next(c for c in fake.calls if c[:2] == ["docker", "run"])
    assert run[run.index("--runtime") + 1] == "runsc"
    assert "--cap-drop" in run and "ALL" in run and "-v" in run
    assert run[run.index("-v") + 1] == "tb-p1:/workspace"
    assert "/etc/teambots/sandbox-resolv.conf:/etc/resolv.conf:ro" in run
    exec_ = next(c for c in fake.calls if c[:2] == ["docker", "exec"])
    assert exec_ == ["docker", "exec", "-i", "-w", "/workspace", "-e", "DISPLAY=:1", "tb-p1", "cat"]


def test_raw_bytes_and_file_helpers(sandboxd):
    fake, _ = sandboxd
    asyncio.run(sandbox.put_bytes("p2", "a/b.bin", b"\xff\x00\x01"))
    assert fake.stdin[-1] == b"\xff\x00\x01"
    data = asyncio.run(sandbox.get_bytes("p2", "a/b.bin"))
    assert data == b"hello\n"


def test_container_from_runc_is_recreated_under_runsc(sandboxd):
    fake, _ = sandboxd
    fake.containers["tb-p3"] = "runc"
    asyncio.run(sandbox.sb_exec("p3", ["true"]))
    assert ["docker", "rm", "-f", "tb-p3"] in fake.calls
    assert fake.containers["tb-p3"] == "runsc"


def test_container_already_on_runsc_is_kept(sandboxd):
    fake, _ = sandboxd
    fake.containers["tb-p4"] = "runsc"
    asyncio.run(sandbox.sb_exec("p4", ["true"]))
    assert not any(c[:2] in (["docker", "run"], ["docker", "rm"]) for c in fake.calls)


def test_addr_and_destroy(sandboxd):
    fake, _ = sandboxd
    assert asyncio.run(sandbox.sandbox_addr("p5")) == "172.20.0.5"
    import subprocess
    calls = []
    orig = subprocess.run
    subprocess.run = lambda argv, **kw: calls.append(argv)
    try:
        sandbox.destroy("p5")
    finally:
        subprocess.run = orig
    assert calls == [["docker", "rm", "-f", "tb-p5"], ["docker", "volume", "rm", "tb-p5"]]


def test_sandboxd_rejects_bad_requests(sandboxd):
    _, sock = sandboxd
    with httpx.Client(transport=httpx.HTTPTransport(uds=sock), base_url="http://s") as c:
        for body in (
            {"name": "../etc", "argv": ["true"]},
            {"name": "other-container", "argv": ["true"]},
            {"name": "tb-p1", "argv": []},
            {"name": "tb-p1", "argv": ["true"], "env": {"--privileged": "1"}},
        ):
            assert c.post("/exec", json=body).status_code == 400, body
        assert c.post("/destroy", json={"name": "teambots-app-1"}).status_code == 400
        for path in ("/docs", "/openapi.json"):
            assert c.get(path).status_code == 404


def test_interrupted_turn_stops_the_command(sandboxd):
    fake, _ = sandboxd

    async def go():
        task = asyncio.create_task(sandbox.sb_exec("p6", ["sleep-forever"], timeout=300))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert fake.cancelled.wait(5), "sandboxd kept the command running after the app hung up"
