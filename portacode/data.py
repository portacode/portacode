from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "portacode"
APP_AUTHOR = "Portacode"


def get_data_dir() -> Path:
    """Return the platform-specific *user data* directory for Portacode.

    This is where all persistent user data (keypairs, logs, pid files, …) is stored.
    The directory is created on first use.
    """

    data_dir = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_key_dir() -> Path:
    key_dir = get_data_dir() / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    return key_dir


def get_runtime_dir() -> Path:
    """Return directory for runtime files (pid, sockets, …)."""
    runtime_dir = get_data_dir() / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_pid_file() -> Path:
    return get_runtime_dir() / "gateway.pid"


def get_gateway_lock_file() -> Path:
    """Return the host-wide lock used to serialize gateway connections.

    This deliberately does not live below the user data directory: separate
    Unix users on the same host must contend for the same lock.  The PID file
    remains in its historical location for compatibility with older clients.
    """
    # TMPDIR is commonly user-specific, so use the shared Unix temporary
    # directory explicitly. Windows' temp directory is already the appropriate
    # common coordination location for this file-lock implementation.
    base_dir = Path(tempfile.gettempdir()) if sys.platform.startswith("win") else Path("/tmp")
    return base_dir / "portacode-gateway.lock"


def is_process_running(pid: int) -> bool:
    """Check whether *pid* refers to a currently running process."""
    if sys.platform.startswith("win"):
        import ctypes
        import ctypes.wintypes as wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
