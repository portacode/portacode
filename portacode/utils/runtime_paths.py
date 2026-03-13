"""Helpers for normalizing runtime-provided filesystem paths on the device."""

from __future__ import annotations

import os


def expand_runtime_path(path: str) -> str:
    """Expand shell-style user and env markers into an absolute device path."""
    expanded = os.path.expanduser(path)
    expanded = os.path.expandvars(expanded)
    return os.path.abspath(expanded)
