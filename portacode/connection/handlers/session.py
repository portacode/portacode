"""Terminal session management."""

import asyncio
import logging
import os
import sys
import uuid
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from collections import deque

if TYPE_CHECKING:
    from ..multiplex import Channel

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")

# Minimal, safe defaults for interactive shells
_DEFAULT_ENV = {
    "TERM": "xterm-256color",
    "LANG": "C.UTF-8",
}


def _build_child_env() -> Dict[str, str]:
    """Return a copy of os.environ with sensible fallbacks added."""
    env = os.environ.copy()
    for k, v in _DEFAULT_ENV.items():
        env.setdefault(k, v)
    return env


class TerminalSession:
    """Represents a local shell subprocess bound to a mux channel."""

    def __init__(self, session_id: str, proc: Process, channel: "Channel"):
        self.id = session_id
        self.proc = proc
        self.channel = channel
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._buffer: deque[str] = deque(maxlen=400)

    async def start_io_forwarding(self) -> None:
        """Spawn background task that copies stdout/stderr to the channel."""
        assert self.proc.stdout is not None, "stdout pipe not set"

        async def _pump() -> None:
            try:
                while True:
                    data = await self.proc.stdout.read(1024)
                    if not data:
                        break
                    text = data.decode(errors="ignore")
                    logging.getLogger("portacode.terminal").debug(f"[MUX] Terminal {self.id} output: {text!r}")
                    self._buffer.append(text)
                    try:
                        await self.channel.send(text)
                    except Exception as exc:
                        logger.warning("Failed to forward terminal output: %s", exc)
                        await asyncio.sleep(0.5)
                        continue
            finally:
                if self.proc and self.proc.returncode is None:
                    pass  # Keep alive across reconnects

        # Cancel existing reader task if it exists
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            
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

    def snapshot_buffer(self) -> str:
        """Return concatenated last buffer contents suitable for UI."""
        return "".join(self._buffer)

    async def reattach_channel(self, new_channel: "Channel") -> None:
        """Reattach this session to a new channel after reconnection."""
        logger.info("Reattaching terminal %s to channel %s", self.id, new_channel.id)
        self.channel = new_channel
        # Restart I/O forwarding with new channel
        await self.start_io_forwarding()


class WindowsTerminalSession(TerminalSession):
    """Terminal session backed by a Windows ConPTY."""

    def __init__(self, session_id: str, pty, channel: "Channel"):
        # Create a proxy for the PTY process
        class _WinPTYProxy:
            def __init__(self, pty):
                self._pty = pty

            @property
            def pid(self):
                return self._pty.pid

            @property
            def returncode(self):
                return None if self._pty.isalive() else self._pty.exitstatus

            async def wait(self):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._pty.wait)

        super().__init__(session_id, _WinPTYProxy(pty), channel)
        self._pty = pty

    async def start_io_forwarding(self) -> None:
        """Spawn background task that copies stdout/stderr to the channel."""
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
                    logging.getLogger("portacode.terminal").debug(f"[MUX] Terminal {self.id} output: {text!r}")
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

        # Cancel existing reader task if it exists
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            
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


class SessionManager:
    """Manages terminal sessions."""

    def __init__(self, mux):
        self.mux = mux
        self._sessions: Dict[str, TerminalSession] = {}

    def _allocate_channel_id(self) -> str:
        """Allocate a new unique channel ID for a terminal session using UUID."""
        return uuid.uuid4().hex

    async def create_session(self, shell: Optional[str] = None, cwd: Optional[str] = None) -> Dict[str, Any]:
        """Create a new terminal session."""
        term_id = uuid.uuid4().hex
        channel_id = self._allocate_channel_id()
        channel = self.mux.get_channel(channel_id)

        # Choose shell
        if shell is None:
            shell = os.getenv("SHELL") if not _IS_WINDOWS else os.getenv("COMSPEC", "cmd.exe")

        logger.info("Launching terminal %s using shell=%s on channel=%s", term_id, shell, channel_id)

        if _IS_WINDOWS:
            try:
                from winpty import PtyProcess
            except ImportError as exc:
                logger.error("winpty (pywinpty) not found: %s", exc)
                raise RuntimeError("pywinpty not installed on client")

            pty_proc = PtyProcess.spawn(shell, cwd=cwd or None, env=_build_child_env())
            session = WindowsTerminalSession(term_id, pty_proc, channel)
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
                    env=_build_child_env(),
                )
                # Wrap master_fd into a StreamReader
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb", buffering=0))
                proc.stdout = reader
                # Use writer for stdin
                writer_transport, writer_protocol = await loop.connect_write_pipe(
                    lambda: asyncio.Protocol(), os.fdopen(master_fd, "wb", buffering=0)
                )
                proc.stdin = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)
            except Exception:
                logger.warning("Failed to allocate PTY, falling back to pipes")
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=_build_child_env(),
                )
            session = TerminalSession(term_id, proc, channel)

        self._sessions[term_id] = session
        await session.start_io_forwarding()

        return {
            "terminal_id": term_id,
            "channel": channel_id,
            "pid": session.proc.pid,
            "shell": shell,
            "cwd": cwd,
        }

    def get_session(self, terminal_id: str) -> Optional[TerminalSession]:
        """Get a terminal session by ID."""
        return self._sessions.get(terminal_id)

    def remove_session(self, terminal_id: str) -> Optional[TerminalSession]:
        """Remove and return a terminal session."""
        return self._sessions.pop(terminal_id, None)

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all terminal sessions."""
        return [
            {
                "terminal_id": s.id,
                "channel": s.channel.id,
                "pid": s.proc.pid,
                "returncode": s.proc.returncode,
                "buffer": s.snapshot_buffer(),
                "status": "active" if s.proc.returncode is None else "exited",
                "created_at": None,  # Could add timestamp if needed
                "shell": None,  # Could store shell info if needed
                "cwd": None,    # Could store cwd info if needed
            }
            for s in self._sessions.values()
        ]

    async def reattach_sessions(self, mux):
        """Reattach sessions to a new multiplexer after reconnection."""
        self.mux = mux
        logger.info("Reattaching %d terminal sessions to new multiplexer", len(self._sessions))
        
        # Clean up any sessions with dead processes first
        dead_sessions = []
        for term_id, sess in list(self._sessions.items()):
            if sess.proc.returncode is not None:
                logger.info("Cleaning up dead terminal session %s (exit code: %s)", term_id, sess.proc.returncode)
                dead_sessions.append(term_id)
        
        for term_id in dead_sessions:
            self._sessions.pop(term_id, None)
        
        # Reattach remaining live sessions
        for sess in self._sessions.values():
            try:
                # Get the existing channel ID (UUID string)
                channel_id = sess.channel.id
                new_channel = self.mux.get_channel(channel_id)
                await sess.reattach_channel(new_channel)
                logger.info("Successfully reattached terminal %s", sess.id)
            except Exception as exc:
                logger.error("Failed to reattach terminal %s: %s", sess.id, exc) 