"""Ensure PyYAML is installed and importable.

We avoid `pip install` at runtime because:
- services should not hang on prompts or mutate user site-packages
- pip user-site installs can fail if ~/.local permissions are wrong

Instead we install via the OS package manager using sudo -n when needed.
Supported:
- Alpine: apk -> py3-yaml
- Debian/Ubuntu: apt-get -> python3-yaml
- CentOS/RHEL-like: dnf/yum -> python3-pyyaml
- openSUSE: zypper -> python3-PyYAML
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

from .privileged import have, run, run_checked


def _can_import() -> bool:
    proc = subprocess.run([sys.executable, "-c", "import yaml"], text=True, capture_output=True)
    return proc.returncode == 0


def _detect_pkg_manager() -> Optional[str]:
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


def ensure_pyyaml_installed() -> None:
    if _can_import():
        return

    pkg_mgr = _detect_pkg_manager()
    if pkg_mgr is None:
        raise RuntimeError("PyYAML missing and no supported package manager detected to install it.")

    if pkg_mgr == "apk":
        run_checked(["apk", "add", "--no-cache", "py3-yaml"])
        return
    if pkg_mgr == "apt":
        update = run(["apt-get", "update"])
        if update.returncode not in (0, 100):
            msg = (update.stderr or update.stdout or "").strip() or "Command failed: apt-get update"
            raise RuntimeError(msg)
        run_checked(["apt-get", "install", "-y", "python3-yaml"])
        return
    if pkg_mgr == "dnf":
        run_checked(["dnf", "install", "-y", "python3-pyyaml"])
        return
    if pkg_mgr == "yum":
        run_checked(["yum", "install", "-y", "python3-pyyaml"])
        return
    if pkg_mgr == "zypper":
        run_checked(["zypper", "--non-interactive", "install", "-y", "python3-PyYAML"])
        return

    raise RuntimeError(f"Unsupported package manager: {pkg_mgr}")


__all__ = ["ensure_pyyaml_installed"]
