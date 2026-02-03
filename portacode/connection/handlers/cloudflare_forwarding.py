"""Handler for managing ingress rules for a Cloudflare named tunnel."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .base import SyncHandler
from .proxmox_infra import (
    _call_subprocess,
    _connect_proxmox,
    _DeviceLookupError,
    _ensure_infra_configured,
    _get_node_from_config,
    _resolve_vmid_for_device,
    _resolve_vmid_for_device_in_proxmox,
)
from portacode.tunneling.forwarding_state import load_forwarding_state, persist_forwarding_state
from portacode.tunneling.state import default_config_path, load_state

logger = logging.getLogger(__name__)

DNSMASQ_LEASES_PATH = Path("/var/lib/misc/portacode_dnsmasq.leases")
DEVICE_DEST_PATTERN = re.compile(
    r"^(https?)://\[(?P<device_id>\d+)\](?::(?P<port>\d+))?(?P<path>/.*)?$",
    re.IGNORECASE,
)

def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _validate_hostname(hostname: str, domain: str) -> str:
    normalized = (hostname or "").strip().lower()
    if not normalized:
        raise ValueError("hostname is required for each rule")
    domain = domain.lower().strip()
    if normalized == domain or normalized.endswith(f".{domain}"):
        return normalized
    raise ValueError(f"{hostname!r} is not a subdomain of {domain!r}")


def _parse_destination(destination: str) -> Dict[str, Any]:
    value = (destination or "").strip()
    if not value:
        raise ValueError("destination is required for each rule")
    device_match = DEVICE_DEST_PATTERN.match(value)
    if device_match:
        scheme = device_match.group(1).lower()
        device_id = device_match.group("device_id")
        port = device_match.group("port")
        path = device_match.group("path") or ""
        return {
            "type": "device",
            "scheme": scheme,
            "device_id": device_id,
            "port": int(port) if port else (80 if scheme == "http" else 443),
            "path": path,
        }
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("destination must be a valid http:// or https:// URL")
    return {
        "type": "url",
        "service_url": value,
        "path": parsed.path or "",
    }


def _normalize_rules(
    rules: List[Dict[str, Any]], domain: str, *, from_storage: bool = False
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for entry in rules or []:
        if not isinstance(entry, dict):
            raise ValueError("Each rule must be an object with hostname and destination")
        hostname = entry.get("hostname") or entry.get("host") or entry.get("domain")
        parsed = entry.get("parsed") if from_storage else None
        destination = entry.get("destination") or entry.get("service")
        validated_hostname = _validate_hostname(hostname or "", domain)
        if parsed is None:
            parsed = _parse_destination(destination or "")
        normalized.append(
            {
                "hostname": validated_hostname,
                "destination": destination or "",
                "parsed": parsed,
            }
        )
    return normalized


def _parse_net_entry(entry: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for part in entry.split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            parsed[key.strip()] = val.strip()
    return parsed


def _load_leases() -> List[Dict[str, str]]:
    if not DNSMASQ_LEASES_PATH.exists():
        raise RuntimeError(f"{DNSMASQ_LEASES_PATH} does not exist; run dnsmasq first")
    leases: List[Dict[str, str]] = []
    for line in DNSMASQ_LEASES_PATH.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        leases.append(
            {
                "mac": parts[1].lower(),
                "ip": parts[2],
                "hostname": parts[3].lower() if len(parts) > 3 else "",
            }
        )
    return leases


def _lookup_lease_ip(
    leases: List[Dict[str, str]], *, mac: Optional[str] = None, hostname: Optional[str] = None
) -> Optional[str]:
    if mac:
        target_mac = mac.lower()
        for entry in leases:
            if entry["mac"] == target_mac:
                return entry["ip"]
    if hostname:
        target_host = hostname.lower()
        for entry in leases:
            if entry["hostname"] == target_host:
                return entry["ip"]
    return None


def _resolve_device_vmid(device_id: str, proxmox: Any, node: str) -> int:
    try:
        return _resolve_vmid_for_device(device_id)
    except _DeviceLookupError:
        return _resolve_vmid_for_device_in_proxmox(proxmox, node, device_id)


def _find_container_ip(
    proxmox: Any,
    node: str,
    vmid: int,
    leases: List[Dict[str, str]],
) -> str:
    cfg = (
        proxmox.nodes(node).lxc(str(vmid)).config.get()
        or {}
    )
    net0 = cfg.get("net0") or ""
    net_props = _parse_net_entry(net0)
    mac = net_props.get("hwaddr")
    hostname = (cfg.get("hostname") or "").lower()
    ip = _lookup_lease_ip(leases, mac=mac, hostname=hostname)
    if not ip:
        raise RuntimeError(
            f"Unable to find DHCP lease for container {vmid} (mac={mac or 'unknown'}, hostname={hostname})"
        )
    return ip


def _resolve_service_endpoint(
    parsed: Dict[str, Any],
    proxmox: Optional[Any],
    node: Optional[str],
    leases: List[Dict[str, str]],
    cache: Dict[str, str],
) -> str:
    if parsed["type"] == "url":
        return parsed["service_url"]
    if proxmox is None or node is None:
        raise RuntimeError("Proxmox infrastructure is required to resolve <device_id> destinations.")
    device_id = parsed["device_id"]
    if device_id in cache:
        ip = cache[device_id]
    else:
        vmid = _resolve_device_vmid(device_id, proxmox, node)
        ip = _find_container_ip(proxmox, node, vmid, leases)
        cache[device_id] = ip
    port = parsed["port"]
    scheme = parsed["scheme"]
    return f"{scheme}://{ip}:{port}"


def _build_ingress_entries(
    rules: List[Dict[str, Any]], proxmox: Optional[Any], node: Optional[str]
) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    leases = _load_leases() if proxmox else []
    device_ip_cache: Dict[str, str] = {}
    for rule in rules:
        parsed = rule["parsed"]
        service = _resolve_service_endpoint(parsed, proxmox, node, leases, device_ip_cache)
        entry: Dict[str, str] = {"hostname": rule["hostname"], "service": service}
        path = parsed.get("path", "")
        if path:
            entry["path"] = path
        entries.append(entry)
    return entries


def _write_cloudflared_config(state: Dict[str, Any], entries: List[Dict[str, str]]) -> None:
    config_path = Path(default_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    credentials = state.get("credentials_file") or ""
    if not credentials:
        raise RuntimeError("Cloudflare credentials file unknown; re-run tunnel setup")
    tunnel_id = state.get("tunnel_id")
    if not tunnel_id:
        raise RuntimeError("Cloudflare tunnel ID missing; re-run tunnel setup")
    lines = [
        f"tunnel: {tunnel_id}",
        f"credentials-file: {credentials}",
        "ingress:",
    ]
    for entry in entries:
        if "hostname" in entry:
            lines.append(f"  - hostname: {entry['hostname']}")
            if "path" in entry:
                lines.append(f"    path: {entry['path']}")
            lines.append(f"    service: {entry['service']}")
        else:
            lines.append(f"  - service: {entry['service']}")
    lines.append("  - service: http_status:404")
    lines.append("")  # trailing newline
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _route_dns(hostnames: List[str], tunnel_name: str) -> None:
    seen = set()
    for hostname in hostnames:
        if hostname in seen:
            continue
        seen.add(hostname)
        _call_subprocess(
            ["cloudflared", "tunnel", "route", "dns", tunnel_name, hostname],
            check=True,
        )


def _reload_cloudflared_service() -> None:
    reload_cmd = ["/bin/systemctl", "reload", "cloudflared"]
    result = _call_subprocess(reload_cmd, check=False)
    if result.returncode != 0:
        _call_subprocess(["/bin/systemctl", "restart", "cloudflared"], check=True)


class CloudflareForwardingHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "configure_cloudflare_forwarding"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not _is_root():
            raise RuntimeError("Root privileges are required to manage cloudflared config.")

        tunnel_state = load_state()
        if not tunnel_state.get("configured"):
            raise RuntimeError("Cloudflare tunnel is not configured yet.")
        domain = tunnel_state.get("domain")
        tunnel_name = tunnel_state.get("tunnel_name")
        if not domain or not tunnel_name:
            raise RuntimeError("Cloudflare domain or tunnel name missing from state.")

        device_id = str(message.get("device_id") or "").strip()
        if not device_id:
            raise ValueError("device_id is required to configure forwarding rules")

        user_rules = message.get("rules")
        if user_rules is None:
            stored = load_forwarding_state().get("rules", [])
            rules = _normalize_rules(stored, domain, from_storage=True)
        else:
            rules = _normalize_rules(user_rules, domain)
        state = persist_forwarding_state(rules)

        requires_proxmox = any(rule["parsed"]["type"] == "device" for rule in rules)
        proxmox = node = None
        infra_config = None
        if requires_proxmox:
            infra_config = _ensure_infra_configured()
            proxmox = _connect_proxmox(infra_config)
            node = _get_node_from_config(infra_config)
        entries = _build_ingress_entries(rules, proxmox, node)
        _write_cloudflared_config(tunnel_state, entries)
        hostnames = [entry["hostname"] for entry in entries if entry.get("hostname")]
        if hostnames:
            _route_dns(hostnames, tunnel_name)
        _reload_cloudflared_service()

        sanitized_rules = [
            {"hostname": rule["hostname"], "destination": rule["destination"]}
            for rule in rules
        ]

        return {
            "event": "cloudflare_forwarding_configured",
            "success": True,
            "message": f"Cloudflare ingress configured for {len(rules)} rule(s).",
            "rules": sanitized_rules,
            "updated_at": state.get("updated_at"),
            "device_id": device_id,
        }
