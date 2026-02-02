"""Cloudflare tunnel setup handler for Proxmox infrastructure nodes."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional

from .base import SyncHandler
from portacode.tunneling.ensure_cloudflared import ensure_cloudflared_installed
from portacode.tunneling.cloudflared_login import run_login
from portacode.tunneling.get_domain import get_authenticated_domain
from portacode.tunneling.service_install import ensure_tunnel_and_service
from portacode.tunneling.state import (
    credentials_path_for_tunnel,
    default_cert_path,
    default_config_path,
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
    try:
        import yaml  # noqa: F401
    except ModuleNotFoundError as exc:
        logger.info("PyYAML missing; installing via pip")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "PyYAML"],
                check=True,
                text=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as pip_exc:
            msg = pip_exc.stderr or pip_exc.stdout or str(pip_exc)
            raise RuntimeError(f"Failed to install PyYAML: {msg}") from pip_exc
        try:
            import yaml  # noqa: F401
        except ModuleNotFoundError as post_exc:
            raise RuntimeError("PyYAML installation did not resolve the import.") from post_exc


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _build_tunnel_name(device_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9-]+", "-", device_id.strip()).strip("-")
    normalized = normalized.lower() or "device"
    return f"portacode-proxmox-{normalized}"


class CloudflareTunnelSetupHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "setup_cloudflare_tunnel"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not _is_root():
            raise RuntimeError("Root privileges are required to configure cloudflared.")

        device_id = str(message.get("device_id") or "").strip()
        if not device_id:
            raise ValueError("device_id is required to configure a tunnel")

        timeout = message.get("timeout")
        timeout_value: Optional[int] = int(timeout) if timeout else 600

        _ensure_pyyaml()

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
        tunnel_info = ensure_tunnel_and_service(tunnel_name, config_path=default_config_path())

        state = update_state(
            {
                "connected": True,
                "domain": domain,
                "tunnel_name": tunnel_name,
                "tunnel_id": tunnel_info.tunnel_id,
                "tunnel_existed": tunnel_info.existed,
                "credentials_file": str(credentials_path_for_tunnel(tunnel_info.tunnel_id)),
                "config_path": str(default_config_path()),
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
