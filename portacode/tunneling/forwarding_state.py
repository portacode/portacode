"""Persist Cloudflare forwarding metadata."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import platformdirs

CONFIG_DIR = Path(platformdirs.user_config_dir("portacode"))
STATE_PATH = CONFIG_DIR / "cloudflare_forwarding.json"


def _current_time_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_forwarding_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"rules": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"rules": []}


def persist_forwarding_state(rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"rules": rules, "updated_at": _current_time_iso()}
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)
    return payload


__all__ = ["STATE_PATH", "load_forwarding_state", "persist_forwarding_state"]
