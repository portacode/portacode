"""Helpers for normalizing runtime-provided filesystem paths on the device."""

from __future__ import annotations

import os
import pwd


def _resolve_runtime_user_home() -> str | None:
    runtime_user = (os.environ.get("PORTACODE_DEFAULT_RUNTIME_USER") or "").strip()
    if not runtime_user:
        return None
    try:
        return pwd.getpwnam(runtime_user).pw_dir
    except KeyError:
        return "/root" if runtime_user == "root" else f"/home/{runtime_user}"


def expand_runtime_path(path: str) -> str:
    """Expand shell-style user and env markers into an absolute device path."""
    expanded = path
    runtime_home = _resolve_runtime_user_home()
    if runtime_home and (expanded == "~" or expanded.startswith("~/")):
        expanded = runtime_home + expanded[1:]
    expanded = os.path.expanduser(expanded)
    expanded = os.path.expandvars(expanded)
    return os.path.abspath(expanded)
