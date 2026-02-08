"""Helpers shared by CLI and handlers that install Portacode via pip."""

from __future__ import annotations

import os
import sys
from typing import List, Sequence


def build_pip_install_command(
    package: str = "portacode",
    version: str | None = None,
    extra_args: Sequence[str] | None = None,
) -> List[str]:
    """Return a pip install command list that upgrades the target package.

    The command runs as root if the current process already has elevated privileges;
    otherwise it is wrapped with ``sudo -H -n`` so the installer can write to
    system site-packages without prompting for a password.
    """
    target = package if version is None else f"{package}=={version}"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    if extra_args:
        cmd.extend(extra_args)
    if getattr(os, "geteuid", lambda: 0)() != 0:
        cmd = ["sudo", "-H", "-n", *cmd]
    return cmd
