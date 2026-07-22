"""Tests for the Codex app-server JSON-RPC bridge."""

from __future__ import annotations

import asyncio
import json

import pytest

from portacode.connection.codex_app_server import CodexAppServer, CodexAppServerError


class FakeWriteStream:
    def __init__(self, process: "FakeProcess | None" = None) -> None:
        self.written: list[bytes] = []
        self._process = process

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if self._process is not None:
            self._process._maybe_auto_respond(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeReadStream:
    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue
        self._closed = False

    async def readline(self) -> bytes:
        if self._closed:
            return b""
        return await self._queue.get()

    def close(self) -> None:
        self._closed = True


class FakeProcess:
    returncode: int | None = None

    def __init__(
        self,
        responses: list[dict] | None = None,
        *,
        auto_initialize: bool = False,
    ) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        for response in responses or []:
            self.queue.put_nowait(
                (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            )
        self.stdin = FakeWriteStream(self if auto_initialize else None)
        self.stdout = FakeReadStream(self.queue)
        self.stderr = FakeReadStream(asyncio.Queue())
        self._auto_initialize = auto_initialize

    def _maybe_auto_respond(self, data: bytes) -> None:
        if not self._auto_initialize:
            return
        try:
            message = json.loads(data.decode("utf-8").strip())
        except Exception:
            return
        if message.get("method") != "initialize":
            return
        request_id = message.get("id")
        if request_id is None:
            return
        self.queue.put_nowait((json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "v2"}},
            separators=(",", ":"),
        ) + "\n").encode("utf-8"))

    async def wait(self) -> int:
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def _factory_for(*processes: FakeProcess):
    """Return a factory callable that yields the supplied fake processes in order."""
    proc_iter = iter(processes)

    async def _factory(*_args, **_kwargs):
        try:
            proc = next(proc_iter)
        except StopIteration as exc:
            raise RuntimeError("No more fake processes") from exc
        proc.spawn_kwargs = _kwargs
        return proc

    return _factory


@pytest.mark.asyncio
async def test_initialize_handshake():
    proc = FakeProcess([{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}}])
    bridge = CodexAppServer(_process_factory=_factory_for(proc))
    await bridge.start()

    assert await bridge.healthy() is True
    assert any(b'"method":"initialize"' in chunk for chunk in proc.stdin.written)
    await bridge.stop()


@pytest.mark.asyncio
async def test_spawn_injects_openai_api_key(tmp_path, monkeypatch):
    env_file = tmp_path / "codex.env"
    env_file.write_text("OPENAI_API_KEY=portacode-local\n", encoding="utf-8")
    monkeypatch.setattr("portacode.codex_prepare.CODEX_ENV_PATH", env_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_runtime_user_home",
        lambda message=None: str(home),
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.should_switch_user",
        lambda user: True,
    )
    monkeypatch.setattr("portacode.codex_prepare.ensure_codex_home", lambda: home / ".codex")

    proc = FakeProcess([{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}}])
    bridge = CodexAppServer(_process_factory=_factory_for(proc))
    await bridge.start()

    env = getattr(proc, "spawn_kwargs", {}).get("env") or {}
    assert env.get("OPENAI_API_KEY") == "portacode-local"
    assert env.get("HOME") == str(home)
    assert env.get("USER") == "bishoy"
    assert env.get("CODEX_HOME") == str(home / ".codex")
    assert getattr(proc, "spawn_kwargs", {}).get("cwd") == str(home)
    # factory receives argv as *args before stdout/stdin kwargs
    await bridge.stop()


@pytest.mark.asyncio
async def test_spawn_runs_as_runtime_user(tmp_path, monkeypatch):
    env_file = tmp_path / "codex.env"
    env_file.write_text("OPENAI_API_KEY=portacode-local\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("portacode.codex_prepare.CODEX_ENV_PATH", env_file)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_runtime_user_home",
        lambda message=None: str(home),
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.should_switch_user",
        lambda user: True,
    )
    def _which(name: str):
        if name == "runuser":
            return "/usr/sbin/runuser"
        if name in {"codex", "codex.cmd"}:
            return "/usr/bin/codex"
        return None

    monkeypatch.setattr("portacode.connection.handlers.runtime_user.shutil.which", _which)
    monkeypatch.setattr("portacode.connection.codex_app_server.shutil.which", _which)
    monkeypatch.setattr("portacode.codex_prepare.ensure_codex_home", lambda: home / ".codex")

    seen: list[tuple] = []
    seen_kwargs: list[dict] = []

    async def factory(*args, **kwargs):
        seen.append(args)
        seen_kwargs.append(kwargs)
        proc = FakeProcess([{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}}])
        proc.spawn_kwargs = kwargs
        return proc

    bridge = CodexAppServer(_process_factory=factory)
    await bridge.start()
    assert seen
    assert seen[0][:5] == ("/usr/sbin/runuser", "-u", "bishoy", "--preserve-environment", "--")
    assert "codex" in seen[0]
    assert seen_kwargs[0].get("cwd") == str(home)
    await bridge.stop()


