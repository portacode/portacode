"""Ensure a named tunnel exists and install cloudflared as a service.

This module must work on:
- systemd distros (Ubuntu, CentOS, openSUSE, Proxmox host)
- OpenRC distros (Alpine)

And under:
- root (sudo may not exist)
- non-root with passwordless sudo (preferred for containers)
"""

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
from .privileged import ensure_dir, have, run_checked, write_text

logger = logging.getLogger(__name__)

SYSTEM_TOKEN_PATH = Path("/etc/cloudflared/tunnel.token")
WRAPPER_PATH = Path("/usr/local/share/portacode/cloudflared_run.sh")


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


def ensure_tunnel(name: str) -> TunnelInfo:
    """Ensure a named tunnel exists (no service install / no config writes)."""
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
    return TunnelInfo(name=name, tunnel_id=str(tunnel_id), existed=existed)


def write_config(config_path: Path, *, tunnel_id: str, credentials_path: Path) -> None:
    _write_config(Path(config_path), str(tunnel_id), Path(credentials_path))


def ensure_service_installed(*, config_path: Path) -> None:
    install_service(config_path=Path(config_path))


def _write_config(config_path: Path, tunnel_id: str, credentials_path: Path) -> None:
    ensure_dir(config_path.parent)
    lines = [f"tunnel: {tunnel_id}"]
    # When running cloudflared with a token, credentials-file is optional and may
    # not exist. Keep the directive only when the file is present.
    try:
        if credentials_path and Path(credentials_path).exists():
            lines.append(f"credentials-file: {credentials_path}")
    except OSError:
        pass
    lines += [
        "ingress:",
        "  - service: http_status:404",
        "",
    ]
    content = "\n".join(lines)
    write_text(config_path, content, mode=0o644)


def system_credentials_path_for_tunnel(tunnel_id: str) -> Path:
    # Keep system tunnel credentials alongside the system config for consistency.
    # This avoids coupling the system service to any particular user's $HOME.
    return Path("/etc/cloudflared") / f"{tunnel_id}.json"


def _remove_user_config() -> None:
    user_config = default_cloudflared_dir() / "config.yml"
    if user_config.exists():
        try:
            user_config.unlink()
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.debug("Failed to remove user config %s: %s", user_config, exc)


def _uninstall_existing_service() -> None:
    # Best-effort: try cloudflared built-in uninstall first (systemd environments),
    # then fall back to disabling via init system.
    result = run(["cloudflared", "service", "uninstall"])
    if result.returncode != 0:
        logger.debug(
            "cloudflared service uninstall returned %d (%s)",
            result.returncode,
            (result.stderr or "").strip(),
        )


def _detect_init() -> str:
    if have("systemctl"):
        return "systemd"
    if have("rc-service") or Path("/sbin/openrc").exists():
        return "openrc"
    return "none"


def _wrapper_text() -> str:
    cloudflared = shutil.which("cloudflared") or "/usr/local/bin/cloudflared"
    return "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            'CFG="${1:-/etc/cloudflared/config.yml}"',
            f'TOKEN="{SYSTEM_TOKEN_PATH}"',
            'if [ -s "$TOKEN" ]; then',
            '  tok="$(cat "$TOKEN" | tr -d \'\\r\\n\')"',
            f'  exec {cloudflared} --config "$CFG" tunnel run --token "$tok"',
            "fi",
            f'exec {cloudflared} --config "$CFG" tunnel run',
            "",
        ]
    )


