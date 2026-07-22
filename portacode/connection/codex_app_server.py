"""JSON-RPC bridge to the Codex CLI app-server over stdio."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

DEFAULT_CODEX_COMMAND = ["codex", "app-server", "--listen", "stdio://"]


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server cannot be started or is unhealthy."""


class CodexAppServer:
    """Thin asyncio JSON-RPC 2.0 client for `codex app-server --listen stdio://`.

    One instance per device agent is enough: the app-server can host multiple
    threads/cwds, so we keep it alive for the lifetime of the connection and
    lazily spawn it on the first command.
    """

    def __init__(
        self,
        command: Optional[List[str]] = None,
        client_info: Optional[Dict[str, Any]] = None,
        on_notification: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        _process_factory: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self.command = command or list(DEFAULT_CODEX_COMMAND)
        if client_info is None:
            try:
                from portacode import __version__ as _portacode_version
            except Exception:
                _portacode_version = "dev"
            client_info = {"name": "portacode", "version": _portacode_version}
        self.client_info = client_info
        self.on_notification = on_notification
        self._process_factory = _process_factory or asyncio.create_subprocess_exec

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._initialized = asyncio.Event()
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the app-server if it is not already running."""
        if self._closed:
            raise CodexAppServerError("CodexAppServer has been closed")
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._spawn()

    async def stop(self) -> None:
        """Stop the app-server and cancel background tasks."""
        async with self._lock:
            self._closed = True
            await self._kill_locked()

    async def _spawn(self) -> None:
        """Spawn the subprocess and perform the JSON-RPC initialize handshake."""
        exe = shutil.which(self.command[0]) or self.command[0]
        if not shutil.which(self.command[0]):
            raise CodexAppServerError(
                f"{self.command[0]} not found on PATH. Run `portacode prepare codex` on this device."
            )

        # systemd services do not load shell profile env; merge /etc/portacode/codex.env.
        from portacode.codex_prepare import build_codex_subprocess_env, ensure_codex_home
        from portacode.connection.handlers.runtime_user import (
            get_default_runtime_user,
            get_runtime_user_home,
            wrap_argv_for_user,
        )

        child_env = build_codex_subprocess_env()
        # Match terminals/file writes: run Codex as the configured runtime user
        # (e.g. bishoy), not as the root agent process.
        runtime_user = get_default_runtime_user()
        runtime_home = get_runtime_user_home()
        child_env["HOME"] = runtime_home
        child_env["USER"] = runtime_user
        child_env["LOGNAME"] = runtime_user
        try:
            child_env["CODEX_HOME"] = str(ensure_codex_home())
        except Exception:
            child_env.setdefault("CODEX_HOME", str(Path(runtime_home) / ".codex"))

        preserve = [
            "OPENAI_API_KEY",
            "CODEX_HOME",
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "PORTACODE_DEFAULT_RUNTIME_USER",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ]
        spawn_cmd = wrap_argv_for_user(
            self.command,
            runtime_user,
            preserve_env_names=preserve,
            login=False,
        )
        # Critical: the agent often runs with cwd=/root (mode 0700). Spawning a
        # non-root Codex via runuser/setpriv while cwd is inaccessible makes
        # Node/Codex hang or exit before answering initialize.
        spawn_cwd = runtime_home if Path(runtime_home).is_dir() else None
        LOGGER.info(
            "Spawning Codex app-server as %s (cwd=%s): %s",
            runtime_user,
            spawn_cwd or ".",
            " ".join(spawn_cmd),
        )
        self._recent_stderr: List[str] = []
        try:
            spawn_kwargs: Dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "env": child_env,
            }
            if spawn_cwd:
                spawn_kwargs["cwd"] = spawn_cwd
            self._proc = await self._process_factory(*spawn_cmd, **spawn_kwargs)
        except Exception as exc:
            raise CodexAppServerError(f"Failed to spawn codex app-server: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="codex-app-server-stderr")

        await self._initialize()

    async def recycle(self) -> None:
        """Stop a running app-server so the next ``start()`` respawns with fresh env."""
        async with self._lock:
            await self._kill_locked()
            self._closed = False
            self._initialized.clear()

    async def _kill_locked(self) -> None:
        """Tear down the subprocess and reader tasks."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CodexAppServerError("Codex app-server stopped"))
        self._pending.clear()

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None

        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None
        self._initialized.clear()

    # ------------------------------------------------------------------
    # JSON-RPC I/O
    # ------------------------------------------------------------------
    async def _initialize(self) -> None:
        """Send the initialize handshake and wait for its response.

        Uses `_request_on_running_proc` so we do not re-enter `start()` while
        `start()` already holds `_lock` (asyncio.Lock is not reentrant).
        """
        # Backfill / first boot can exceed a few seconds after CODEX_HOME moves
        # or session indexes are rebuilt.
        response = await self._request_on_running_proc(
            "initialize",
            {
                "clientInfo": self.client_info,
                "capabilities": {"streaming": True},
            },
            timeout=60.0,
        )
        LOGGER.info("Codex app-server initialized: %s", response.get("result", {}))
        # Required by current Codex app-server clients so post-init notifications
        # (e.g. thread/name/updated) are delivered. Must not call start() again —
        # we are already inside start()'s lock.
        await self._write_notification_on_running_proc("initialized", {})
        self._initialized.set()

    async def _request(
        self, method: str, params: Dict[str, Any], timeout: float = 60.0
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request and return the response object."""
        await self.start()
        return await self._request_on_running_proc(method, params, timeout=timeout)

    async def _request_on_running_proc(
        self, method: str, params: Dict[str, Any], timeout: float = 60.0
    ) -> Dict[str, Any]:
        """Write a JSON-RPC request to an already-spawned app-server process."""
        assert self._proc is not None and self._proc.stdin is not None

        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future

        try:
            self._proc.stdin.write((payload + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception as exc:
            self._pending.pop(request_id, None)
            raise CodexAppServerError(f"Failed to write to codex app-server: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            detail = f"codex app-server request timed out: {method}"
            recent = getattr(self, "_recent_stderr", None) or []
            if recent:
                detail = f"{detail} (stderr: {' | '.join(recent[-5:])})"
            if self._proc is not None and self._proc.returncode is not None:
                detail = f"{detail} (process exited code {self._proc.returncode})"
            raise CodexAppServerError(detail) from exc

    async def _write_notification_on_running_proc(
        self, method: str, params: Dict[str, Any]
    ) -> None:
        """Write a JSON-RPC notification to an already-spawned app-server."""
        assert self._proc is not None and self._proc.stdin is not None
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Fire a JSON-RPC notification (no response expected)."""
        await self.start()
        await self._write_notification_on_running_proc(method, params)

    async def _send_error_response(self, request_id: Any, code: int, message: str) -> None:
        """Reply to a server->client request with a JSON-RPC error."""
        assert self._proc is not None and self._proc.stdin is not None
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    # ------------------------------------------------------------------
    # Reading loops
    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        """Read newline-delimited JSON-RPC messages from app-server stdout."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    LOGGER.warning("Ignoring non-JSON line from codex app-server: %r (%s)", line, exc)
                    continue
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("codex app-server read loop failed")
        finally:
            # Process exited; schedule a restart if not closed.
            if not self._closed:
                asyncio.create_task(self._on_reader_exit())

    async def _stderr_loop(self) -> None:
        """Forward app-server stderr lines to the logger."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    # Keep a small ring for timeout errors; use WARNING so
                    # initialize failures are visible at default journal levels.
                    buf = getattr(self, "_recent_stderr", None)
                    if buf is None:
                        self._recent_stderr = []
                        buf = self._recent_stderr
                    buf.append(text)
                    if len(buf) > 40:
                        del buf[:-40]
                    LOGGER.warning("codex app-server stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("codex app-server stderr loop failed")

    async def _dispatch_message(self, message: Dict[str, Any]) -> None:
        """Route a single JSON-RPC message to pending response, notification, or request handler."""
        request_id = message.get("id")
        has_result = "result" in message
        has_error = "error" in message
        method = message.get("method")
        params = message.get("params", {})

        if has_result or has_error:
            if request_id is not None and request_id in self._pending:
                future = self._pending.pop(request_id)
                if not future.done():
                    future.set_result(message)
            return

        if request_id is not None and method is not None:
            # The server sent us a request. We currently auto-decline/unsupported everything
            # because the portacode config sets approval_policy="never" and danger-full-access.
            LOGGER.debug("Declining codex app-server request: %s", method)
            await self._send_error_response(request_id, -32601, f"Method not supported by portacode: {method}")
            return

        if method is not None:
            # Notification from app-server.
            LOGGER.debug("codex app-server notification: %s", method)
            if self.on_notification is not None:
                try:
                    await self.on_notification(method, params)
                except Exception:
                    LOGGER.exception("codex notification handler failed for %s", method)
            return

        LOGGER.debug("Ignoring unexpected codex app-server message: %s", message)

    async def _on_reader_exit(self) -> None:
        """Restart the app-server once after an unexpected exit."""
        async with self._lock:
            if self._closed or self._proc is None:
                return
            # If initialize never completed, the caller still holding start()
            # will surface the error — avoid a restart storm on bad cwd/env.
            if not self._initialized.is_set():
                LOGGER.warning(
                    "Codex app-server process exited during startup (code %s)",
                    self._proc.returncode,
                )
                return
            LOGGER.warning("Codex app-server process exited (code %s); restarting", self._proc.returncode)
            await self._kill_locked()
        try:
            await self.start()
        except Exception:
            LOGGER.exception("Failed to restart codex app-server")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def call(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0
    ) -> Dict[str, Any]:
        """Call a Codex app-server method and return the `result` field."""
        params = params or {}
        response = await self._request(method, params, timeout)
        if "error" in response:
            error = response["error"]
            raise CodexAppServerError(
                f"codex app-server error ({method}): {error.get('message', error)}"
            )
        return response.get("result", {})

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification."""
        await self._send_notification(method, params or {})

    async def healthy(self) -> bool:
        """Return True if the app-server is initialized and the process is alive."""
        if self._proc is None or self._proc.returncode is not None:
            return False
        return self._initialized.is_set()

    @staticmethod
    def get_binary_path() -> Optional[str]:
        return shutil.which("codex")

    @staticmethod
    def version() -> Optional[str]:
        import subprocess

        exe = shutil.which("codex")
        if not exe:
            return None
        try:
            result = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() or None
        except Exception:
            return None

    def get_running_thread_id(self, turn_id: Optional[str] = None) -> Optional[str]:
        """Hook for the manager to resolve turn_id -> thread_id if needed."""
        return None

    def __del__(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except Exception:
                pass
