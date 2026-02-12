"""Ensure cloudflared is installed and return its version.

Design goals:
- Work when cloudflared is already present in PATH (any distro/user).
- When missing, install on at least: Alpine (apk), Debian/Ubuntu (apt),
  CentOS/RHEL-like (yum/dnf), openSUSE (zypper).
- Support non-root execution when the caller is a sudoer (prefer sudo -n for
  non-interactive environments; fail fast with clear errors).

Implementation notes:
- Prefer installing a single cloudflared binary from GitHub "latest" releases.
  This avoids distro-specific repo configuration and works across distros,
  including minimal containers.
- Use the system package manager only to ensure prerequisites (curl/wget and
  ca-certificates) if needed.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

_BIN_DIR = Path("/usr/local/bin")
_BIN_PATH = _BIN_DIR / "cloudflared"

_GITHUB_LATEST_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"
_BINARY_BY_ARCH = {
    # platform.machine() -> GitHub artifact suffix
    "x86_64": "linux-amd64",
    "amd64": "linux-amd64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
    "armv7l": "linux-arm",
    "armv6l": "linux-arm",
    "i386": "linux-386",
    "i686": "linux-386",
}


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _sudo_prefix(non_interactive: bool = True) -> Optional[list[str]]:
    if is_root():
        return []
    if not have("sudo"):
        return None
    # Prefer -n (no prompt) to avoid hangs in services/automation.
    if non_interactive:
        return ["sudo", "-n"]
    return ["sudo"]


def _run_checked(cmd: list[str], *, allow_sudo: bool = True) -> None:
    prefix: list[str] = []
    if allow_sudo and not is_root():
        sp = _sudo_prefix(non_interactive=True)
        if sp is None:
            raise RuntimeError("Root privileges required but sudo is not available.")
        prefix = sp
    proc = subprocess.run([*prefix, *cmd], text=True, capture_output=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or f"Command failed: {' '.join(cmd)}"
        raise RuntimeError(msg)


def _cloudflared_version() -> str:
    return subprocess.check_output(["cloudflared", "--version"], text=True).strip()


def _detect_pkg_manager() -> Optional[str]:
    # Order matters: prefer newer tooling when both exist.
    if have("apk"):
        return "apk"
    if have("apt-get"):
        return "apt"
    if have("dnf"):
        return "dnf"
    if have("yum"):
        return "yum"
    if have("zypper"):
        return "zypper"
    return None


def _install_prereqs(pkg_mgr: Optional[str]) -> None:
    # Needed for downloading the binary securely.
    if have("curl") or have("wget"):
        return
    if pkg_mgr is None:
        raise RuntimeError("Neither curl nor wget is available and no supported package manager was detected.")

    if pkg_mgr == "apk":
        _run_checked(["apk", "add", "--no-cache", "ca-certificates", "curl"])
        return
    if pkg_mgr == "apt":
        _run_checked(["apt-get", "update"])
        _run_checked(["apt-get", "install", "-y", "ca-certificates", "curl"])
        return
    if pkg_mgr == "dnf":
        _run_checked(["dnf", "install", "-y", "ca-certificates", "curl"])
        return
    if pkg_mgr == "yum":
        _run_checked(["yum", "install", "-y", "ca-certificates", "curl"])
        return
    if pkg_mgr == "zypper":
        _run_checked(["zypper", "--non-interactive", "install", "-y", "ca-certificates", "curl"])
        return
    raise RuntimeError(f"Unsupported package manager: {pkg_mgr}")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if have("curl"):
        _run_checked(["curl", "-fL", url, "-o", str(dest)], allow_sudo=False)
        return
    if have("wget"):
        _run_checked(["wget", "-O", str(dest), url], allow_sudo=False)
        return
    raise RuntimeError("No downloader available (curl/wget).")


def _arch_suffix() -> str:
    machine = (platform.machine() or "").strip().lower()
    suffix = _BINARY_BY_ARCH.get(machine)
    if not suffix:
        raise RuntimeError(f"Unsupported CPU architecture for cloudflared binary install: {machine!r}")
    return suffix


def _install_cloudflared_binary() -> None:
    suffix = _arch_suffix()
    url = f"{_GITHUB_LATEST_BASE}/cloudflared-{suffix}"

    with tempfile.TemporaryDirectory(prefix="portacode-cloudflared-") as td:
        tmp = Path(td) / "cloudflared"
        _download(url, tmp)
        tmp.chmod(0o755)

        # install(1) preserves mode and is widely available; fallback to mv+chmod.
        if have("install"):
            _run_checked(["install", "-m", "0755", str(tmp), str(_BIN_PATH)])
        else:
            _run_checked(["mkdir", "-p", str(_BIN_DIR)])
            _run_checked(["mv", str(tmp), str(_BIN_PATH)])
            _run_checked(["chmod", "0755", str(_BIN_PATH)])


def ensure_cloudflared_installed() -> str:
    if have("cloudflared"):
        return _cloudflared_version()

    pkg_mgr = _detect_pkg_manager()
    _install_prereqs(pkg_mgr)

    # Install the binary to a standard location. This works for Alpine + most
    # mainstream distros without extra repo setup.
    _install_cloudflared_binary()

    if not have("cloudflared"):
        # Should not happen if /usr/local/bin is in PATH, but keep error actionable.
        raise RuntimeError(
            f"cloudflared installed to {_BIN_PATH} but not found in PATH. Ensure /usr/local/bin is in PATH."
        )
    return _cloudflared_version()


__all__ = ["ensure_cloudflared_installed"]