def _systemd_unit_text(config_path: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Cloudflare Tunnel (cloudflared)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={WRAPPER_PATH} {config_path}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _openrc_script_text(config_path: Path) -> str:
    return "\n".join(
        [
            "#!/sbin/openrc-run",
            'description="Cloudflare Tunnel (cloudflared)"',
            "",
            f'command="{WRAPPER_PATH}"',
            f'command_args="{config_path}"',
            'pidfile="/run/cloudflared.pid"',
            "supervisor=supervise-daemon",
            "respawn_delay=5",
            "respawn_max=0",
            "",
            "depend() {",
            "  need net",
            "}",
            "",
        ]
    )


def install_service(*, config_path: Path) -> None:
    _uninstall_existing_service()
    init = _detect_init()
    # Ensure wrapper exists before enabling the service.
    write_text(WRAPPER_PATH, _wrapper_text(), mode=0o755)
    if init == "systemd":
        unit_path = Path("/etc/systemd/system/cloudflared.service")
        write_text(unit_path, _systemd_unit_text(config_path), mode=0o644)
        run_checked(["systemctl", "daemon-reload"])
        run_checked(["systemctl", "enable", "--now", "cloudflared"])
        # Ensure the running service picks up the current config/unit content.
        run_checked(["systemctl", "restart", "cloudflared"])
        return
    if init == "openrc":
        init_path = Path("/etc/init.d/cloudflared")
        write_text(init_path, _openrc_script_text(config_path), mode=0o755)
        # Enable at boot and start now
        try:
            run_checked(["rc-update", "add", "cloudflared", "default"])
        except RuntimeError as exc:
            msg = str(exc).strip().lower()
            if "exists" not in msg and "already" not in msg:
                raise
        # Restart to ensure config changes are picked up.
        run_checked(["rc-service", "cloudflared", "restart"])
        return
    raise RuntimeError("Unsupported init system for cloudflared service (no systemctl or OpenRC).")


def restart_service() -> None:
    init = _detect_init()
    if init == "systemd":
        # reload may fail if not supported; fall back to restart
        try:
            run_checked(["systemctl", "reload", "cloudflared"])
        except Exception:
            run_checked(["systemctl", "restart", "cloudflared"])
        return
    if init == "openrc":
        run_checked(["rc-service", "cloudflared", "restart"])
        return
    # No init system: nothing to restart.
    return


def ensure_tunnel_and_service(
    name: str,
    *,
    config_path: Optional[Path] = None,
    credentials_path: Optional[Path] = None,
) -> TunnelInfo:
    tunnel_info = ensure_tunnel(name)
    config_path = config_path or default_config_path()
    credentials_path = credentials_path or credentials_path_for_tunnel(str(tunnel_info.tunnel_id))
    _write_config(Path(config_path), str(tunnel_info.tunnel_id), Path(credentials_path))
    install_service(config_path=Path(config_path))
    return tunnel_info


def download_tunnel_credentials(tunnel_id: str, credentials_path: Path) -> None:
    if not tunnel_id:
        raise ValueError("Tunnel ID is required to download credentials.")
    if credentials_path.exists():
        return

    # If cloudflared created user-scoped credentials during `tunnel create`, prefer
    # copying them into the system location instead of generating a token flow.
    user_creds = default_cloudflared_dir() / f"{tunnel_id}.json"
    if user_creds.exists():
        ensure_dir(credentials_path.parent)
        from .privileged import copy_file as _copy_file

        _copy_file(user_creds, credentials_path, mode=0o600)
        return

    # Fallback: use a tunnel token. This is supported by cloudflared across distros,
    # and avoids relying on a credentials JSON file being generated.
    result = subprocess.run(
        ["cloudflared", "tunnel", "token", tunnel_id],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        out = (result.stdout or "").strip()
        if err:
            print(err, file=sys.stderr)
        if out and out != err:
            print(out, file=sys.stderr)
        details = err or out or f"exit={result.returncode}"
        raise RuntimeError(f"Failed to fetch tunnel token for {tunnel_id}: {details}")

    token = (result.stdout or "").strip()
    if not token:
        raise RuntimeError(f"cloudflared tunnel token returned empty output for {tunnel_id}.")
    ensure_dir(SYSTEM_TOKEN_PATH.parent)
    write_text(SYSTEM_TOKEN_PATH, token + "\n", mode=0o600)


__all__ = [
    "TunnelInfo",
    "ensure_tunnel",
    "ensure_tunnel_and_service",
    "download_tunnel_credentials",
    "restart_service",
    "system_credentials_path_for_tunnel",
    "write_config",
    "ensure_service_installed",
    "SYSTEM_TOKEN_PATH",
    "WRAPPER_PATH",
]
