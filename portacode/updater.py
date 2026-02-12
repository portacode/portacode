"""Helpers shared by CLI and handlers that install Portacode via pip."""

from __future__ import annotations

import os
import sys
import shutil
from typing import List, Optional, Sequence


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

    For non-interactive contexts, callers should ensure the environment has
    permission to write to the chosen install target; we intentionally avoid
    sudo prompts here to keep websocket/service updates safe.
    """
    target = package if version is None else f"{package}=={version}"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    if extra_args:
        cmd.extend(extra_args)

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
