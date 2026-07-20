"""Stash the latest Codex usage-limit reset timestamp seen by the loopback proxy."""

from __future__ import annotations

import time
from typing import Optional

_last_resets_at: Optional[int] = None
_last_noted_at: float = 0.0


def note_usage_limit_resets_at(resets_at: Optional[int]) -> None:
    global _last_resets_at, _last_noted_at
    if resets_at is None:
        return
    try:
        value = int(resets_at)
    except (TypeError, ValueError):
        return
    if value > 10_000_000_000:
        value //= 1000
    if value < 1_000_000_000:
        return
    _last_resets_at = value
    _last_noted_at = time.time()


def peek_usage_limit_resets_at(max_age_sec: float = 300.0) -> Optional[int]:
    if _last_resets_at is None:
        return None
    if time.time() - _last_noted_at > max_age_sec:
        return None
    return _last_resets_at


def attach_resets_at_to_params(params: dict) -> dict:
    """Copy params and inject resetsAt onto nested error when available."""
    resets_at = peek_usage_limit_resets_at()
    if resets_at is None:
        return params
    out = dict(params or {})
    err = out.get("error")
    if isinstance(err, dict):
        err = dict(err)
        err.setdefault("resetsAt", resets_at)
        err.setdefault("resets_at", resets_at)
        out["error"] = err
    else:
        out["resetsAt"] = resets_at
        out["resets_at"] = resets_at
    return out
