from __future__ import annotations

"""Terminal session management for Portacode client.

This module allows the Portacode gateway to remotely create interactive shell
sessions on the client machine.  Each session gets its own *virtual* channel
on the existing WebSocket connection so multiple interactive terminals can run
concurrently without opening extra sockets.

The current implementation intentionally stays simple so it works on both
Unix and Windows without extra native dependencies:

* On Unix we *try* to allocate a real PTY so command‐line applications detect
  a TTY and are willing to emit colour sequences, progress bars, etc.
* On Windows we fall back to a standard subprocess with pipes.  While this
  means some programs may not detect a TTY, it still provides a usable shell
  until winpty/pywinpty support is added in the future.

Each message exchanged with the gateway uses JSON frames on the **control
channel 0**.  The schema is kept deliberately small for now – it will evolve
as we add more capabilities:

Gateway → Client commands (via channel 0)
-----------------------------------------

1. Start new terminal::

    {"cmd": "terminal_start", "shell": "/bin/bash"}  // *shell* optional

   • Response (same channel):
     {"event": "terminal_started", "terminal_id": "...", "channel": 123}

2. Send data to running terminal::

    {"cmd": "terminal_send", "terminal_id": "...", "data": "ls -l\n"}

3. Close running terminal::

    {"cmd": "terminal_stop", "terminal_id": "..."}

4. List terminals::

    {"cmd": "terminal_list"}
   • Response: {"event": "terminal_list", "sessions": [...]}  // see code

5. System information::

    {"cmd": "system_info"}
   • Response: {"event": "system_info", "info": {...}}

Client → Gateway events (channel 0 unless stated otherwise)
---------------------------------------------------------

• Terminal exited (channel 0)::

    {"event": "terminal_exit", "terminal_id": "...", "returncode": 0}

• Terminal output (dedicated *session* channel)::

    <raw utf-8 text frames>  // JSON encoding avoided for efficiency

NOTE: For output we currently send **plain text** frames (not JSON) so binary
streams like progress bars or colour escapes are delivered unchanged.  If the
payload must be JSON for some transports, base64-encoding could be used – but
that adds 33 % overhead so plain frames are preferred.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any, Dict, Optional
from collections import deque

import psutil  # type: ignore

from .multiplex import Multiplexer, Channel

logger = logging.getLogger(__name__)

__all__ = [
    "TerminalManager",
]


_IS_WINDOWS = sys.platform.startswith("win")


class TerminalSession:
    """Represents a local shell subprocess bound to a mux *channel*."""

    def __init__(self, session_id: str, proc: Process, channel: Channel):
        self.id = session_id
        self.proc = proc
        self.channel = channel
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._buffer: deque[str] = deque(maxlen=400)  # keep last ~400 lines of raw output

    async def start_io_forwarding(self) -> None:
        """Spawn background task that copies stdout/stderr to the channel."""
        assert self.proc.stdout is not None, "stdout pipe not set"

        async def _pump() -> None:
            try:
                while True:
                    data = await self.proc.stdout.read(1024)
                    if not data:
                        break
                    # Send *raw* text – no JSON envelope, keep escapes intact.
                    text = data.decode(errors="ignore")
                    logging.getLogger("portacode.terminal").debug(f"[MUX] Terminal {self.id} output: {text!r}")
                    self._buffer.append(text)
                    try:
                        await self.channel.send(text)
                    except Exception as exc:
                        # Likely connection dropped – keep process running and retry later
                        logger.warning("Failed to forward terminal output: %s", exc)
                        await asyncio.sleep(0.5)
                        continue
            finally:
                # Ensure process gets reaped if reader exits unexpectedly.
                if self.proc and self.proc.returncode is None:
                    # Only kill if explicitly stopped elsewhere; keep alive across reconnects
                    pass

        self._reader_task = asyncio.create_task(_pump())

    async def write(self, data: str) -> None:
        if self.proc.stdin is None:
            logger.warning("stdin pipe closed for terminal %s", self.id)
            return
        try:
            self.proc.stdin.write(data.encode())
            await self.proc.stdin.drain()
        except Exception as exc:
            logger.warning("Failed to write to terminal %s: %s", self.id, exc)

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.terminate()
        if self._reader_task:
            await self._reader_task
        await self.proc.wait()

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def snapshot_buffer(self) -> str:
        """Return concatenated last buffer contents suitable for UI."""
        return "".join(self._buffer)


class TerminalManager:
    """Manage multiple *TerminalSession*s controlled over a mux channel."""

    CONTROL_CHANNEL_ID = 0  # messages with JSON commands/events

    def __init__(self, mux: Multiplexer):
        self.mux = mux
        self._sessions: Dict[str, TerminalSession] = {}
        self._next_channel = 100  # channel ids >=100 reserved for terminals
        # Control channel & loop
        self._set_mux(mux)

    # ------------------------------------------------------------------
    # Mux attach/detach helpers (for reconnection resilience)
    # ------------------------------------------------------------------

    def attach_mux(self, mux: Multiplexer) -> None:
        """Attach a *new* Multiplexer after a reconnect, re-binding channels."""
        self._set_mux(mux)

        # Re-map existing terminal sessions to fresh channels
        session_payloads = []
        highest_cid = self._next_channel
        for sess in self._sessions.values():
            cid = sess.channel.id
            # Re-bind to same channel id on the new mux
            sess.channel = self.mux.get_channel(cid)
            session_payloads.append({
                "terminal_id": sess.id,
                "channel": cid,
            })
            highest_cid = max(highest_cid, cid + 1)

        # Ensure future allocations don't clash with existing ones
        self._next_channel = max(self._next_channel, highest_cid)

        if session_payloads:
            # Send consolidated list so dashboard can reconcile state
            asyncio.create_task(self._control_channel.send({
                "event": "terminal_list",
                "sessions": session_payloads,
            }))

    def _set_mux(self, mux: Multiplexer) -> None:
        self.mux = mux
        self._control_channel = self.mux.get_channel(self.CONTROL_CHANNEL_ID)
        # Start (or restart) control loop task
        if getattr(self, "_ctl_task", None):
            try:
                self._ctl_task.cancel()
            except Exception:
                pass
        self._ctl_task = asyncio.create_task(self._control_loop())

    # ---------------------------------------------------------------------
    # Control loop – receives commands from gateway
    # ---------------------------------------------------------------------

    async def _control_loop(self) -> None:
        while True:
            message = await self._control_channel.recv()
            # Older parts of the system may send *raw* str.  Ensure dict.
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except Exception:
                    logger.warning("Discarding non-JSON control frame: %s", message)
                    continue
            if not isinstance(message, dict):
                logger.warning("Invalid control frame type: %r", type(message))
                continue
            cmd = message.get("cmd")
            if not cmd:
                # Ignore frames that are *events* coming from the remote side
                if message.get("event"):
                    continue
                logger.warning("Missing 'cmd' in control frame: %s", message)
                continue
            try:
                if cmd == "terminal_start":
                    shell = message.get("shell")
                    cwd = message.get("cwd")
                    await self._cmd_terminal_start(shell=shell, cwd=cwd)
                elif cmd == "terminal_send":
                    await self._cmd_terminal_send(message)
                elif cmd == "terminal_stop":
                    await self._cmd_terminal_stop(message)
                elif cmd == "terminal_list":
                    await self._cmd_terminal_list()
                elif cmd == "system_info":
                    await self._cmd_system_info()
                else:
                    await self._send_error(f"Unknown cmd: {cmd}")
            except Exception as exc:
                logger.exception("Unhandled exception processing %s: %s", cmd, exc)
                await self._send_error(str(exc))

    # ------------------------------------------------------------------
    # Individual command handlers
    # ------------------------------------------------------------------

    async def _cmd_terminal_start(self, *, shell: Optional[str], cwd: Optional[str] = None) -> None:
        term_id = uuid.uuid4().hex
        channel_id = self._allocate_channel_id()
        channel = self.mux.get_channel(channel_id)
        # Choose shell
        if shell is None:
            shell = os.getenv("SHELL") if not _IS_WINDOWS else os.getenv("COMSPEC", "cmd.exe")
        logger.info("Launching terminal %s using shell=%s on channel=%d", term_id, shell, channel_id)
        if _IS_WINDOWS:
            # Windows: use ConPTY via pywinpty for full TTY semantics
            try:
                from winpty import PtyProcess  # type: ignore
            except ImportError as exc:
                logger.error("winpty (pywinpty) not found – please install pywinpty: %s", exc)
                await self._send_error("pywinpty not installed on client")
                return

            pty_proc = PtyProcess.spawn(shell, cwd=cwd or None)

            class _WinPTYProxy:
                """Expose .pid and .returncode for compatibility with Linux branch."""

                def __init__(self, pty):
                    self._pty = pty

                @property
                def pid(self):
                    return self._pty.pid

                @property
                def returncode(self):
                    # None while running, else exitstatus
                    return None if self._pty.isalive() else self._pty.exitstatus

                async def wait(self):
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._pty.wait)

            class WindowsTerminalSession(TerminalSession):
                """Terminal session backed by a Windows ConPTY."""

                def __init__(self, session_id: str, pty, channel: Channel):
                    super().__init__(session_id, _WinPTYProxy(pty), channel)
                    self._pty = pty

                async def start_io_forwarding(self) -> None:
                    loop = asyncio.get_running_loop()

                    async def _pump() -> None:
                        try:
                            while True:
                                data = await loop.run_in_executor(None, self._pty.read, 1024)
                                if not data:
                                    if not self._pty.isalive():
                                        break
                                    await asyncio.sleep(0.05)
                                    continue
                                if isinstance(data, bytes):
                                    text = data.decode(errors="ignore")
                                else:
                                    text = data
                                logging.getLogger("portacode.terminal").debug(
                                    f"[MUX] Terminal {self.id} output: {text!r}"
                                )
                                self._buffer.append(text)
                                try:
                                    await self.channel.send(text)
                                except Exception as exc:
                                    logger.warning("Failed to forward terminal output: %s", exc)
                                    await asyncio.sleep(0.5)
                                    continue
                        finally:
                            if self._pty and self._pty.isalive():
                                self._pty.kill()

                    self._reader_task = asyncio.create_task(_pump())

                async def write(self, data: str) -> None:
                    loop = asyncio.get_running_loop()
                    try:
                        await loop.run_in_executor(None, self._pty.write, data)
                    except Exception as exc:
                        logger.warning("Failed to write to terminal %s: %s", self.id, exc)

                async def stop(self) -> None:
                    if self._pty.isalive():
                        self._pty.kill()
                    if self._reader_task:
                        await self._reader_task

            session = WindowsTerminalSession(term_id, pty_proc, channel)
            self._sessions[term_id] = session
            await session.start_io_forwarding()
            await self._control_channel.send(
                {
                    "event": "terminal_started",
                    "terminal_id": term_id,
                    "channel": channel_id,
                }
            )

            asyncio.create_task(self._watch_process_exit(session))
            return  # windows branch done
        else:
            # Unix: try real PTY for proper TTY semantics
            try:
                import pty
                master_fd, slave_fd = pty.openpty()
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                    cwd=cwd,
                )
                # Wrap master_fd into a StreamReader
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb", buffering=0))
                proc.stdout = reader  # type: ignore[assignment]
                # Use writer for stdin
                writer_transport, writer_protocol = await loop.connect_write_pipe(lambda: asyncio.Protocol(), os.fdopen(master_fd, "wb", buffering=0))
                proc.stdin = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)  # type: ignore[assignment]
            except Exception:
                logger.warning("Failed to allocate PTY, falling back to pipes")
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                )
            session = TerminalSession(term_id, proc, channel)
            self._sessions[term_id] = session
            await session.start_io_forwarding()
            await self._control_channel.send(
                {
                    "event": "terminal_started",
                    "terminal_id": term_id,
                    "channel": channel_id,
                }
            )

            # Also create background watcher for process exit
            asyncio.create_task(self._watch_process_exit(session))

    async def _cmd_terminal_send(self, msg: Dict[str, Any]) -> None:
        term_id = msg.get("terminal_id")
        data = msg.get("data", "")
        session = self._sessions.get(term_id)
        if not session:
            await self._send_error(f"terminal_id {term_id} not found")
            return
        await session.write(data)

    async def _cmd_terminal_stop(self, msg: Dict[str, Any]) -> None:
        term_id = msg.get("terminal_id")
        session = self._sessions.pop(term_id, None)
        if not session:
            await self._send_error(f"terminal_id {term_id} not found")
            return
        await session.stop()
        await self._control_channel.send({"event": "terminal_stopped", "terminal_id": term_id})

    async def _cmd_terminal_list(self) -> None:
        sessions = [
            {
                "terminal_id": s.id,
                "channel": s.channel.id,
                "pid": s.proc.pid,
                "returncode": s.proc.returncode,
                "buffer": s.snapshot_buffer(),
            }
            for s in self._sessions.values()
        ]
        await self._control_channel.send({"event": "terminal_list", "sessions": sessions})

    async def _cmd_system_info(self) -> None:
        info = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory": psutil.virtual_memory()._asdict(),
            "disk": psutil.disk_usage(str(Path.home()))._asdict(),
        }
        await self._control_channel.send({"event": "system_info", "info": info})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_error(self, message: str) -> None:
        await self._control_channel.send({"event": "error", "message": message})

    def _allocate_channel_id(self) -> int:
        cid = self._next_channel
        self._next_channel += 1
        return cid

    async def _watch_process_exit(self, session: TerminalSession) -> None:
        await session.proc.wait()
        await self._control_channel.send({
            "event": "terminal_exit",
            "terminal_id": session.id,
            "returncode": session.proc.returncode,
        })
        # Cleanup session table
        self._sessions.pop(session.id, None) 