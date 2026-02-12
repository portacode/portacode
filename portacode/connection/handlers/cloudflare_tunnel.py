"""Cloudflare tunnel setup handler for Proxmox infrastructure nodes."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .base import SyncHandler
from portacode.tunneling.ensure_cloudflared import ensure_cloudflared_installed
from portacode.tunneling.ensure_pyyaml import ensure_pyyaml_installed
from portacode.tunneling.cloudflared_login import run_login
from portacode.tunneling.get_domain import get_authenticated_domain
from portacode.tunneling.service_install import (
    download_tunnel_credentials,
    ensure_service_installed,
    ensure_tunnel,
    system_credentials_path_for_tunnel,
    write_config,
    SYSTEM_TOKEN_PATH,
)
from portacode.tunneling.state import (
    clear_state,
    credentials_path_for_tunnel,
    default_cert_path,
    default_cloudflared_dir,
    load_state,
    update_state,
)

logger = logging.getLogger(__name__)


def _emit_cloudflare_event(handler: SyncHandler, payload: Dict[str, Any]) -> None:
    loop = handler.context.get("event_loop")
    if not loop or loop.is_closed():
        logger.debug("cloudflare event skipped (no event loop) event=%s", payload.get("event"))
        return
    future = asyncio.run_coroutine_threadsafe(handler.send_response(payload), loop)
    future.add_done_callback(
        lambda fut: logger.warning(
            "Failed to emit cloudflare event %s: %s", payload.get("event"), fut.exception()
        )
        if fut.exception()
        else None
    )


def _ensure_pyyaml() -> None:
    # Needed for parsing/modifying existing cloudflared config on some platforms.
    # Install via OS package manager (sudo-aware) to avoid pip user-site permission issues.
    ensure_pyyaml_installed()


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _safe_remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Failed to remove %s: %s", path, exc)


def _reset_previous_installation() -> None:
    previous = load_state()
    _safe_remove(default_cert_path())
    _safe_remove(default_cloudflared_dir() / "config.yml")
    if previous.get("tunnel_id") and previous.get("credentials_file"):
        previous_path = Path(previous["credentials_file"])
        if previous_path.exists():
            logger.debug("Preserving existing credentials file %s", previous_path)
        else:
            logger.debug("Credentials file %s missing, will regenerate if needed", previous_path)
    clear_state()


def _build_tunnel_name(device_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9-]+", "-", device_id.strip()).strip("-")
    normalized = normalized.lower() or "device"
    return f"portacode-proxmox-{normalized}"


def _ensure_tunnel_with_credentials(tunnel_name: str, config_path: Path) -> TunnelInfo:
    tunnel_info = ensure_tunnel(tunnel_name)
    system_install = str(config_path).startswith("/etc/")
    credentials_path = (
        Path(system_credentials_path_for_tunnel(tunnel_info.tunnel_id))
        if system_install
        else Path(credentials_path_for_tunnel(tunnel_info.tunnel_id))
    )
    if credentials_path.exists():
        # Ensure config/service are still present.
        write_config(Path(config_path), tunnel_id=tunnel_info.tunnel_id, credentials_path=credentials_path)
        ensure_service_installed(config_path=Path(config_path))
        return tunnel_info

    download_tunnel_credentials(tunnel_info.tunnel_id, credentials_path)
    if not credentials_path.exists():
        raise RuntimeError(
            f"Cloudflare credentials file {credentials_path} still missing after downloading token."
        )
    write_config(Path(config_path), tunnel_id=tunnel_info.tunnel_id, credentials_path=credentials_path)
    ensure_service_installed(config_path=Path(config_path))
    return tunnel_info


class CloudflareTunnelSetupHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "setup_cloudflare_tunnel"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(message.get("device_id") or "").strip()
        if not device_id:
            raise ValueError("device_id is required to configure a tunnel")

        timeout = message.get("timeout")
        timeout_value: Optional[int] = int(timeout) if timeout else 600

        _ensure_pyyaml()
        _reset_previous_installation()

        version = ensure_cloudflared_installed()
        cert_path = default_cert_path()

        def _handle_url(url: str) -> None:
            _emit_cloudflare_event(
                self,
                {
                    "event": "cloudflare_tunnel_login",
                    "status": "pending",
                    "login_url": url,
                    "message": "Open the login URL to authorize Cloudflare for this domain.",
                },
            )

        login_result = run_login(str(cert_path), timeout_value, on_url=_handle_url)
        if not login_result.cert_detected:
            if login_result.timed_out:
                raise RuntimeError("Cloudflare login timed out; try again.")
            raise RuntimeError("Cloudflare login did not complete.")

        domain = get_authenticated_domain(str(cert_path))
        tunnel_name = _build_tunnel_name(device_id)
        # Prefer a system config path when we're provisioning a long-running service
        # across both systemd and OpenRC distros. Privileged writes are handled by
        # the service_install helpers.
        config_path = Path("/etc/cloudflared/config.yml")
        tunnel_info = _ensure_tunnel_with_credentials(tunnel_name, config_path=config_path)

        state = update_state(
            {
                "connected": True,
                "domain": domain,
                "tunnel_name": tunnel_name,
                "tunnel_id": tunnel_info.tunnel_id,
                "tunnel_existed": tunnel_info.existed,
                "credentials_file": str(
                    system_credentials_path_for_tunnel(tunnel_info.tunnel_id)
                ),
                "token_file": str(SYSTEM_TOKEN_PATH),
                "config_path": str(config_path),
                "cert_path": str(cert_path),
                "cloudflared_version": version,
                "service_installed": True,
            }
        )

        return {
            "event": "cloudflare_tunnel_configured",
            "success": True,
            "message": f"Cloudflare tunnel ready for {domain}.",
            "cloudflare_tunnel": state,
        }
