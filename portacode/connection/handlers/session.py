"""Terminal session management."""

import asyncio
import json
import logging
import os
import struct
import sys
import time
import uuid
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, List, TYPE_CHECKING

from platformdirs import user_data_dir

from portacode.link_capture import prepare_link_capture_bin

import pyte

if TYPE_CHECKING:
    from ..multiplex import Channel

# Terminal data rate limiting configuration
TERMINAL_DATA_RATE_LIMIT_MS = 60  # Minimum time between terminal_data events (milliseconds)
TERMINAL_DATA_MAX_WAIT_MS = 1000   # Maximum time to wait before sending accumulated data (milliseconds)
TERMINAL_DATA_INITIAL_WAIT_MS = 10  # Time to wait for additional data even on first event (milliseconds)

# Terminal buffer configuration - using pyte for proper screen state management
TERMINAL_COLUMNS = 80  # Default terminal width
TERMINAL_ROWS = 24     # Default terminal height (visible area)
TERMINAL_SCROLLBACK_LIMIT = 1000  # Maximum number of scrollback lines to preserve

# Link event folder for capturing helper notifications
_LINK_EVENT_ROOT = Path(user_data_dir("portacode", "portacode")) / "link_events"
_LINK_EVENT_POLL_INTERVAL = 0.5  # seconds
LINK_EVENT_THROTTLE_SECONDS = 5.0
LINK_CAPTURE_ORIGINAL_BROWSER_ENV = "PORTACODE_LINK_CAPTURE_ORIGINAL_BROWSER"

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")


def _configure_pty_window_size(fd: int, rows: int, cols: int) -> None:
    """Set the PTY window size so subprocesses see a real terminal."""
    if _IS_WINDOWS:
        return
    try:
        import fcntl
        import termios
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except ImportError:
        logger.debug("termios/fcntl unavailable; skipping PTY window sizing")
    except OSError as exc:
        logger.warning("Failed to set PTY window size (%sx%s): %s", cols, rows, exc)



# Minimal, safe defaults for interactive shells
_DEFAULT_ENV = {
    "TERM": "xterm-256color",
    "LANG": "C.UTF-8",
    "SHELL": "/bin/bash",
}


def _build_child_env() -> Dict[str, str]:
    """Return a copy of os.environ with sensible fallbacks added."""
    env = os.environ.copy()
    for k, v in _DEFAULT_ENV.items():
        env.setdefault(k, v)
    env.setdefault("COLUMNS", str(TERMINAL_COLUMNS))
    env.setdefault("LINES", str(TERMINAL_ROWS))
    return env


_LINK_EVENT_DISPATCHER: Optional["LinkEventDispatcher"] = None

