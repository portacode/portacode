from __future__ import annotations

"""Cross-platform utilities to install/uninstall a *background* service that
runs ``portacode connect`` automatically at login / boot.

Platforms implemented:

• Linux (systemd **user** service)        – no root privileges required
• macOS (launchd LaunchAgent plist)       – per-user
• Windows (Task Scheduler *ONLOGON* task) – highest privilege, current user

The service simply executes::

    portacode connect

so any configuration (e.g. gateway URL env var) has to be available in the
user's environment at login.
"""

from pathlib import Path
import platform
import subprocess
import sys
import textwrap
import os
from typing import Protocol

__all__ = [
    "ServiceManager",
    "get_manager",
]


class ServiceManager(Protocol):
    """Common interface all platform managers implement."""

    def install(self) -> None:  # noqa: D401 – short description
        """Create + enable the service and start it immediately."""

    def uninstall(self) -> None:
        """Disable + remove the service."""

    def start(self) -> None:
        """Start the service now (if already installed)."""

    def stop(self) -> None:
        """Stop the service if running."""

    def status(self) -> str:
        """Return short human-readable status string (active/running/inactive)."""

    def status_verbose(self) -> str:  # noqa: D401 – optional detailed info
        """Return multi-line diagnostic information suitable for display."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Linux – systemd (user) implementation
# ---------------------------------------------------------------------------

class _SystemdUserService:
    NAME = "portacode"

    def __init__(self) -> None:
        self.service_path = (
            Path.home()
            / ".config/systemd/user"
            / f"{self.NAME}.service"
        )

    # Helper to run *systemctl --user*
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["systemctl", "--user", *args]
        return subprocess.run(cmd, text=True, capture_output=True)

    def install(self) -> None:
        self.service_path.parent.mkdir(parents=True, exist_ok=True)
        unit = textwrap.dedent(
            f"""
            [Unit]
            Description=Portacode persistent connection
            After=network.target

            [Service]
            Type=simple
            ExecStart={sys.executable} -m portacode connect --non-interactive
            Restart=on-failure
            RestartSec=5

            [Install]
            WantedBy=default.target
            """
        ).lstrip()
        self.service_path.write_text(unit)
        self._run("daemon-reload")
        self._run("enable", "--now", self.NAME)

    def uninstall(self) -> None:
        self._run("disable", "--now", self.NAME)
        if self.service_path.exists():
            self.service_path.unlink()
        self._run("daemon-reload")

    def start(self) -> None:
        self._run("start", self.NAME)

    def stop(self) -> None:
        self._run("stop", self.NAME)

    def status(self) -> str:
        res = self._run("is-active", self.NAME)
        state = res.stdout.strip() or res.stderr.strip()
        return state

    def status_verbose(self) -> str:
        # Combine unit status and last 20 journal lines
        status = self._run("status", "--no-pager", self.NAME).stdout
        journal = subprocess.run(
            ["journalctl", "--user", "-n", "20", "-u", f"{self.NAME}.service", "--no-pager"],
            text=True, capture_output=True
        ).stdout
        return (status or "") + "\n--- recent logs ---\n" + (journal or "<no logs>")


# ---------------------------------------------------------------------------
# macOS – launchd (LaunchAgent) implementation
# ---------------------------------------------------------------------------
class _LaunchdService:
    LABEL = "com.portacode.connect"

    def __init__(self) -> None:
        self.plist_path = (
            Path.home()
            / "Library/LaunchAgents"
            / f"{self.LABEL}.plist"
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["launchctl", *args]
        return subprocess.run(cmd, text=True, capture_output=True)

    def install(self) -> None:
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist = textwrap.dedent(
            f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
                <key>Label</key><string>{self.LABEL}</string>
                <key>ProgramArguments</key>
                <array>
                    <string>{sys.executable}</string>
                    <string>-m</string><string>portacode</string>
                    <string>connect</string>
                    <string>--non-interactive</string>
                </array>
                <key>RunAtLoad</key><true/>
                <key>KeepAlive</key><true/>
            </dict>
            </plist>
            """
        ).lstrip()
        self.plist_path.write_text(plist)
        self._run("load", "-w", str(self.plist_path))

    def uninstall(self) -> None:
        self._run("unload", "-w", str(self.plist_path))
        if self.plist_path.exists():
            self.plist_path.unlink()

    def start(self) -> None:
        self._run("start", self.LABEL)

    def stop(self) -> None:
        self._run("stop", self.LABEL)

    def status(self) -> str:
        res = self._run("list", self.LABEL)
        return "running" if res.returncode == 0 else "stopped"

    def status_verbose(self) -> str:
        res = self._run("list", self.LABEL)
        return res.stdout or res.stderr


# ---------------------------------------------------------------------------
# Windows – Task Scheduler implementation
# ---------------------------------------------------------------------------
if sys.platform.startswith("win"):
    import shlex


class _WindowsTask:
    NAME = "PortacodeConnect"

    def __init__(self) -> None:
        from pathlib import Path as _P
        self._home = _P.home()
        self._script_path = self._home / ".local" / "share" / "portacode" / "connect_service.cmd"
        self.log_path = self._home / ".local" / "share" / "portacode" / "connect.log"

    # ------------------------------------------------------------------

    def _run(self, cmd: str | list[str]) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list):
            return subprocess.run(cmd, text=True, capture_output=True)
        return subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def install(self) -> None:
        python = sys.executable
        from pathlib import Path as _P
        pyw = _P(python).with_name("pythonw.exe")
        use_pyw = pyw.exists()

        # Always use wrapper so we can capture logs reliably
        self._script_path.parent.mkdir(parents=True, exist_ok=True)
        py_cmd = f'"{pyw}"' if use_pyw else f'"{python}"'
        script = (
            "@echo off\r\n"
            "cd /d %USERPROFILE%\r\n"
            f"{py_cmd} -m portacode connect --non-interactive >> \"%USERPROFILE%\\.local\\share\\portacode\\connect.log\" 2>>&1\r\n"
        )
        self._script_path.write_text(script)

        cmd = [
            "schtasks", "/Create", "/SC", "ONLOGON", "/RL", "HIGHEST",
            "/TN", self.NAME, "/TR", str(self._script_path), "/F",
        ]
        self._run(cmd)

        # Start the task immediately so user doesn't need to log off/on
        self.start()

    def uninstall(self) -> None:
        self._run(["schtasks", "/Delete", "/TN", self.NAME, "/F"])
        try:
            if self._script_path.exists():
                self._script_path.unlink()
        except Exception:
            pass

    def start(self) -> None:
        self._run(["schtasks", "/Run", "/TN", self.NAME])

    def stop(self) -> None:
        self._run(["schtasks", "/End", "/TN", self.NAME])

    def status(self) -> str:
        res = self._run(["schtasks", "/Query", "/TN", self.NAME])
        if res.returncode != 0:
            return "stopped"
        # When running, output contains "Running"; else "Ready"
        return "running" if "Running" in res.stdout else "stopped"

    def status_verbose(self) -> str:
        res = self._run(["schtasks", "/Query", "/TN", self.NAME, "/V", "/FO", "LIST"])
        return res.stdout or res.stderr


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_manager() -> ServiceManager:
    system = platform.system().lower()
    if system == "linux":
        return _SystemdUserService()  # type: ignore[return-value]
    if system == "darwin":
        return _LaunchdService()      # type: ignore[return-value]
    if system.startswith("windows") or system == "windows":
        return _WindowsTask()         # type: ignore[return-value]
    raise RuntimeError(f"Unsupported platform: {system}") 