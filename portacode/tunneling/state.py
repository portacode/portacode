"""Persist Cloudflare tunnel metadata for the device."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import platformdirs

CONFIG_DIR = Path(platformdirs.user_config_dir("portacode"))
STATE_PATH = CONFIG_DIR / "cloudflare_tunnel.json"
SYSTEM_CONFIG_PATH = Path("/etc/cloudflared/config.yml")


def _current_time_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_cloudflared_dir() -> Path:
    return Path.home() / ".cloudflared"


def default_cert_path() -> Path:
    return default_cloudflared_dir() / "cert.pem"


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def default_config_path() -> Path:
    if _running_as_root():
        return SYSTEM_CONFIG_PATH
    return default_cloudflared_dir() / "config.yml"


def credentials_path_for_tunnel(tunnel_id: str) -> Path:
    return default_cloudflared_dir() / f"{tunnel_id}.json"


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)
    os.chmod(STATE_PATH, stat.S_IRUSR | stat.S_IWUSR)


def update_state(patch: Dict[str, Any]) -> Dict[str, Any]:
    data = load_state()
    data.update(patch)
    data.setdefault("configured", True)
    data["updated_at"] = _current_time_iso()
    save_state(data)
    return data


def clear_state() -> None:
    if STATE_PATH.exists():
        try:
            STATE_PATH.unlink()
        except OSError:
            pass


__all__ = [
    "CONFIG_DIR",
    "STATE_PATH",
    "default_cert_path",
    "default_config_path",
    "credentials_path_for_tunnel",
    "SYSTEM_CONFIG_PATH",
    "clear_state",
    "load_state",
    "save_state",
    "update_state",
]
