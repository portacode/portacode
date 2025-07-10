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
import shutil
import pwd

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

    def __init__(self, system_mode: bool = False) -> None:
        self.system_mode = system_mode
        if system_mode:
            self.service_path = Path("/etc/systemd/system") / f"{self.NAME}.service"
            self.user = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.getlogin()
            try:
                self.home = Path(pwd.getpwnam(self.user).pw_dir)
            except KeyError:
                self.home = Path("/root") if self.user == "root" else Path(f"/home/{self.user}")
            self.python = shutil.which("python3") or sys.executable
        else:
            self.service_path = (
                Path.home() / ".config/systemd/user" / f"{self.NAME}.service"
            )
            self.user = os.environ.get("USER") or os.getlogin()
            self.home = Path.home()
            self.python = sys.executable

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        if self.system_mode:
            sudo_needed = os.geteuid() != 0
            base = ["systemctl"] if not sudo_needed else ["sudo", "systemctl"]
            cmd = [*base, *args]
        else:
            cmd = ["systemctl", "--user", *args]
        return subprocess.run(cmd, text=True, capture_output=True)

    def install(self) -> None:
        self.service_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Capture current SHELL for the service to prevent using /bin/sh in containers/virtualized environments
        current_shell = os.getenv("SHELL", "/bin/bash")
        
        if self.system_mode:
            sudo_needed = os.geteuid() != 0
            prefix = ["sudo"] if sudo_needed else []
            unit = textwrap.dedent(f"""
                [Unit]
                Description=Portacode persistent connection (system-wide)
                After=network.target

                [Service]
                Type=simple
                User={self.user}
                WorkingDirectory={self.home}
                Environment=SHELL={current_shell}
                ExecStart={self.python} -m portacode connect --non-interactive
                Restart=on-failure
                RestartSec=5

                [Install]
                WantedBy=multi-user.target
            """).lstrip()
        else:
            unit = textwrap.dedent(f"""
                [Unit]
                Description=Portacode persistent connection
                After=network.target

                [Service]
                Type=simple
                Environment=SHELL={current_shell}
                ExecStart={self.python} -m portacode.cli connect --non-interactive
                Restart=on-failure
                RestartSec=5

                [Install]
                WantedBy=default.target
            """).lstrip()
        self.service_path.write_text(unit)
        if self.system_mode:
            sudo_needed = os.geteuid() != 0
            prefix = ["sudo"] if sudo_needed else []
            subprocess.run([*prefix, "systemctl", "daemon-reload"])
            subprocess.run([*prefix, "systemctl", "enable", "--now", self.NAME])
        else:
            self._run("daemon-reload")
            self._run("enable", "--now", self.NAME)

    def uninstall(self) -> None:
        if self.system_mode:
            sudo_needed = os.geteuid() != 0
            prefix = ["sudo"] if sudo_needed else []
            subprocess.run([*prefix, "systemctl", "disable", "--now", self.NAME])
            if self.service_path.exists():
                subprocess.run([*prefix, "rm", str(self.service_path)])
            subprocess.run([*prefix, "systemctl", "daemon-reload"])
        else:
            self._run("disable", "--now", self.NAME)
            if self.service_path.exists():
                self.service_path.unlink()
            self._run("daemon-reload")

    def start(self) -> None:
        if self.system_mode:
            prefix = ["sudo"] if os.geteuid() != 0 else []
            subprocess.run([*prefix, "systemctl", "start", self.NAME])
        else:
            self._run("start", self.NAME)

    def stop(self) -> None:
        if self.system_mode:
            prefix = ["sudo"] if os.geteuid() != 0 else []
            subprocess.run([*prefix, "systemctl", "stop", self.NAME])
        else:
            self._run("stop", self.NAME)

    def status(self) -> str:
        if self.system_mode:
            prefix = ["sudo"] if os.geteuid() != 0 else []
            res = subprocess.run([*prefix, "systemctl", "is-active", self.NAME], text=True, capture_output=True)
            state = res.stdout.strip() or res.stderr.strip()
            return state
        else:
            res = self._run("is-active", self.NAME)
            state = res.stdout.strip() or res.stderr.strip()
            return state

    def status_verbose(self) -> str:
        if self.system_mode:
            prefix = ["sudo"] if os.geteuid() != 0 else []
            res = subprocess.run([*prefix, "systemctl", "status", "--no-pager", self.NAME], text=True, capture_output=True)
            status = res.stdout or res.stderr
            journal = subprocess.run([
                *(prefix or ["sudo"] if os.geteuid()!=0 else []), "journalctl", "-n", "20", "-u", f"{self.NAME}.service", "--no-pager"
            ], text=True, capture_output=True).stdout
            return (status or "") + "\n--- recent logs ---\n" + (journal or "<no logs>")
        else:
            res = self._run("status", "--no-pager", self.NAME)
            status = res.stdout or res.stderr
            journal = subprocess.run([
                "journalctl", "--user", "-n", "20", "-u", f"{self.NAME}.service", "--no-pager"
            ], text=True, capture_output=True).stdout
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

        # Use cmd.exe /c to ensure the batch file is found and executed correctly
        action = f'cmd.exe /c "{self._script_path}"'
        cmd = [
            "schtasks", "/Create", "/SC", "ONLOGON", "/RL", "HIGHEST",
            "/TN", self.NAME, "/TR", action, "/F",
        ]
        res = self._run(cmd)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout)

        # Start immediately and verify
        self.start()
        self._wait_until_running()

    def uninstall(self) -> None:
        self._run(["schtasks", "/Delete", "/TN", self.NAME, "/F"])
        # Kill all running portacode connect processes for this user
        self._kill_all_connect()
        try:
            if self._script_path.exists():
                self._script_path.unlink()
        except Exception:
            pass

    def start(self) -> None:
        res = self._run(["schtasks", "/Run", "/TN", self.NAME])
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout)
        # wait till running or raise with details
        self._wait_until_running()

    def stop(self) -> None:
        self._run(["schtasks", "/End", "/TN", self.NAME])
        self._kill_all_connect()

    def status(self) -> str:
        res = self._run(["schtasks", "/Query", "/TN", self.NAME])
        if res.returncode != 0:
            return "stopped"
        # When running, output contains "Running"; else "Ready"
        return "running" if "Running" in res.stdout else "stopped"

    def status_verbose(self) -> str:
        res = self._run(["schtasks", "/Query", "/TN", self.NAME, "/V", "/FO", "LIST"])
        return res.stdout or res.stderr

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _query_task(self) -> dict[str, str]:
        """Return key→value mapping of *schtasks /Query /V /FO LIST* output."""
        res = self._run(["schtasks", "/Query", "/TN", self.NAME, "/V", "/FO", "LIST"])
        if res.returncode != 0:
            return {}
        out: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out

    def _tail_log(self, lines: int = 20) -> str:
        try:
            if self.log_path.exists():
                with self.log_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.readlines()
                    tail = "".join(content[-lines:])
                    return tail
        except Exception:
            pass
        return "<no log available>"

    def _wait_until_running(self, timeout: int = 15) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self._query_task()
            status = info.get("Status")
            if status == "Running":
                return  # success
            if status and status != "Ready":
                # Task executed but stopped – raise with last result
                code = info.get("Last Result", "?")
                log_tail = self._tail_log()
                raise RuntimeError(f"Task stopped (LastResult={code}).\n--- log ---\n{log_tail}")
            time.sleep(1)
        # Timeout
        log_tail = self._tail_log()
        raise RuntimeError(f"Task did not reach Running state within {timeout}s.\n--- log ---\n{log_tail}")

    def _kill_all_connect(self):
        import subprocess, os
        try:
            # List all python/pythonw processes for this user
            whoami = os.getlogin()
            for exe in ["python.exe", "pythonw.exe"]:
                out = subprocess.run([
                    "wmic", "process", "where",
                    f"name='{exe}' and CommandLine like '%portacode connect%' and (UserModeTime > 0 or KernelModeTime > 0)",
                    "get", "ProcessId,CommandLine,Name,UserModeTime,KernelModeTime", "/FORMAT:csv"
                ], capture_output=True, text=True)
                for line in out.stdout.splitlines():
                    if "portacode connect" in line and whoami in line:
                        parts = line.split(",")
                        try:
                            pid = int(parts[-1])
                            os.kill(pid, 9)
                        except Exception:
                            pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_manager(system_mode: bool = False) -> ServiceManager:
    system = platform.system().lower()
    if system == "linux":
        return _SystemdUserService(system_mode=system_mode)  # type: ignore[return-value]
    if system == "darwin":
        return _LaunchdService()      # type: ignore[return-value]
    if system.startswith("windows") or system == "windows":
        return _WindowsTask()         # type: ignore[return-value]
    raise RuntimeError(f"Unsupported platform: {system}") 