def test_build_codex_subprocess_env_reads_managed_file(tmp_path):
    from portacode.codex_prepare import build_codex_subprocess_env

    env_file = tmp_path / "codex.env"
    env_file.write_text("OPENAI_API_KEY=portacode-local\n", encoding="utf-8")
    env = build_codex_subprocess_env(base={"PATH": "/usr/bin"}, path=env_file)
    assert env["OPENAI_API_KEY"] == "portacode-local"
    assert env["PATH"] == "/usr/bin"


def test_build_codex_subprocess_env_defaults_sentinel_without_file(tmp_path):
    from portacode.codex_prepare import LOCAL_SENTINEL, build_codex_subprocess_env

    missing = tmp_path / "missing.env"
    env = build_codex_subprocess_env(base={}, path=missing)
    assert env["OPENAI_API_KEY"] == LOCAL_SENTINEL


@pytest.mark.asyncio
async def test_request_response_correlation():
    # Only preload the initialize response. Queuing the call response early can
    # race the reader ahead of the pending-future registration.
    proc = FakeProcess([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}},
    ])
    bridge = CodexAppServer(_process_factory=_factory_for(proc))
    await bridge.start()

    async def _respond_to_thread_start() -> None:
        # Wait until the request is written, then enqueue the matching response.
        for _ in range(50):
            if any(b'"method":"thread/start"' in chunk for chunk in proc.stdin.written):
                break
            await asyncio.sleep(0.01)
        proc.queue.put_nowait((json.dumps(
            {"jsonrpc": "2.0", "id": 2, "result": {"id": "th-abc", "name": "test"}},
            separators=(",", ":"),
        ) + "\n").encode("utf-8"))

    respond_task = asyncio.create_task(_respond_to_thread_start())
    result = await bridge.call("thread/start", {"cwd": "/tmp/proj"})
    await respond_task

    assert result["id"] == "th-abc"
    assert any(b'"method":"thread/start"' in chunk and b'/tmp/proj' in chunk for chunk in proc.stdin.written)
    await bridge.stop()


@pytest.mark.asyncio
async def test_notification_dispatch():
    proc = FakeProcess([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}},
    ])
    notifications: list[tuple[str, dict]] = []
    bridge = CodexAppServer(
        on_notification=lambda m, p: notifications.append((m, p)),
        _process_factory=_factory_for(proc),
    )
    await bridge.start()

    proc.queue.put_nowait((json.dumps({"jsonrpc": "2.0", "method": "turn/started", "params": {"turnId": "t1"}}) + "\n").encode("utf-8"))
    await asyncio.sleep(0.05)

    assert len(notifications) == 1
    assert notifications[0][0] == "turn/started"
    assert notifications[0][1]["turnId"] == "t1"
    await bridge.stop()


@pytest.mark.asyncio
async def test_server_request_auto_decline():
    proc = FakeProcess([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}},
    ])
    bridge = CodexAppServer(_process_factory=_factory_for(proc))
    await bridge.start()

    proc.queue.put_nowait((json.dumps({
        "jsonrpc": "2.0",
        "id": 42,
        "method": "commandExecutionRequestApproval",
        "params": {"command": "rm -rf /"},
    }) + "\n").encode("utf-8"))
    await asyncio.sleep(0.05)

    assert any(b'"id":42' in chunk and b'"error"' in chunk for chunk in proc.stdin.written)
    await bridge.stop()


@pytest.mark.asyncio
async def test_missing_binary_raises():
    bridge = CodexAppServer(command=["this-binary-does-not-exist"])
    with pytest.raises(CodexAppServerError, match="not found"):
        await bridge.start()


@pytest.mark.asyncio
async def test_restart_after_crash():
    first = FakeProcess([{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "v2"}}])
    # Auto-echo initialize with whatever request id the restarted bridge uses.
    second = FakeProcess(auto_initialize=True)

    bridge = CodexAppServer(_process_factory=_factory_for(first, second))
    await bridge.start()
    assert await bridge.healthy()

    # Simulate process exit.
    first.stdout.close()
    first.returncode = 1
    first.queue.put_nowait(b"")

    await asyncio.sleep(0.15)

    assert second is not first
    assert await bridge.healthy()
    await bridge.stop()