class LinkEventDispatcher:
    """Watch a shared folder for link capture files."""

    def __init__(self, directory: Optional[Path]):
        self.directory = directory
        self._task: Optional[asyncio.Task[None]] = None

        # Callbacks that are notified whenever a new event file is processed
        self._callbacks: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []

    def start(self) -> None:
        if not self.directory:
            return
        if self._task and not self._task.done():
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("link_watcher: Failed to create directory %s: %s", self.directory, exc)
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                if self.directory.exists():
                    for entry in sorted(self.directory.iterdir()):
                        if not entry.is_file():
                            continue
                        await self._process_entry(entry)
                await asyncio.sleep(_LINK_EVENT_POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("link_watcher: error scanning %s: %s", self.directory, exc)
                await asyncio.sleep(_LINK_EVENT_POLL_INTERVAL)

    async def _process_entry(self, entry: Path) -> None:
        try:
            raw = entry.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning("link_watcher: failed to read %s: %s", entry, exc)
        else:
            terminal_id = payload.get("terminal_id")
            link = payload.get("url")
            if link:
                logger.info("link_watcher: terminal %s captured link %s", terminal_id, link)
            else:
                logger.info("link_watcher: terminal %s observed link capture without url (%s)", terminal_id, payload)
            await self._notify_callbacks(payload)
        finally:
            try:
                entry.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("link_watcher: failed to remove %s: %s", entry, exc)

    def register_callback(self, callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Register a coroutine callback for processed link events."""
        if callback in self._callbacks:
            return
        self._callbacks.append(callback)

    async def _notify_callbacks(self, payload: Dict[str, Any]) -> None:
        if not self._callbacks:
            return
        for callback in list(self._callbacks):
            try:
                result = callback(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("link_watcher: callback raised an exception: %s", exc)


def _get_link_event_dispatcher() -> "LinkEventDispatcher":
    global _LINK_EVENT_DISPATCHER
    if _LINK_EVENT_DISPATCHER is None:
        _LINK_EVENT_DISPATCHER = LinkEventDispatcher(_LINK_EVENT_ROOT)
    return _LINK_EVENT_DISPATCHER


class TerminalSession:
    """Represents a local shell subprocess bound to a mux channel."""

    def __init__(self, session_id: str, proc: Process, channel: "Channel", project_id: Optional[str] = None, terminal_manager: Optional["TerminalManager"] = None):
        self.id = session_id
        self.proc = proc
        self.channel = channel
        self.project_id = project_id
        self.terminal_manager = terminal_manager
        self._reader_task: Optional[asyncio.Task[None]] = None

        # Use pyte for proper terminal screen state management
        self._screen = pyte.HistoryScreen(TERMINAL_COLUMNS, TERMINAL_ROWS, history=TERMINAL_SCROLLBACK_LIMIT)
        self._stream = pyte.Stream(self._screen)  # Use Stream (not ByteStream) since data is already decoded to strings

        # Rate limiting for terminal_data events
        self._last_send_time: float = 0
        self._pending_data: str = ""
        self._debounce_task: Optional[asyncio.Task[None]] = None

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
                    
                    # Use rate-limited sending instead of immediate sending
                    await self._handle_terminal_data(text)
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
            if hasattr(self.proc.stdin, 'write') and hasattr(self.proc.stdin, 'drain'):
                # StreamWriter (pipe fallback)
                self.proc.stdin.write(data.encode())
                await self.proc.stdin.drain()
            else:
                # File object (PTY)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.proc.stdin.write, data.encode())
                await loop.run_in_executor(None, self.proc.stdin.flush)
        except Exception as exc:
            logger.warning("Failed to write to terminal %s: %s", self.id, exc)

    async def stop(self) -> None:
        """Stop the terminal session with comprehensive logging."""
        logger.info("session.stop: Starting stop process for session %s (PID: %s)", 
                   self.id, getattr(self.proc, 'pid', 'unknown'))
        
        try:
            # Check if process is still running
            if self.proc.returncode is None:
                logger.info("session.stop: Terminating process for session %s", self.id)
                self.proc.terminate()
            else:
                logger.info("session.stop: Process for session %s already exited (returncode: %s)", 
                           self.id, self.proc.returncode)
            
            # Wait for reader task to complete
            if self._reader_task and not self._reader_task.done():
                logger.info("session.stop: Waiting for reader task to complete for session %s", self.id)
                try:
                    await asyncio.wait_for(self._reader_task, timeout=5.0)
                    logger.info("session.stop: Reader task completed for session %s", self.id)
                except asyncio.TimeoutError:
                    logger.warning("session.stop: Reader task timeout for session %s, cancelling", self.id)
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except asyncio.CancelledError:
                        pass
            
            # Cancel and flush any pending terminal data
            if self._debounce_task and not self._debounce_task.done():
                logger.info("session.stop: Cancelling debounce task for session %s", self.id)
                self._debounce_task.cancel()
                try:
                    await self._debounce_task
                except asyncio.CancelledError:
                    pass
            
            # Send any remaining pending data
            if self._pending_data:
                logger.info("session.stop: Flushing pending terminal data for session %s", self.id)
                await self._flush_pending_data()
            
            # Wait for process to exit
            if self.proc.returncode is None:
                logger.info("session.stop: Waiting for process to exit for session %s", self.id)
                await self.proc.wait()
                logger.info("session.stop: Process exited for session %s (returncode: %s)", 
                           self.id, self.proc.returncode)
            else:
                logger.info("session.stop: Process already exited for session %s (returncode: %s)", 
                           self.id, self.proc.returncode)
                
        except Exception as exc:
            logger.exception("session.stop: Error stopping session %s: %s", self.id, exc)
            raise

    async def _send_terminal_data_now(self, data: str) -> None:
        """Send terminal data immediately and update last send time."""
        self._last_send_time = time.time()
        data_size = len(data.encode('utf-8'))

        logger.info("session: Attempting to send terminal_data for terminal %s (data_size=%d bytes)",
                   self.id, data_size)

        # Feed data to pyte screen for proper terminal state management
        self._add_to_buffer(data)

        try:
            # Send terminal data via control channel with client session targeting
            if self.terminal_manager:
                await self.terminal_manager._send_session_aware({
                    "event": "terminal_data",
                    "channel": self.id,
                    "data": data,
                    "project_id": self.project_id
                }, project_id=self.project_id)
                logger.info("session: Successfully queued terminal_data for terminal %s via terminal_manager", self.id)
            else:
                # Fallback to raw channel for backward compatibility
                await self.channel.send(data)
                logger.info("session: Successfully sent terminal_data for terminal %s via raw channel", self.id)
        except Exception as exc:
            logger.warning("session: Failed to forward terminal output for terminal %s: %s", self.id, exc)

    async def _flush_pending_data(self) -> None:
        """Send accumulated pending data and reset pending buffer."""
        if self._pending_data:
            pending_size = len(self._pending_data.encode('utf-8'))
            logger.info("session: Flushing pending terminal_data for terminal %s (pending_size=%d bytes)", 
                       self.id, pending_size)
            data_to_send = self._pending_data
            self._pending_data = ""
            await self._send_terminal_data_now(data_to_send)
        else:
            logger.debug("session: No pending data to flush for terminal %s", self.id)
        
        # Clear the debounce task
        self._debounce_task = None

    async def _handle_terminal_data(self, data: str) -> None:
        """Handle new terminal data with rate limiting and debouncing."""
        current_time = time.time()
        time_since_last_send = (current_time - self._last_send_time) * 1000  # Convert to milliseconds
        data_size = len(data.encode('utf-8'))

        logger.info("session: Received terminal_data for terminal %s (data_size=%d bytes, time_since_last_send=%.1fms)",
                   self.id, data_size, time_since_last_send)

        # Add new data to pending buffer (no trimming needed - pyte handles screen state)
        self._pending_data += data

        # Cancel existing debounce task if any
        if self._debounce_task and not self._debounce_task.done():
            logger.debug("session: Cancelling existing debounce task for terminal %s", self.id)
            self._debounce_task.cancel()

        # Always set up a debounce timer to catch rapid consecutive outputs
        async def _debounce_timer():
            try:
                if time_since_last_send >= TERMINAL_DATA_RATE_LIMIT_MS:
                    # Enough time has passed since last send, wait initial delay for more data
                    wait_time = TERMINAL_DATA_INITIAL_WAIT_MS / 1000
                    logger.info("session: Rate limit satisfied for terminal %s, waiting %.1fms for more data",
                               self.id, wait_time * 1000)
                else:
                    # Too soon since last send, wait for either the rate limit period or max wait time
                    wait_time = min(
                        (TERMINAL_DATA_RATE_LIMIT_MS - time_since_last_send) / 1000,
                        TERMINAL_DATA_MAX_WAIT_MS / 1000
                    )
                    logger.info("session: Rate limit active for terminal %s, waiting %.1fms before send (time_since_last=%.1fms, rate_limit=%dms)",
                               self.id, wait_time * 1000, time_since_last_send, TERMINAL_DATA_RATE_LIMIT_MS)

                await asyncio.sleep(wait_time)
                logger.info("session: Debounce timer expired for terminal %s, flushing pending data", self.id)
                await self._flush_pending_data()
            except asyncio.CancelledError:
                logger.debug("session: Debounce timer cancelled for terminal %s (new data arrived)", self.id)
                # Timer was cancelled, another data event came in
                pass

        self._debounce_task = asyncio.create_task(_debounce_timer())
        logger.info("session: Started debounce timer for terminal %s", self.id)

    def _add_to_buffer(self, data: str) -> None:
        """Feed data to pyte virtual terminal screen."""
        # Feed the data to pyte - it handles all ANSI parsing and screen state management
        self._stream.feed(data)

    def snapshot_buffer(self) -> str:
        """Return the visible terminal content as ANSI sequences suitable for XTerm.js."""
        # Render screen content to ANSI
        result = self._render_screen_to_ansi()

        # Add cursor positioning at the end so XTerm.js knows where the cursor should be
        # This is critical - without it, new data gets written at the wrong position causing duplication
        cursor_y = self._screen.cursor.y + 1  # Convert 0-indexed to 1-indexed
        cursor_x = self._screen.cursor.x + 1  # Convert 0-indexed to 1-indexed

        # Move cursor to the correct position
        result += f'\x1b[{cursor_y};{cursor_x}H'

        return result

    def _render_screen_to_ansi(self) -> str:
        """Convert pyte screen state to ANSI escape sequences.

        This renders both scrollback history and visible screen with full formatting
        (colors, bold, italics, underline) preserved as ANSI sequences.
        """
        lines = []

        # Get scrollback history if available (HistoryScreen provides this)
        if hasattr(self._screen, 'history'):
            # Process scrollback lines (lines that have scrolled off the top)
            history_top = self._screen.history.top
            for line_data in history_top:
                # line_data is a dict mapping column positions to Char objects
                line = self._render_line_to_ansi(line_data, self._screen.columns)
                lines.append(line)

        # Process visible screen lines
        for y in range(self._screen.lines):
            line_data = self._screen.buffer[y]
            line = self._render_line_to_ansi(line_data, self._screen.columns)
            lines.append(line)

        # Join all lines with CRLF for proper terminal display
        return '\r\n'.join(lines)

    def _render_line_to_ansi(self, line_data: Dict[int, 'pyte.screens.Char'], columns: int) -> str:
        """Convert a single line from pyte format to ANSI escape sequences.

        Args:
            line_data: Dict mapping column index to Char objects
            columns: Number of columns in the terminal

        Returns:
            String with ANSI escape codes for formatting
        """
        result = []
        last_char = None
        did_reset = False  # Track if we just emitted a reset code

        for x in range(columns):
            char = line_data.get(x)
            if char is None:
                # Empty cell - reset formatting if we had any
                if last_char is not None and self._char_has_formatting(last_char):
                    result.append('\x1b[0m')
                    did_reset = True
                result.append(' ')
                last_char = None
                continue

            # Check if formatting changed from previous character
            format_changed = last_char is None or self._char_format_changed(last_char, char) or did_reset

            if format_changed:
                # If previous char had formatting and current is different, reset first
                if last_char is not None and self._char_has_formatting(last_char) and not did_reset:
                    result.append('\x1b[0m')

                # Apply new formatting (always apply after reset)
                ansi_codes = self._get_ansi_codes_for_char(char)
                if ansi_codes:
                    result.append(f'\x1b[{ansi_codes}m')
                    did_reset = False
                else:
                    did_reset = True  # No formatting to apply after reset

            # Add the character data
            result.append(char.data)
            last_char = char

        # Reset formatting at end of line if we had any
        if last_char is not None and self._char_has_formatting(last_char):
            result.append('\x1b[0m')

        # Strip trailing whitespace from the line
        line_text = ''.join(result).rstrip()
        return line_text

    def _char_has_formatting(self, char: 'pyte.screens.Char') -> bool:
        """Check if a character has any formatting applied."""
        return (char.bold or
                (hasattr(char, 'dim') and char.dim) or
                char.italics or
                char.underscore or
                (hasattr(char, 'blink') and char.blink) or
                char.reverse or
                (hasattr(char, 'hidden') and char.hidden) or
                char.strikethrough or
                char.fg != 'default' or
                char.bg != 'default')

    def _char_format_changed(self, char1: 'pyte.screens.Char', char2: 'pyte.screens.Char') -> bool:
        """Check if formatting changed between two characters."""
        return (char1.bold != char2.bold or
                (hasattr(char1, 'dim') and hasattr(char2, 'dim') and char1.dim != char2.dim) or
                char1.italics != char2.italics or
                char1.underscore != char2.underscore or
                (hasattr(char1, 'blink') and hasattr(char2, 'blink') and char1.blink != char2.blink) or
                char1.reverse != char2.reverse or
                (hasattr(char1, 'hidden') and hasattr(char2, 'hidden') and char1.hidden != char2.hidden) or
                char1.strikethrough != char2.strikethrough or
                char1.fg != char2.fg or
                char1.bg != char2.bg)

    def _get_ansi_codes_for_char(self, char: 'pyte.screens.Char') -> str:
        """Convert pyte Char formatting to ANSI escape codes.

        Returns:
            String of semicolon-separated ANSI codes (e.g., "1;32;44")
        """
        codes = []

        # Text attributes - comprehensive list matching ANSI SGR codes
        if char.bold:
            codes.append('1')
        if hasattr(char, 'dim') and char.dim:
            codes.append('2')
        if char.italics:
            codes.append('3')
        if char.underscore:
            codes.append('4')
        if hasattr(char, 'blink') and char.blink:
            codes.append('5')
        if char.reverse:
            codes.append('7')
        if hasattr(char, 'hidden') and char.hidden:
            codes.append('8')
        if char.strikethrough:
            codes.append('9')

        # Foreground color
        if char.fg != 'default':
            fg_code = self._color_to_ansi(char.fg, is_background=False)
            if fg_code:
                codes.append(fg_code)

        # Background color
        if char.bg != 'default':
            bg_code = self._color_to_ansi(char.bg, is_background=True)
            if bg_code:
                codes.append(bg_code)

        return ';'.join(codes)

    def _color_to_ansi(self, color, is_background: bool = False) -> Optional[str]:
        """Convert pyte color to ANSI color code.

        Args:
            color: Color value (can be string name, int for 256-color, hex string, or tuple for RGB)
            is_background: True for background color, False for foreground

        Returns:
            ANSI color code string or None
        """
        # Handle default/None
        if color == 'default' or color is None:
            return None

        # Standard base for 8 basic colors
        base = 40 if is_background else 30

        if isinstance(color, str):
            # pyte stores colors as lowercase strings
            color_lower = color.lower()

            # Check for hex color format (pyte stores RGB as hex strings like '4782c8')
            # Hex strings are 6 characters (RRGGBB)
            if len(color_lower) == 6 and all(c in '0123456789abcdef' for c in color_lower):
                try:
                    # Parse hex string to RGB
                    r = int(color_lower[0:2], 16)
                    g = int(color_lower[2:4], 16)
                    b = int(color_lower[4:6], 16)
                    return f'{"48" if is_background else "38"};2;{r};{g};{b}'
                except ValueError:
                    pass  # Not a valid hex color, continue to other checks

            # Named colors (black, red, green, yellow, blue, magenta, cyan, white)
            color_map = {
                'black': 0, 'red': 1, 'green': 2, 'yellow': 3,
                'blue': 4, 'magenta': 5, 'cyan': 6, 'white': 7
            }

            # Check for bright/intense colors first (pyte may use different formats)
            # Format 1: "brightred", "brightblue", etc.
            if color_lower.startswith('bright') and len(color_lower) > 6:
                color_base = color_lower[6:]  # Remove 'bright' prefix
                if color_base in color_map:
                    # Bright colors: 90-97 (fg), 100-107 (bg)
                    return str(base + 60 + color_map[color_base])

            # Format 2: "bright_red", "bright_blue", etc.
            if color_lower.startswith('bright_'):
                color_base = color_lower[7:]  # Remove 'bright_' prefix
                if color_base in color_map:
                    return str(base + 60 + color_map[color_base])

            # Standard color names
            if color_lower in color_map:
                return str(base + color_map[color_lower])

            # Some terminals use color names like "brown" instead of "yellow"
            color_aliases = {
                'brown': 3,  # yellow
                'lightgray': 7, 'lightgrey': 7,  # white
                'darkgray': 0, 'darkgrey': 0,  # black
            }
            if color_lower in color_aliases:
                return str(base + color_aliases[color_lower])

        elif isinstance(color, int):
            # 256-color palette (0-255)
            # Note: 0-15 are the basic and bright colors, 16-231 are 216 color cube, 232-255 are grayscale
            if 0 <= color <= 255:
                return f'{"48" if is_background else "38"};5;{color}'

        elif isinstance(color, tuple) and len(color) == 3:
            # RGB color (true color / 24-bit)
            try:
                r, g, b = color
                # Ensure values are in valid range
                r = max(0, min(255, int(r)))
                g = max(0, min(255, int(g)))
                b = max(0, min(255, int(b)))
                return f'{"48" if is_background else "38"};2;{r};{g};{b}'
            except (ValueError, TypeError):
                logger.warning("Invalid RGB color tuple: %s", color)
                return None

        # If we got here, we don't recognize the color format
        logger.info("PYTE_COLOR_DEBUG: Unrecognized color format - type: %s, value: %r, is_bg: %s",
                   type(color).__name__, color, is_background)
        return None

    async def reattach_channel(self, new_channel: "Channel") -> None:
        """Reattach this session to a new channel after reconnection."""
        logger.info("Reattaching terminal %s to channel %s", self.id, new_channel.id)
        self.channel = new_channel
        # Restart I/O forwarding with new channel
        await self.start_io_forwarding()


class WindowsTerminalSession(TerminalSession):
    """Terminal session backed by a Windows ConPTY."""

    def __init__(self, session_id: str, pty, channel: "Channel", project_id: Optional[str] = None, terminal_manager: Optional["TerminalManager"] = None):
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

        super().__init__(session_id, _WinPTYProxy(pty), channel, project_id, terminal_manager)
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
                    
                    # Use rate-limited sending instead of immediate sending
                    await self._handle_terminal_data(text)
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
        """Stop the Windows terminal session with comprehensive logging."""
        logger.info("session.stop: Starting stop process for Windows session %s (PID: %s)", 
                   self.id, getattr(self._pty, 'pid', 'unknown'))
        
        try:
            # Check if PTY is still alive
            if self._pty.isalive():
                logger.info("session.stop: Killing PTY process for session %s", self.id)
                self._pty.kill()
            else:
                logger.info("session.stop: PTY process for session %s already exited", self.id)
            
            # Wait for reader task to complete
            if self._reader_task and not self._reader_task.done():
                logger.info("session.stop: Waiting for reader task to complete for Windows session %s", self.id)
                try:
                    await asyncio.wait_for(self._reader_task, timeout=5.0)
                    logger.info("session.stop: Reader task completed for Windows session %s", self.id)
                except asyncio.TimeoutError:
                    logger.warning("session.stop: Reader task timeout for Windows session %s, cancelling", self.id)
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except asyncio.CancelledError:
                        pass
            
            # Cancel and flush any pending terminal data
            if self._debounce_task and not self._debounce_task.done():
                logger.info("session.stop: Cancelling debounce task for Windows session %s", self.id)
                self._debounce_task.cancel()
                try:
                    await self._debounce_task
                except asyncio.CancelledError:
                    pass
            
            # Send any remaining pending data
            if self._pending_data:
                logger.info("session.stop: Flushing pending terminal data for Windows session %s", self.id)
                await self._flush_pending_data()
            
            logger.info("session.stop: Successfully stopped Windows session %s", self.id)
                
        except Exception as exc:
            logger.exception("session.stop: Error stopping Windows session %s: %s", self.id, exc)
            raise


class SessionManager:
    """Manages terminal sessions."""

    def __init__(self, mux, terminal_manager=None):
        self.mux = mux
        self.terminal_manager = terminal_manager
        self._sessions: Dict[str, TerminalSession] = {}
        self._link_event_dispatcher = _get_link_event_dispatcher()
        self._link_event_dispatcher.register_callback(self._handle_link_capture_event)
        self._link_event_dispatcher.start()

    def _allocate_channel_id(self) -> str:
        """Allocate a new unique channel ID for a terminal session using UUID."""
        return uuid.uuid4().hex

    async def create_session(self, shell: Optional[str] = None, cwd: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a new terminal session."""
        # Use the same UUID for both terminal_id and channel_id to ensure consistency
        session_uuid = uuid.uuid4().hex
        term_id = session_uuid
        channel_id = session_uuid
        channel = self.mux.get_channel(channel_id)

        # Choose shell - prefer bash over sh for better terminal compatibility
        if shell is None:
            if not _IS_WINDOWS:
                shell = os.getenv("SHELL")
                # If the default shell is /bin/sh, try to use bash instead for better terminal support
                if shell == "/bin/sh":
                    for bash_path in ["/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]:
                        if os.path.exists(bash_path):
                            shell = bash_path
                            logger.info("Switching from /bin/sh to %s for better terminal compatibility", shell)
                            break
            else:
                shell = os.getenv("COMSPEC", "cmd.exe")

        logger.info("Launching terminal %s using shell=%s on channel=%s", term_id, shell, channel_id)

        env = _build_child_env()

        env["PORTACODE_LINK_CHANNEL"] = str(_LINK_EVENT_ROOT)
        env["PORTACODE_TERMINAL_ID"] = term_id

        bin_dir = prepare_link_capture_bin()
        if bin_dir:
            current_path = env.get("PATH", os.environ.get("PATH", ""))
            path_entries = current_path.split(os.pathsep) if current_path else []
            bin_str = str(bin_dir)
            if bin_str not in path_entries:
                env["PATH"] = os.pathsep.join([bin_str] + path_entries) if path_entries else bin_str
            browser_path = bin_dir / "xdg-open"
            if browser_path.exists():
                original_browser = env.get("BROWSER")
                if original_browser:
                    env[LINK_CAPTURE_ORIGINAL_BROWSER_ENV] = original_browser
                elif LINK_CAPTURE_ORIGINAL_BROWSER_ENV in env:
                    env.pop(LINK_CAPTURE_ORIGINAL_BROWSER_ENV, None)
                env["BROWSER"] = str(browser_path)

        if _IS_WINDOWS:
            try:
                from winpty import PtyProcess
            except ImportError as exc:
                logger.error("winpty (pywinpty) not found: %s", exc)
                raise RuntimeError("pywinpty not installed on client")

            pty_proc = PtyProcess.spawn(shell, cwd=cwd or None, env=env)
            session = WindowsTerminalSession(term_id, pty_proc, channel, project_id, self.terminal_manager)
        else:
            # Unix: try real PTY for proper TTY semantics
            try:
                import pty
                master_fd, slave_fd = pty.openpty()
                _configure_pty_window_size(slave_fd, TERMINAL_ROWS, TERMINAL_COLUMNS)
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                    cwd=cwd,
                    env=env,
                )
                # Wrap master_fd into a StreamReader
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb", buffering=0))
                proc.stdout = reader
                # Use writer for stdin - create a simple file-like wrapper
                proc.stdin = os.fdopen(master_fd, "wb", buffering=0)
            except Exception:
                logger.warning("Failed to allocate PTY, falling back to pipes")
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                )
            session = TerminalSession(term_id, proc, channel, project_id, self.terminal_manager)

        self._sessions[term_id] = session
        await session.start_io_forwarding()

        return {
            "terminal_id": term_id,
            "channel": channel_id,
            "pid": session.proc.pid,
            "shell": shell,
            "cwd": cwd,
            "project_id": project_id,
        }

    async def _handle_link_capture_event(self, payload: Dict[str, Any]) -> None:
        """Translate link capture files into websocket events."""
        link = payload.get("url")
        terminal_id = payload.get("terminal_id")
        if not link:
            logger.debug("session_manager: Ignoring link capture without URL (%s)", payload)
            return
        if not terminal_id:
            logger.warning("session_manager: Link capture missing terminal_id: %s", payload)
            return
        session = self.get_session(terminal_id)
        if not session:
            logger.info("session_manager: No active session for terminal %s, dropping link event", terminal_id)
            return
        if not self.terminal_manager:
            logger.warning("session_manager: No terminal_manager available for link event")
            return

        event_payload = {
            "event": "terminal_link_request",
            "terminal_id": session.id,
            "channel": getattr(session.channel, "id", session.id),
            "url": link,
            "command": payload.get("command"),
            "args": payload.get("args"),
            "pid": getattr(session.proc, "pid", None),
            "timestamp": payload.get("timestamp"),
            "project_id": session.project_id,
        }
        logger.info("session_manager: Dispatching link request for terminal %s to clients", terminal_id)
        await self.terminal_manager._send_session_aware(event_payload, project_id=session.project_id)

    def get_session(self, terminal_id: str) -> Optional[TerminalSession]:
        """Get a terminal session by ID."""
        return self._sessions.get(terminal_id)

    def remove_session(self, terminal_id: str) -> Optional[TerminalSession]:
        """Remove and return a terminal session."""
        session = self._sessions.pop(terminal_id, None)
        if session:
            logger.info("session_manager: Removed session %s (PID: %s) from session manager", 
                       terminal_id, getattr(session.proc, 'pid', 'unknown'))
        else:
            logger.warning("session_manager: Attempted to remove non-existent session %s", terminal_id)
        return session

    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all terminal sessions, optionally filtered by project_id."""
        filtered_sessions = []
        for s in self._sessions.values():
            if project_id == "all":
                filtered_sessions.append(s)
            elif project_id is None:
                if s.project_id is None:
                    filtered_sessions.append(s)
            else:
                if s.project_id == project_id:
                    filtered_sessions.append(s)

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
                "project_id": s.project_id,
            }
            for s in filtered_sessions
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
