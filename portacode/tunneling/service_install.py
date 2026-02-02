"""Ensure a named tunnel exists and install cloudflared as a service."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from .state import default_config_path, credentials_path_for_tunnel


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


def install_service() -> None:
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
    _write_config(Path(config_path), str(tunnel_id), Path(credentials_path))
    install_service()
    return TunnelInfo(name=name, tunnel_id=str(tunnel_id), existed=existed)


__all__ = ["TunnelInfo", "ensure_tunnel_and_service"]
