from __future__ import annotations

"""
Centralized restart logic used by:

- `portacode restart`
- `portacode setversion`
- Websocket "update_portacode_cli" handler

Goal: all restart-triggering codepaths behave consistently and reuse the same
decision logic.
"""

import os
import sys
from typing import Literal, Optional

RESTART_EXIT_CODE = 42


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def running_under_systemd() -> bool:
    # systemd sets these for services; INVOCATION_ID is the most common.
    if os.getenv("INVOCATION_ID") or os.getenv("JOURNAL_STREAM") or os.getenv("SYSTEMD_EXEC_PID"):
        return True
    # Best-effort parent-process check (Linux only).
    try:
        ppid = os.getppid()
        with open(f"/proc/{ppid}/comm", "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read().strip() == "systemd"
    except Exception:
        return False


def restart_service(*, system_mode: bool = True) -> str:
    """Perform an actual service restart via the platform service manager."""
    from .service import get_manager

    mgr = get_manager(system_mode=system_mode)
    if hasattr(mgr, "restart"):
        mgr.restart()  # type: ignore[attr-defined]
    else:
        # Fallback for any legacy/partial manager.
        try:
            mgr.stop()
        finally:
            mgr.start()

    try:
        return mgr.status()
    except Exception:
        return "unknown"


def request_restart(
    *,
    message: Optional[str] = None,
    method: Literal["auto", "systemctl", "exit"] = "auto",
    in_service: bool = False,
) -> None:
    """
    Request a restart.

    - For manual CLI usage: prefer a real service restart (`systemctl`/OpenRC/etc).
    - For in-service usage (websocket updates): prefer exiting with a special
      non-zero code so the supervisor restarts the process.

    Note: on systems without a supervisor that auto-restarts on non-zero exit,
    callers should ensure the installed service definition provides supervision.
    """

    if message:
        # Keep this module UI-agnostic: plain stdout only.
        print(message)

    method = method.lower()
    if method == "exit":
        raise SystemExit(RESTART_EXIT_CODE)

    if method == "auto":
        # When running under systemd as a service, calling systemctl from within
        # the service can be unreliable; prefer "ask supervisor to restart me".
        if in_service and running_under_systemd() and not _is_interactive_tty():
            raise SystemExit(RESTART_EXIT_CODE)

    if in_service:
        # Best-effort: attempt a manager restart (useful on non-systemd systems).
        # If that fails (often due to missing sudo in non-interactive context),
        # fall back to supervisor restart.
        try:
            restart_service(system_mode=True)
            return
        except Exception:
            raise SystemExit(RESTART_EXIT_CODE)

    restart_service(system_mode=True)
