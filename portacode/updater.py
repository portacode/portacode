"""Helpers shared by CLI and handlers that install Portacode via pip."""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from typing import List, Optional, Sequence


def _running_in_virtualenv() -> bool:
    """Return True when the current interpreter is inside a virtual environment."""
    if hasattr(sys, "real_prefix"):
        return True
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    return sys.prefix != base_prefix


def _is_permission_error(output: str) -> bool:
    text = (output or "").lower()
    return "permission denied" in text or "errno 13" in text


def _sudo_requires_password(output: str) -> bool:
    text = (output or "").lower()
    return ("sudo:" in text and "password" in text) or "a password is required" in text


def run_pip_install_command(
    cmd: Sequence[str],
    *,
    allow_sudo_fallback: bool = False,
    interactive_sudo: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a pip install command, with optional sudo fallback on permission errors."""
    result = subprocess.run(list(cmd), capture_output=True, text=True)
    if result.returncode == 0:
        return result

    if not allow_sudo_fallback:
        return result

    if os.geteuid() == 0 or not shutil.which("sudo"):
        return result

    combined = f"{result.stderr or ''}\n{result.stdout or ''}"
    if not _is_permission_error(combined):
        return result

    sudo_non_interactive = subprocess.run(
        ["sudo", "-n", *cmd], capture_output=True, text=True
    )
    if sudo_non_interactive.returncode == 0:
        return sudo_non_interactive

    if not interactive_sudo:
        return sudo_non_interactive

    sudo_output = f"{sudo_non_interactive.stderr or ''}\n{sudo_non_interactive.stdout or ''}"
    if _sudo_requires_password(sudo_output):
        return subprocess.run(["sudo", *cmd], capture_output=True, text=True)

    return sudo_non_interactive


def build_pip_install_command(
    package: str = "portacode",
    version: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
) -> List[str]:
    """Return a pip install command list that upgrades the target package.

    By default this targets the *current user* environment.

    - If running as a normal user, we use `pip install --user ...` so the
      running service (typically configured with `User=...`) and the user's CLI
      load the same version.
    - If running as root via sudo, we try to install into the invoking user's
      `--user` site-packages (SUDO_USER) to avoid installing into root's Python.
    - If running from a virtualenv interpreter, install directly into that
      virtualenv (never pass `--user`, which pip rejects in venvs).

    For non-interactive contexts, callers should ensure the environment has
    permission to write to the chosen install target; we intentionally avoid
    sudo prompts here to keep websocket/service updates safe.
    """
    target = package if version is None else f"{package}=={version}"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    if extra_args:
        cmd.extend(extra_args)

    if _running_in_virtualenv():
        return cmd

    geteuid = getattr(os, "geteuid", None)
    is_root = bool(geteuid and geteuid() == 0)

    # Prefer user installs for consistency between the service user and the CLI user.
    pip_user_flag = ["--user"]

    if not is_root:
        return [*cmd, *pip_user_flag]

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        # If the operator ran `sudo portacode setversion ...`, still install into
        # the invoking user's environment, not root's.
        if shutil.which("sudo"):
            return ["sudo", "-u", sudo_user, "-H", *cmd, *pip_user_flag]

    # Root (or no sudo available): fall back to system site-packages.
    return cmd
