from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from portacode.tunneling.privileged import run_checked, write_text


WEBMIN_CONFIG_PATH = Path("/etc/webmin/config")
WEBMIN_MINISERV_PATH = Path("/etc/webmin/miniserv.conf")
WEBMIN_DIR = Path("/etc/webmin")
WEBMIN_RESTART_CMD = ["/etc/webmin/restart"]


def _normalize_line_value(value: Any) -> str:
    return str(value or "").strip()


def _replace_or_append_setting(text: str, key: str, value: str) -> str:
    value = _normalize_line_value(value)
    lines = (text or "").splitlines()
    replaced = False
    output: list[str] = []

    for raw_line in lines:
        if raw_line.startswith(f"{key}="):
            if not replaced:
                output.append(f"{key}={value}")
                replaced = True
            continue
        output.append(raw_line)

    if not replaced:
        output.append(f"{key}={value}")

    return "\n".join(output).rstrip() + "\n"


def resolve_webmin_public_host(exposed_services: list[dict[str, Any]]) -> Optional[str]:
    for item in exposed_services or []:
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError):
            continue
        if port != 12321:
            continue
        host = _normalize_line_value(item.get("hostname"))
        if host:
            return host
    return None


def apply_turnkey_webmin_proxy_config(exposed_services: list[dict[str, Any]]) -> bool:
    """
    Auto-configure Webmin when the SDK detects a public HTTPS mapping for port 12321.

    Returns True if a restart was performed, False otherwise.
    """
    public_host = resolve_webmin_public_host(exposed_services)
    if not public_host:
        return False
    if not WEBMIN_DIR.exists():
        return False
    if not WEBMIN_CONFIG_PATH.exists() or not WEBMIN_MINISERV_PATH.exists():
        return False

    config_before = WEBMIN_CONFIG_PATH.read_text(encoding="utf-8")
    miniserv_before = WEBMIN_MINISERV_PATH.read_text(encoding="utf-8")

    config_after = _replace_or_append_setting(config_before, "referers", public_host)
    miniserv_after = _replace_or_append_setting(miniserv_before, "redirect_host", public_host)

    changed = False
    if config_after != config_before:
        write_text(WEBMIN_CONFIG_PATH, config_after, mode=0o600)
        changed = True
    if miniserv_after != miniserv_before:
        write_text(WEBMIN_MINISERV_PATH, miniserv_after, mode=0o600)
        changed = True

    if not changed:
        return False

    run_checked(WEBMIN_RESTART_CMD)
    return True

