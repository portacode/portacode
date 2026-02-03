"""Ensure a named tunnel exists and install cloudflared as a service."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .state import (
    SYSTEM_CONFIG_PATH,
    default_cloudflared_dir,
    default_config_path,
    credentials_path_for_tunnel,
)

logger = logging.getLogger(__name__)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


@dataclass(frozen=True)
class TunnelInfo:
    name: str
    tunnel_id: str
    existed: bool


def ensure_cloudflared() -> None:
    if shutil.which("cloudflared") is None:
        print("cloudflared not found in PATH.", file=sys.stderr)
        raise RuntimeError("cloudflared not found in PATH.")


def list_tunnels() -> list[dict]:
    result = run(["cloudflared", "tunnel", "list", "--output", "json"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError("Failed to list tunnels.")
    if not result.stdout.strip():
        return []
    try:
        tunnels = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse tunnel list JSON.") from exc
    return tunnels if isinstance(tunnels, list) else []


def find_tunnel(name: str) -> Optional[dict]:
    tunnels = list_tunnels()
    return next((t for t in tunnels if t.get("name") == name), None)


def create_tunnel(name: str) -> None:
    result = run(["cloudflared", "tunnel", "create", name])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError("Failed to create tunnel.")


def delete_tunnel(tunnel_id: str) -> None:
    if not tunnel_id:
        raise ValueError("Tunnel ID is required to delete a tunnel.")
    result = run(["cloudflared", "tunnel", "delete", tunnel_id])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError(f"Failed to delete tunnel {tunnel_id}.")


def _write_config(config_path: Path, tunnel_id: str, credentials_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            f"tunnel: {tunnel_id}",
            f"credentials-file: {credentials_path}",
            "ingress:",
            "  - service: http_status:404",
            "",
        ]
    )
    config_path.write_text(content, encoding="utf-8")


def _cleanup_system_config() -> None:
    if not SYSTEM_CONFIG_PATH.exists():
        return
    timestamp = int(time.time())
    backup = SYSTEM_CONFIG_PATH.with_suffix(f".{timestamp}.bak")
    try:
        SYSTEM_CONFIG_PATH.replace(backup)
    except OSError:
        try:
            backup.write_text(SYSTEM_CONFIG_PATH.read_text(), encoding="utf-8")
            SYSTEM_CONFIG_PATH.unlink()
        except Exception as exc:
            print(f"Unable to remove {SYSTEM_CONFIG_PATH}: {exc}", file=sys.stderr)
    if SYSTEM_CONFIG_PATH.exists():
        raise RuntimeError(
            f"Conflicting config at {SYSTEM_CONFIG_PATH} could not be removed"
        )


def _remove_user_config() -> None:
    user_config = default_cloudflared_dir() / "config.yml"
    if user_config.exists():
        try:
            user_config.unlink()
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.debug("Failed to remove user config %s: %s", user_config, exc)


def _uninstall_existing_service() -> None:
    result = run(["cloudflared", "service", "uninstall"])
    if result.returncode != 0:
        logger.debug("cloudflared service uninstall returned %d (%s)", result.returncode, result.stderr.strip())


def install_service() -> None:
    _uninstall_existing_service()
    result = run(["cloudflared", "service", "install"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError("Failed to install cloudflared service.")
    result = run(["systemctl", "enable", "--now", "cloudflared"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError("Failed to enable cloudflared service.")


def ensure_tunnel_and_service(
    name: str,
    *,
    config_path: Optional[Path] = None,
    credentials_path: Optional[Path] = None,
) -> TunnelInfo:
    ensure_cloudflared()
    tunnel = find_tunnel(name)
    existed = tunnel is not None
    if not tunnel:
        create_tunnel(name)
        tunnel = find_tunnel(name)
        if not tunnel:
            raise RuntimeError("Tunnel creation succeeded but tunnel not found.")

    tunnel_id = tunnel.get("id") or ""
    if not tunnel_id:
        raise RuntimeError("Tunnel ID missing from tunnel list output.")
    config_path = config_path or default_config_path()
    credentials_path = credentials_path or credentials_path_for_tunnel(str(tunnel_id))
    _cleanup_system_config()
    _remove_user_config()
    _write_config(Path(config_path), str(tunnel_id), Path(credentials_path))
    install_service()
    return TunnelInfo(name=name, tunnel_id=str(tunnel_id), existed=existed)


def download_tunnel_credentials(tunnel_id: str, credentials_path: Path) -> None:
    if not tunnel_id:
        raise ValueError("Tunnel ID is required to download credentials.")
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            "cloudflared",
            "tunnel",
            "token",
            tunnel_id,
            "--cred-file",
            str(credentials_path),
        ]
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError(f"Failed to download credentials for tunnel {tunnel_id}.")


__all__ = [
    "TunnelInfo",
    "ensure_tunnel_and_service",
    "download_tunnel_credentials",
]
