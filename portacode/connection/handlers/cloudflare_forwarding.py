"""Handler for managing ingress rules for a Cloudflare named tunnel."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import tempfile
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
    _run_pct_exec,
    _run_pct_exec_check,
    _run_pct_push,
    _resolve_vmid_for_device,
    _resolve_vmid_for_device_in_proxmox,
)
from portacode.tunneling.forwarding_state import load_forwarding_state, persist_forwarding_state
from portacode.tunneling.state import default_config_path, load_state
from portacode.tunneling.privileged import write_text
from portacode.tunneling.service_install import restart_service

logger = logging.getLogger(__name__)

DNSMASQ_LEASES_PATH = Path("/var/lib/misc/portacode_dnsmasq.leases")
DEVICE_DEST_PATTERN = re.compile(
    r"^(https?)://\[(?P<device_id>\d+)\](?::(?P<port>\d+))?(?P<path>/.*)?$",
    re.IGNORECASE,
)
CONTAINER_SUBDOMAIN_RE = re.compile(r"^(?:(?P<index>\d+)_)?(?P<device_id>\d+)$")
_FORWARDING_UPDATE_LOCK = threading.RLock()
EXPOSED_SERVICES_JSON_PATH = "/etc/portacode/exposed_services.json"
EXPOSED_SERVICES_ENV_PATH = "/etc/portacode/exposed_services.env"
EXPOSED_SERVICES_PROFILE_PATH = "/etc/profile.d/portacode_exposed_services.sh"
SYSTEM_ENV_PATH = "/etc/environment"
SYSTEM_ENV_D_PATH = "/etc/environment.d/90-portacode-exposed-services.conf"
DEFAULT_ENV_PATH = "/etc/default/portacode_exposed_services"
SYSTEMD_MANAGER_DROPIN_PATH = "/etc/systemd/system.conf.d/90-portacode-exposed-services.conf"
OPENRC_ENV_PATH = "/etc/conf.d/portacode_exposed_services"
GLOBAL_SHELL_HOOK_PATHS = (
    "/etc/profile",
    "/etc/bash.bashrc",
    "/etc/bash/bashrc",
    "/etc/zsh/zshenv",
)
MANAGED_BLOCK_BEGIN = "# >>> PORTACODE_EXPOSED_SERVICES >>>"
MANAGED_BLOCK_END = "# <<< PORTACODE_EXPOSED_SERVICES <<<"

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
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    leases = _load_leases() if proxmox else []
    device_ip_cache: Dict[str, str] = {}
    for rule in rules:
        parsed = rule["parsed"]
        service = _resolve_service_endpoint(parsed, proxmox, node, leases, device_ip_cache)
        entry: Dict[str, Any] = {"hostname": rule["hostname"], "service": service}
        path = parsed.get("path", "")
        if path:
            entry["path"] = path
        parsed_service = urlparse(service)
        host = parsed_service.hostname
        if parsed_service.scheme == "https" and host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                entry["originRequest"] = {"noTLSVerify": True}
        entries.append(entry)
    return entries


def _format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_cloudflared_config(state: Dict[str, Any], entries: List[Dict[str, Any]]) -> None:
    cfg = state.get("config_path")
    config_path = Path(str(cfg)) if cfg else Path(default_config_path())
    credentials = str(state.get("credentials_file") or "").strip()
    token_file = str(state.get("token_file") or "").strip()
    tunnel_id = state.get("tunnel_id")
    if not tunnel_id:
        raise RuntimeError("Cloudflare tunnel ID missing; re-run tunnel setup")
    have_creds = False
    if credentials:
        try:
            have_creds = Path(credentials).exists()
        except OSError:
            have_creds = False
    have_token = False
    if token_file:
        try:
            have_token = Path(token_file).exists()
        except OSError:
            have_token = False
    # We can run the tunnel with either credentials JSON or a token (via wrapper).
    if not have_creds and not have_token:
        raise RuntimeError("Cloudflare credentials/token missing; re-run tunnel setup")

    lines = [f"tunnel: {tunnel_id}"]
    if have_creds:
        lines.append(f"credentials-file: {credentials}")
    lines.append("ingress:")
    for entry in entries:
        if "hostname" in entry:
            lines.append(f"  - hostname: {entry['hostname']}")
            if "path" in entry:
                lines.append(f"    path: {entry['path']}")
            lines.append(f"    service: {entry['service']}")
            origin_request = entry.get("originRequest")
            if origin_request:
                lines.append("    originRequest:")
                for key, value in origin_request.items():
                    lines.append(f"      {key}: {_format_config_value(value)}")
        else:
            lines.append(f"  - service: {entry['service']}")
    lines.append("  - service: http_status:404")
    lines.append("")  # trailing newline
    write_text(config_path, "\n".join(lines), mode=0o644)


def _route_dns(hostnames: List[str], tunnel_name: str) -> None:
    seen = set()
    for hostname in hostnames:
        if hostname in seen:
            continue
        seen.add(hostname)
        try:
            _call_subprocess(
                ["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", tunnel_name, hostname],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (getattr(exc, "stderr", "") or "").strip()
            stdout = (getattr(exc, "stdout", "") or "").strip()
            details = stderr or stdout or f"exit={exc.returncode}"
            # Common case: record already exists (A/AAAA/CNAME). Give the user the exact output.
            raise RuntimeError(
                f"cloudflared route dns failed for {hostname!r} (tunnel={tunnel_name!r}): {details}"
            ) from exc


def _reload_cloudflared_service() -> None:
    # Restart via init-system aware logic; sudo/root handled inside.
    restart_service()


def _load_tunnel_state() -> Dict[str, Any]:
    tunnel_state = load_state()
    if not tunnel_state.get("configured"):
        raise RuntimeError("Cloudflare tunnel is not configured yet.")
    domain = str(tunnel_state.get("domain") or "").strip().lower().rstrip(".")
    tunnel_name = tunnel_state.get("tunnel_name")
    if not domain or not tunnel_name:
        raise RuntimeError("Cloudflare domain or tunnel name missing from state.")
    return tunnel_state


def _sanitize_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [
        {"hostname": rule["hostname"], "destination": rule["destination"]}
        for rule in rules
    ]


def _apply_and_persist_forwarding_rules(
    rules: List[Dict[str, Any]],
    *,
    tunnel_state: Dict[str, Any],
) -> Dict[str, Any]:
    domain = str(tunnel_state.get("domain") or "").strip().lower().rstrip(".")
    tunnel_name = tunnel_state.get("tunnel_name")
    if not domain or not tunnel_name:
        raise RuntimeError("Cloudflare domain or tunnel name missing from state.")

    requires_proxmox = any(rule["parsed"]["type"] == "device" for rule in rules)
    proxmox = node = None
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
    state = persist_forwarding_state(rules)
    return {
        "rules": _sanitize_rules(rules),
        "updated_at": state.get("updated_at"),
    }


def _normalize_subdomain_label(subdomain: Any, device_id: str, index: int) -> str:
    candidate = str(subdomain or "").strip().lower().rstrip(".")
    if not candidate:
        candidate = device_id if index == 0 else f"{index}_{device_id}"
    if "." in candidate:
        raise ValueError("subdomain must be a single hostname label")
    if not CONTAINER_SUBDOMAIN_RE.match(candidate):
        raise ValueError("subdomain must follow '<device_id>' or '<index>_<device_id>' format")
    return candidate


def _normalize_container_rule_specs(
    container_device_id: Any,
    container_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    device_id = str(container_device_id or "").strip()
    if not device_id or not device_id.isdigit():
        raise ValueError("container device_id must be a numeric value")
    if not isinstance(container_rules, list):
        raise ValueError("rules must be a list")

    normalized: List[Dict[str, Any]] = []
    for index, rule in enumerate(container_rules):
        if not isinstance(rule, dict):
            raise ValueError("each container rule must be an object")
        label = _normalize_subdomain_label(rule.get("subdomain"), device_id, index)
        raw_port = rule.get("port")
        try:
            port = int(str(raw_port).strip())
        except (TypeError, ValueError):
            raise ValueError("each container rule requires a valid integer port") from None
        if port < 1 or port > 65535:
            raise ValueError("container rule ports must be between 1 and 65535")
        normalized.append({"subdomain": label, "port": port})
    return normalized


def _push_root_file_to_container(vmid: int, path: str, data: bytes, mode: int = 0o644) -> None:
    tmp_path: Optional[str] = None
    try:
        parent = str(Path(path).parent)
        if parent not in {"", ".", "/"}:
            _run_pct_exec_check(vmid, ["mkdir", "-p", parent])
            _run_pct_exec_check(vmid, ["chown", "root:root", parent])
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        res = _run_pct_push(vmid, tmp_path, path)
        if res.returncode != 0:
            raise RuntimeError(res.stderr or res.stdout or f"pct push returned {res.returncode}")
        _run_pct_exec_check(vmid, ["chown", "root:root", path])
        _run_pct_exec_check(vmid, ["chmod", format(mode, "o"), path])
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _shell_quote_single(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _build_exposed_services_env(exposed_ports: List[Dict[str, Any]]) -> str:
    hosts = [str(item.get("hostname") or "").strip() for item in exposed_ports if item.get("hostname")]
    urls = [str(item.get("url") or "").strip() for item in exposed_ports if item.get("url")]
    ports = [str(item.get("port")) for item in exposed_ports if item.get("port") is not None]
    primary_url = urls[0] if urls else ""
    primary_host = hosts[0] if hosts else ""
    payload_json = json.dumps(exposed_ports, separators=(",", ":"))

    lines = [
        "# Managed by portacode: do not edit manually.",
        f"PORTACODE_EXPOSED_SERVICES_JSON={_shell_quote_single(payload_json)}",
        f"PORTACODE_EXPOSED_PORTS={_shell_quote_single(','.join(ports))}",
        f"PORTACODE_PUBLIC_HOSTS={_shell_quote_single(','.join(hosts))}",
        f"PORTACODE_PUBLIC_URLS={_shell_quote_single(','.join(urls))}",
        f"PORTACODE_PRIMARY_PUBLIC_URL={_shell_quote_single(primary_url)}",
        f"PORTACODE_PRIMARY_PUBLIC_HOST={_shell_quote_single(primary_host)}",
        "",
    ]
    return "\n".join(lines)


def _build_exposed_services_env_map(exposed_ports: List[Dict[str, Any]]) -> Dict[str, str]:
    hosts = [str(item.get("hostname") or "").strip() for item in exposed_ports if item.get("hostname")]
    urls = [str(item.get("url") or "").strip() for item in exposed_ports if item.get("url")]
    ports = [str(item.get("port")) for item in exposed_ports if item.get("port") is not None]
    primary_url = urls[0] if urls else ""
    primary_host = hosts[0] if hosts else ""
    payload_json = json.dumps(exposed_ports, separators=(",", ":"))
    return {
        "PORTACODE_EXPOSED_SERVICES_JSON": payload_json,
        "PORTACODE_EXPOSED_PORTS": ",".join(ports),
        "PORTACODE_PUBLIC_HOSTS": ",".join(hosts),
        "PORTACODE_PUBLIC_URLS": ",".join(urls),
        "PORTACODE_PRIMARY_PUBLIC_URL": primary_url,
        "PORTACODE_PRIMARY_PUBLIC_HOST": primary_host,
    }


def _format_etc_environment_value(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _merge_system_environment(existing_text: str, env_map: Dict[str, str]) -> str:
    kept_lines: List[str] = []
    managed_keys = set(env_map.keys())
    for raw_line in (existing_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            kept_lines.append(raw_line)
            continue
        if stripped.startswith("#"):
            kept_lines.append(raw_line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in managed_keys:
            continue
        kept_lines.append(raw_line)

    for key in sorted(managed_keys):
        kept_lines.append(f"{key}={_format_etc_environment_value(env_map[key])}")

    merged = "\n".join(kept_lines).rstrip() + "\n"
    return merged


def _build_exposed_services_profile_script() -> str:
    return (
        "#!/bin/sh\n"
        "# Managed by portacode: loads exposure env vars for new shell sessions.\n"
        f'if [ -f "{EXPOSED_SERVICES_ENV_PATH}" ]; then\n'
        "  set -a\n"
        f'  . "{EXPOSED_SERVICES_ENV_PATH}"\n'
        "  set +a\n"
        "fi\n"
    )


def _build_shell_hook_block() -> str:
    return (
        f"{MANAGED_BLOCK_BEGIN}\n"
        "# Managed by portacode: loads exposure env vars for shell sessions.\n"
        f'if [ -f "{EXPOSED_SERVICES_ENV_PATH}" ]; then\n'
        "  set -a\n"
        f'  . "{EXPOSED_SERVICES_ENV_PATH}"\n'
        "  set +a\n"
        "fi\n"
        f"{MANAGED_BLOCK_END}\n"
    )


def _strip_managed_block(text: str) -> str:
    source = text or ""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(MANAGED_BLOCK_BEGIN)}\n.*?^\s*{re.escape(MANAGED_BLOCK_END)}\n?"
    )
    cleaned = re.sub(pattern, "", source)
    return cleaned.rstrip() + ("\n" if cleaned.strip() else "")


def _upsert_managed_shell_hook(vmid: int, path: str, mode: int = 0o644) -> None:
    current = _run_pct_exec(vmid, ["cat", path])
    existing_text = current.stdout if current.returncode == 0 else ""
    merged = _strip_managed_block(existing_text) + _build_shell_hook_block()
    _push_root_file_to_container(vmid, path, merged.encode("utf-8"), mode=mode)


def _build_environmentd_content(env_map: Dict[str, str]) -> str:
    lines = ["# Managed by portacode: exposed services environment."]
    for key in sorted(env_map.keys()):
        lines.append(f"{key}={_format_etc_environment_value(env_map[key])}")
    lines.append("")
    return "\n".join(lines)


def _build_default_env_content(env_map: Dict[str, str]) -> str:
    lines = ["# Managed by portacode: exposed services environment."]
    for key in sorted(env_map.keys()):
        lines.append(f"{key}={_shell_quote_single(env_map[key])}")
    lines.append("")
    return "\n".join(lines)


def _format_systemd_default_environment(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _build_systemd_manager_dropin(env_map: Dict[str, str]) -> str:
    lines = [
        "# Managed by portacode: exposed services environment.",
        "[Manager]",
    ]
    for key in sorted(env_map.keys()):
        assignment = f"{key}={_format_systemd_default_environment(env_map[key])}"
        lines.append(f'DefaultEnvironment="{assignment}"')
    lines.append("")
    return "\n".join(lines)


def _build_openrc_env_content(env_map: Dict[str, str]) -> str:
    lines = [
        "# Managed by portacode: exposed services environment.",
        "# Sourced by OpenRC scripts that include /etc/conf.d/* shell assignments.",
    ]
    for key in sorted(env_map.keys()):
        lines.append(f"{key}={_shell_quote_single(env_map[key])}")
    lines.append("")
    return "\n".join(lines)


def _best_effort_refresh_service_env(vmid: int) -> None:
    # Refresh managers if they exist so newly activated units/services can pick up updates.
    _run_pct_exec(
        vmid,
        [
            "sh",
            "-lc",
            "if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reexec >/dev/null 2>&1 || true; fi",
        ],
    )
    _run_pct_exec(
        vmid,
        [
            "sh",
            "-lc",
            "if command -v env-update >/dev/null 2>&1; then env-update >/dev/null 2>&1 || true; fi",
        ],
    )


def _sync_exposed_services_into_container(
    container_device_id: str,
    exposed_ports: List[Dict[str, Any]],
    proxmox: Any,
    node: str,
) -> None:
    vmid = _resolve_device_vmid(container_device_id, proxmox, node)
    services_payload = {
        "device_id": container_device_id,
        "exposed_services": exposed_ports,
    }
    json_data = (json.dumps(services_payload, indent=2) + "\n").encode("utf-8")
    env_map = _build_exposed_services_env_map(exposed_ports)
    env_data = _build_exposed_services_env(exposed_ports).encode("utf-8")
    profile_data = _build_exposed_services_profile_script().encode("utf-8")
    envd_data = _build_environmentd_content(env_map).encode("utf-8")
    default_env_data = _build_default_env_content(env_map).encode("utf-8")
    systemd_dropin_data = _build_systemd_manager_dropin(env_map).encode("utf-8")
    openrc_env_data = _build_openrc_env_content(env_map).encode("utf-8")

    _push_root_file_to_container(vmid, EXPOSED_SERVICES_JSON_PATH, json_data, mode=0o644)
    _push_root_file_to_container(vmid, EXPOSED_SERVICES_ENV_PATH, env_data, mode=0o644)
    _push_root_file_to_container(vmid, EXPOSED_SERVICES_PROFILE_PATH, profile_data, mode=0o755)
    for hook_path in GLOBAL_SHELL_HOOK_PATHS:
        _upsert_managed_shell_hook(vmid, hook_path, mode=0o644)

    current_env = _run_pct_exec(vmid, ["cat", SYSTEM_ENV_PATH])
    existing_text = current_env.stdout if current_env.returncode == 0 else ""
    merged_environment = _merge_system_environment(existing_text, env_map)
    _push_root_file_to_container(vmid, SYSTEM_ENV_PATH, merged_environment.encode("utf-8"), mode=0o644)
    _push_root_file_to_container(vmid, SYSTEM_ENV_D_PATH, envd_data, mode=0o644)
    _push_root_file_to_container(vmid, DEFAULT_ENV_PATH, default_env_data, mode=0o644)
    _push_root_file_to_container(vmid, SYSTEMD_MANAGER_DROPIN_PATH, systemd_dropin_data, mode=0o644)
    _push_root_file_to_container(vmid, OPENRC_ENV_PATH, openrc_env_data, mode=0o644)
    _best_effort_refresh_service_env(vmid)


def _rule_targets_container(rule: Dict[str, Any], container_device_id: str, domain: str) -> bool:
    parsed = rule.get("parsed")
    if isinstance(parsed, dict) and parsed.get("type") == "device":
        if str(parsed.get("device_id") or "").strip() == container_device_id:
            return True

    hostname = str(rule.get("hostname") or "").strip().lower().rstrip(".")
    if not hostname:
        return False
    suffix = f".{domain}"
    if not hostname.endswith(suffix):
        return False
    label = hostname[: -len(suffix)]
    match = CONTAINER_SUBDOMAIN_RE.match(label)
    if not match:
        return False
    return str(match.group("device_id")) == container_device_id


def set_container_forwarding_rules(
    container_device_id: Any,
    container_rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    tunnel_state = _load_tunnel_state()
    domain = str(tunnel_state.get("domain") or "").strip().lower().rstrip(".")
    normalized_specs = _normalize_container_rule_specs(container_device_id, container_rules)
    normalized_device_id = str(container_device_id).strip()

    with _FORWARDING_UPDATE_LOCK:
        stored = load_forwarding_state().get("rules", [])
        existing_rules = _normalize_rules(stored, domain, from_storage=True)
        preserved_rules = [
            rule
            for rule in existing_rules
            if not _rule_targets_container(rule, normalized_device_id, domain)
        ]
        new_rules: List[Dict[str, Any]] = []
        exposed_ports: List[Dict[str, Any]] = []
        for spec in normalized_specs:
            hostname = f"{spec['subdomain']}.{domain}"
            destination = f"http://[{normalized_device_id}]:{spec['port']}"
            new_rules.append(
                {
                    "hostname": hostname,
                    "destination": destination,
                    "parsed": _parse_destination(destination),
                }
            )
            exposed_ports.append(
                {
                    "port": spec["port"],
                    "hostname": hostname,
                    "url": f"https://{hostname}",
                }
            )

        merged_rules = preserved_rules + new_rules

        # Ensure the child container receives updated exposure metadata first.
        infra_config = _ensure_infra_configured()
        proxmox = _connect_proxmox(infra_config)
        node = _get_node_from_config(infra_config)
        try:
            _sync_exposed_services_into_container(
                normalized_device_id,
                exposed_ports,
                proxmox,
                node,
            )
        except Exception:
            if exposed_ports:
                raise
            logger.warning(
                "Failed to sync empty exposure metadata into container %s; continuing with forwarding cleanup",
                normalized_device_id,
                exc_info=True,
            )

        persisted = _apply_and_persist_forwarding_rules(merged_rules, tunnel_state=tunnel_state)

    return {
        "device_id": normalized_device_id,
        "domain": domain,
        "rules": persisted["rules"],
        "updated_at": persisted["updated_at"],
        "exposed_ports": exposed_ports,
    }


class CloudflareForwardingHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "configure_cloudflare_forwarding"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        tunnel_state = _load_tunnel_state()
        domain = str(tunnel_state.get("domain") or "").strip().lower().rstrip(".")

        device_id = str(message.get("device_id") or "").strip()
        if not device_id:
            raise ValueError("device_id is required to configure forwarding rules")

        user_rules = message.get("rules")
        if user_rules is None:
            stored = load_forwarding_state().get("rules", [])
            rules = _normalize_rules(stored, domain, from_storage=True)
        else:
            rules = _normalize_rules(user_rules, domain)

        with _FORWARDING_UPDATE_LOCK:
            persisted = _apply_and_persist_forwarding_rules(rules, tunnel_state=tunnel_state)

        return {
            "event": "cloudflare_forwarding_configured",
            "success": True,
            "message": f"Cloudflare ingress configured for {len(rules)} rule(s).",
            "rules": persisted["rules"],
            "updated_at": persisted["updated_at"],
            "device_id": device_id,
        }


class ConfigureProxmoxContainerExposePortsHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "configure_proxmox_container_expose_ports"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        child_device_id = str(message.get("child_device_id") or "").strip()
        if not child_device_id or not child_device_id.isdigit():
            raise ValueError("child_device_id is required")

        raw_ports = message.get("expose_ports")
        if raw_ports is None:
            raw_ports = []
        if not isinstance(raw_ports, list):
            raise ValueError("expose_ports must be a list of integers")

        seen: set[int] = set()
        normalized_ports: List[int] = []
        for raw in raw_ports:
            try:
                port = int(str(raw).strip())
            except (TypeError, ValueError):
                raise ValueError("expose_ports must contain valid integers") from None
            if port < 1 or port > 65535:
                raise ValueError("expose_ports entries must be between 1 and 65535")
            if port in seen:
                continue
            seen.add(port)
            normalized_ports.append(port)
        if len(normalized_ports) > 3:
            raise ValueError("A maximum of 3 ports can be exposed")

        desired_rules = []
        for index, port in enumerate(normalized_ports):
            if index == 0:
                subdomain = child_device_id
            else:
                subdomain = f"{index}_{child_device_id}"
            desired_rules.append({"subdomain": subdomain, "port": port})

        updated = set_container_forwarding_rules(child_device_id, desired_rules)
        return {
            "event": "proxmox_container_expose_ports_configured",
            "success": True,
            "message": f"Applied {len(updated['exposed_ports'])} expose-port rule(s) for device {child_device_id}.",
            "child_device_id": child_device_id,
            "rules": updated["rules"],
            "updated_at": updated["updated_at"],
            "exposed_ports": updated["exposed_ports"],
        }
