"""Proxmox infrastructure configuration handler."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import platformdirs

from .base import SyncHandler

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(platformdirs.user_config_dir("portacode"))
CONFIG_PATH = CONFIG_DIR / "proxmox_infra.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
NET_SETUP_SCRIPT = REPO_ROOT / "proxmox_management" / "net_setup.py"
CONTAINERS_DIR = CONFIG_DIR / "containers"
MANAGED_MARKER = "portacode-managed:true"

DEFAULT_HOST = "localhost"
DEFAULT_NODE_NAME = os.uname().nodename.split(".", 1)[0]
DEFAULT_BRIDGE = "vmbr1"
SUBNET_CIDR = "10.10.0.1/24"
BRIDGE_IP = SUBNET_CIDR.split("/", 1)[0]
DHCP_START = "10.10.0.100"
DHCP_END = "10.10.0.200"
DNS_SERVER = "1.1.1.1"
IFACES_PATH = Path("/etc/network/interfaces")
SYSCTL_PATH = Path("/etc/sysctl.d/99-portacode-forward.conf")
UNIT_DIR = Path("/etc/systemd/system")
_MANAGED_CONTAINERS_STATE_LOCK = threading.Lock()
_MANAGED_CONTAINERS_STATE: Dict[str, Any] = {
    "initialized": False,
    "base_summary": None,
    "initial_totals": {"ram_mib": 0, "disk_gib": 0, "cpu_share": 0.0},
    "records": {},
    "pending": {},
}
TEMPLATES_REFRESH_INTERVAL_S = 300

ProgressCallback = Callable[[int, int, Dict[str, Any], str, Optional[Dict[str, Any]]], None]


def _emit_host_event(
    handler: SyncHandler,
    payload: Dict[str, Any],
) -> None:
    loop = handler.context.get("event_loop")
    if not loop or loop.is_closed():
        logger.debug("host event skipped (no event loop) event=%s", payload.get("event"))
        return

    future = asyncio.run_coroutine_threadsafe(handler.send_response(payload), loop)
    future.add_done_callback(
        lambda fut: logger.warning(
            "Failed to emit host event %s: %s", payload.get("event"), fut.exception()
        )
        if fut.exception()
        else None
    )


def _emit_progress_event(
    handler: SyncHandler,
    *,
    step_index: int,
    total_steps: int,
    step_name: str,
    step_label: str,
    status: str,
    message: str,
    phase: str,
    request_id: Optional[str],
    details: Optional[Dict[str, Any]] = None,
    on_behalf_of_device: Optional[str] = None,
) -> None:
    loop = handler.context.get("event_loop")
    if not loop or loop.is_closed():
        logger.debug(
            "progress event skipped (no event loop) step=%s status=%s",
            step_name,
            status,
        )
        return

    payload: Dict[str, Any] = {
        "event": "proxmox_container_progress",
        "step_name": step_name,
        "step_label": step_label,
        "status": status,
        "phase": phase,
        "step_index": step_index,
        "total_steps": total_steps,
        "message": message,
    }
    if request_id:
        payload["request_id"] = request_id
    if details:
        payload["details"] = details
    if on_behalf_of_device:
        payload["on_behalf_of_device"] = str(on_behalf_of_device)

    future = asyncio.run_coroutine_threadsafe(handler.send_response(payload), loop)
    future.add_done_callback(
        lambda fut: logger.warning(
            "Failed to emit progress event for %s: %s", step_name, fut.exception()
        )
        if fut.exception()
        else None
    )


def _call_subprocess(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("DEBIAN_FRONTEND", "noninteractive")
    return subprocess.run(cmd, env=env, text=True, capture_output=True, **kwargs)


def _ensure_proxmoxer() -> Any:
    try:
        from proxmoxer import ProxmoxAPI  # noqa: F401
    except ModuleNotFoundError as exc:
        python = sys.executable
        logger.info("Proxmoxer missing; installing via pip")
        try:
            _call_subprocess([python, "-m", "pip", "install", "proxmoxer"], check=True)
        except subprocess.CalledProcessError as pip_exc:
            msg = pip_exc.stderr or pip_exc.stdout or str(pip_exc)
            raise RuntimeError(f"Failed to install proxmoxer: {msg}") from pip_exc
        from proxmoxer import ProxmoxAPI  # noqa: F401
    from proxmoxer import ProxmoxAPI
    return ProxmoxAPI


def _parse_token(token_identifier: str) -> Tuple[str, str]:
    identifier = token_identifier.strip()
    if "!" not in identifier or "@" not in identifier:
        raise ValueError("Expected API token in the form user@realm!tokenid")
    user_part, token_name = identifier.split("!", 1)
    user = user_part.strip()
    token_name = token_name.strip()
    if "@" not in user:
        raise ValueError("API token missing user realm (user@realm)")
    if not token_name:
        raise ValueError("Token identifier missing token name")
    return user, token_name


def _save_config(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, CONFIG_PATH)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse Proxmox infra config: %s", exc)
        return {}


def _pick_node(client: Any) -> str:
    nodes = client.nodes().get()
    for node in nodes:
        if node.get("node") == DEFAULT_NODE_NAME:
            return DEFAULT_NODE_NAME
    return nodes[0].get("node") if nodes else DEFAULT_NODE_NAME


def _list_templates(client: Any, node: str, storages: Iterable[Dict[str, Any]]) -> List[str]:
    templates: List[str] = []
    for storage in storages:
        storage_name = storage.get("storage")
        if not storage_name:
            continue
        try:
            items = client.nodes(node).storage(storage_name).content.get()
        except Exception:
            continue
        for item in items:
            if item.get("content") == "vztmpl" and item.get("volid"):
                templates.append(item["volid"])
    return templates


def _build_proxmox_client_from_config(config: Dict[str, Any]):
    user = config.get("user")
    token_name = config.get("token_name")
    token_value = config.get("token_value")
    if not user or not token_name or not token_value:
        raise RuntimeError("Proxmox API credentials are missing")
    ProxmoxAPI = _ensure_proxmoxer()
    return ProxmoxAPI(
        config.get("host", DEFAULT_HOST),
        user=user,
        token_name=token_name,
        token_value=token_value,
        verify_ssl=config.get("verify_ssl", False),
        timeout=30,
    )


def _current_time_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _templates_need_refresh(config: Dict[str, Any]) -> bool:
    if not config or not config.get("token_value"):
        return False
    last = _parse_iso_timestamp(config.get("templates_last_refreshed") or "")
    if not last:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= TEMPLATES_REFRESH_INTERVAL_S


def _ensure_templates_refreshed_on_startup(config: Dict[str, Any]) -> None:
    if not _templates_need_refresh(config):
        return
    try:
        client = _build_proxmox_client_from_config(config)
        node = config.get("node") or _pick_node(client)
        storages = client.nodes(node).storage.get()
        templates = _list_templates(client, node, storages)
        if templates:
            config["templates"] = templates
            config["templates_last_refreshed"] = _current_time_iso()
            _save_config(config)
    except Exception as exc:
        logger.warning("Unable to refresh Proxmox templates on startup: %s", exc)


def _pick_storage(storages: Iterable[Dict[str, Any]]) -> str:
    candidates = [s for s in storages if "rootdir" in s.get("content", "") and s.get("avail", 0) > 0]
    if not candidates:
        candidates = [s for s in storages if "rootdir" in s.get("content", "")]
    if not candidates:
        return ""
    candidates.sort(key=lambda entry: entry.get("avail", 0), reverse=True)
    return candidates[0].get("storage", "")


def _bytes_to_gib(value: Any) -> float:
    try:
        return float(value) / 1024**3
    except (TypeError, ValueError):
        return 0.0


def _bytes_to_mib(value: Any) -> float:
    try:
        return float(value) / 1024**2
    except (TypeError, ValueError):
        return 0.0


def _size_token_to_gib(token: str) -> float:
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP])?([iI]?[bB])?\s*$", token)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = (match.group(2) or "").upper()
    scale = {
        "": 1,
        "K": 1024**1,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
    }.get(unit, 1)
    return (number * scale) / 1024**3


def _extract_size_gib(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value)
    for part in text.split(","):
        if "size=" in part:
            token = part.split("=", 1)[1]
            return _size_token_to_gib(token)
    return _size_token_to_gib(text)


def _extract_storage_token(value: Any) -> str:
    if not value:
        return "unknown"
    text = str(value)
    if ":" in text:
        return text.split(":", 1)[0].strip() or "unknown"
    return text.strip() or "unknown"


def _storage_from_lxc(cfg: Dict[str, Any], entry: Dict[str, Any]) -> str:
    rootfs = cfg.get("rootfs") or entry.get("rootfs")
    storage = _extract_storage_token(rootfs)
    if storage != "unknown":
        return storage
    for idx in range(0, 10):
        mp_value = cfg.get(f"mp{idx}")
        storage = _extract_storage_token(mp_value)
        if storage != "unknown":
            return storage
    return "unknown"


def _storage_from_qemu(cfg: Dict[str, Any]) -> str:
    preferred_keys: List[str] = []
    for prefix in ("scsi", "virtio", "sata", "ide"):
        preferred_keys.extend(f"{prefix}{idx}" for idx in range(0, 6))
    seen = set()
    for key in preferred_keys:
        value = cfg.get(key)
        if value is None:
            continue
        seen.add(key)
        text = str(value)
        if "media=cdrom" in text or "cloudinit" in text:
            continue
        storage = _extract_storage_token(text)
        if storage != "unknown":
            return storage
    for key in sorted(cfg.keys()):
        if key in seen:
            continue
        if not any(key.startswith(prefix) for prefix in ("scsi", "virtio", "sata", "ide")):
            continue
        value = cfg.get(key)
        if value is None:
            continue
        text = str(value)
        if "media=cdrom" in text or "cloudinit" in text:
            continue
        storage = _extract_storage_token(text)
        if storage != "unknown":
            return storage
    for key in ("efidisk0", "tpmstate0"):
        storage = _extract_storage_token(cfg.get(key))
        if storage != "unknown":
            return storage
    return "unknown"


def _primary_lxc_disk(cfg: Dict[str, Any], entry: Dict[str, Any]) -> str:
    return str(cfg.get("rootfs") or entry.get("rootfs") or "")


def _primary_qemu_disk(cfg: Dict[str, Any]) -> str:
    preferred_keys: List[str] = []
    for prefix in ("scsi", "virtio", "sata", "ide"):
        preferred_keys.extend(f"{prefix}{idx}" for idx in range(0, 6))
    seen = set()
    for key in preferred_keys:
        value = cfg.get(key)
        if value is None:
            continue
        seen.add(key)
        text = str(value)
        if "media=cdrom" in text or "cloudinit" in text:
            continue
        return text
    for key in sorted(cfg.keys()):
        if key in seen:
            continue
        if not any(key.startswith(prefix) for prefix in ("scsi", "virtio", "sata", "ide")):
            continue
        value = cfg.get(key)
        if value is None:
            continue
        text = str(value)
        if "media=cdrom" in text or "cloudinit" in text:
            continue
        return text
    return ""


def _pick_container_storage(kind: str, cfg: Dict[str, Any], entry: Dict[str, Any]) -> str:
    storage = _extract_storage_token(cfg.get("storage") or entry.get("storage"))
    if storage != "unknown":
        return storage
    if kind == "lxc":
        return _storage_from_lxc(cfg, entry)
    return _storage_from_qemu(cfg)


def _pick_container_disk_gib(kind: str, cfg: Dict[str, Any], entry: Dict[str, Any]) -> float:
    if kind == "lxc":
        size = _extract_size_gib(_primary_lxc_disk(cfg, entry))
        if size:
            return size
    else:
        size = _extract_size_gib(_primary_qemu_disk(cfg))
        if size:
            return size
    for candidate in (entry.get("maxdisk"), entry.get("disk"), cfg.get("disk")):
        if candidate is None or candidate == 0:
            continue
        return _bytes_to_gib(candidate)
    return 0.0


def _to_mib(value: Any) -> float:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return 0.0
    if val <= 0:
        return 0.0
    # Heuristic: large values are bytes, smaller ones are already MiB.
    return _bytes_to_mib(val) if val > 10000 else val


def _pick_container_ram_mib(kind: str, cfg: Dict[str, Any], entry: Dict[str, Any]) -> float:
    for candidate in (cfg.get("memory"), entry.get("maxmem"), entry.get("mem")):
        ram = _to_mib(candidate)
        if ram:
            return ram
    return 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pick_container_cpu_share(kind: str, cfg: Dict[str, Any], entry: Dict[str, Any]) -> float:
    if kind == "lxc":
        for key in ("cpulimit", "cores", "cpus"):
            val = _safe_float(cfg.get(key))
            if val:
                return val
        return _safe_float(entry.get("cpus"))

    cores = _safe_float(cfg.get("cores"))
    sockets = _safe_float(cfg.get("sockets")) or 1.0
    if cores:
        return cores * sockets
    val = _safe_float(cfg.get("vcpus"))
    if val:
        return val
    val = _safe_float(entry.get("cpus") or entry.get("maxcpu"))
    if val:
        return val
    return 0.0


def _parse_onboot_flag(value: Any) -> bool:
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _write_bridge_config(bridge: str) -> None:
    begin = f"# Portacode INFRA BEGIN {bridge}"
    end = f"# Portacode INFRA END {bridge}"
    current = IFACES_PATH.read_text(encoding="utf-8") if IFACES_PATH.exists() else ""
    if begin in current:
        return
    block = f"""
{begin}
auto {bridge}
iface {bridge} inet static
    address {SUBNET_CIDR}
    bridge-ports none
    bridge-stp off
    bridge-fd 0
{end}

"""
    mode = "a" if IFACES_PATH.exists() else "w"
    with open(IFACES_PATH, mode, encoding="utf-8") as fh:
        if current and not current.endswith("\n"):
            fh.write("\n")
        fh.write(block)


def _ensure_sysctl() -> None:
    SYSCTL_PATH.write_text("net.ipv4.ip_forward=1\n", encoding="utf-8")
    _call_subprocess(["/sbin/sysctl", "-w", "net.ipv4.ip_forward=1"], check=True)


def _write_units(bridge: str) -> None:
    nat_name = f"portacode-{bridge}-nat.service"
    dns_name = f"portacode-{bridge}-dnsmasq.service"
    nat = UNIT_DIR / nat_name
    dns = UNIT_DIR / dns_name
    nat.write_text(f"""[Unit]
Description=Portacode NAT for {bridge}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/iptables -t nat -A POSTROUTING -s {BRIDGE_IP}/24 -o vmbr0 -j MASQUERADE
ExecStart=/usr/sbin/iptables -A FORWARD -i {bridge} -o vmbr0 -j ACCEPT
ExecStart=/usr/sbin/iptables -A FORWARD -i vmbr0 -o {bridge} -m state --state RELATED,ESTABLISHED -j ACCEPT
ExecStop=/usr/sbin/iptables -t nat -D POSTROUTING -s {BRIDGE_IP}/24 -o vmbr0 -j MASQUERADE
ExecStop=/usr/sbin/iptables -D FORWARD -i {bridge} -o vmbr0 -j ACCEPT
ExecStop=/usr/sbin/iptables -D FORWARD -i vmbr0 -o {bridge} -m state --state RELATED,ESTABLISHED -j ACCEPT

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    dns.write_text(f"""[Unit]
Description=Portacode dnsmasq for {bridge}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --interface={bridge} --bind-interfaces --listen-address={BRIDGE_IP} \
  --port=0 --dhcp-range={DHCP_START},{DHCP_END},12h \
  --dhcp-option=option:router,{BRIDGE_IP} \
  --dhcp-option=option:dns-server,{DNS_SERVER} \
  --conf-file=/dev/null --pid-file=/run/portacode_dnsmasq.pid --dhcp-leasefile=/var/lib/misc/portacode_dnsmasq.leases
Restart=always

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")


def _ensure_bridge(bridge: str = DEFAULT_BRIDGE) -> Dict[str, Any]:
    if os.geteuid() != 0:
        raise PermissionError("Bridge setup requires root privileges")
    if not shutil.which("dnsmasq"):
        apt = shutil.which("apt-get")
        if not apt:
            raise RuntimeError("dnsmasq is missing and apt-get unavailable to install it")
        update = _call_subprocess([apt, "update"], check=False)
        if update.returncode not in (0, 100):
            msg = update.stderr or update.stdout or f"exit status {update.returncode}"
            raise RuntimeError(f"apt-get update failed: {msg}")
        _call_subprocess([apt, "install", "-y", "dnsmasq"], check=True)
    _write_bridge_config(bridge)
    _ensure_sysctl()
    _write_units(bridge)
    _call_subprocess(["/bin/systemctl", "daemon-reload"], check=True)
    nat_service = f"portacode-{bridge}-nat.service"
    dns_service = f"portacode-{bridge}-dnsmasq.service"
    _call_subprocess(["/bin/systemctl", "enable", "--now", nat_service, dns_service], check=True)
    _call_subprocess(["/sbin/ifup", bridge], check=False)
    return {"applied": True, "bridge": bridge, "message": f"Bridge {bridge} configured"}


def _verify_connectivity(timeout: float = 5.0) -> bool:
    try:
        _call_subprocess(["/bin/ping", "-c", "2", "1.1.1.1"], check=True, timeout=timeout)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _revert_bridge() -> None:
    try:
        if NET_SETUP_SCRIPT.exists():
            _call_subprocess([sys.executable, str(NET_SETUP_SCRIPT), "revert"], check=True)
    except Exception as exc:
        logger.warning("Proxmox bridge revert failed: %s", exc)


def _ensure_containers_dir() -> None:
    CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)


def _copy_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = summary.copy()
    containers = summary.get("containers")
    if isinstance(containers, list):
        snapshot["containers"] = [entry.copy() for entry in containers]
    unmanaged = summary.get("unmanaged_containers")
    if isinstance(unmanaged, list):
        snapshot["unmanaged_containers"] = [entry.copy() for entry in unmanaged]
    return snapshot


def _refresh_container_statuses(records: List[Dict[str, Any]], config: Dict[str, Any] | None) -> None:
    if not records or not config:
        return
    try:
        proxmox = _connect_proxmox(config)
        node = _get_node_from_config(config)
        statuses = {
            str(ct.get("vmid")): (ct.get("status") or "unknown").lower()
            for ct in proxmox.nodes(node).lxc.get()
        }
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Failed to refresh container statuses: %s", exc)
        return
    for record in records:
        vmid = record.get("vmid")
        if vmid is None:
            continue
        try:
            vmid_key = str(int(vmid))
        except (ValueError, TypeError):
            continue
        status = statuses.get(vmid_key)
        if status:
            record["status"] = status


def _initialize_managed_containers_state(force: bool = False) -> Dict[str, Any]:
    with _MANAGED_CONTAINERS_STATE_LOCK:
        if _MANAGED_CONTAINERS_STATE["initialized"] and not force:
            base = _MANAGED_CONTAINERS_STATE["base_summary"]
            return _copy_summary(base) if base else {}

    config = _load_config()
    records = _load_managed_container_records()
    _refresh_container_statuses(records, config)
    base_summary = _build_full_container_summary(records, config)
    record_map: Dict[str, Dict[str, Any]] = {}
    for record in records:
        vmid = record.get("vmid")
        if vmid is None:
            continue
        try:
            record_map[str(int(vmid))] = record
        except (TypeError, ValueError):
            continue

    initial_totals = {
        "ram_mib": int(base_summary.get("total_ram_mib") or 0),
        "disk_gib": int(base_summary.get("total_disk_gib") or 0),
        "cpu_share": float(base_summary.get("total_cpu_share") or 0.0),
    }

    with _MANAGED_CONTAINERS_STATE_LOCK:
        _MANAGED_CONTAINERS_STATE["initialized"] = True
        _MANAGED_CONTAINERS_STATE["base_summary"] = base_summary
        _MANAGED_CONTAINERS_STATE["initial_totals"] = initial_totals
        _MANAGED_CONTAINERS_STATE["records"] = record_map
        _MANAGED_CONTAINERS_STATE["pending"] = {}
    return _copy_summary(base_summary)


def _load_managed_container_records() -> List[Dict[str, Any]]:
    _ensure_containers_dir()
    records: List[Dict[str, Any]] = []
    for path in sorted(CONTAINERS_DIR.glob("ct-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - best effort logging
            logger.debug("Unable to read container record %s: %s", path, exc)
            continue
        records.append(payload)
    return records


def _build_managed_containers_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_ram = 0
    total_disk = 0
    total_cpu_share = 0.0
    containers: List[Dict[str, Any]] = []

    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    for record in sorted(records, key=lambda entry: _as_int(entry.get("vmid"))):
        ram_mib = _as_int(record.get("ram_mib"))
        disk_gib = _as_int(record.get("disk_gib"))
        cpu_share = _as_float(record.get("cpus"))
        total_ram += ram_mib
        total_disk += disk_gib
        total_cpu_share += cpu_share
        status = (record.get("status") or "unknown").lower()
        containers.append(
            {
                "vmid": str(_as_int(record.get("vmid"))) if record.get("vmid") is not None else None,
                "device_id": record.get("device_id"),
                "hostname": record.get("hostname"),
                "template": record.get("template"),
                "storage": record.get("storage"),
                "disk_gib": disk_gib,
                "ram_mib": ram_mib,
                "cpu_share": cpu_share,
                "created_at": record.get("created_at"),
                "status": status,
            }
        )

    return {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(containers),
        "total_ram_mib": total_ram,
        "total_disk_gib": total_disk,
        "total_cpu_share": round(total_cpu_share, 2),
        "containers": containers,
    }


def _build_full_container_summary(records: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    base_summary = _build_managed_containers_summary(records)
    if not config or not config.get("token_value"):
        return base_summary

    try:
        proxmox = _connect_proxmox(config)
        node = _get_node_from_config(config)
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Unable to extend container summary with Proxmox data: %s", exc)
        return base_summary

    default_storage = (config.get("default_storage") or "").strip()
    record_map: Dict[str, Dict[str, Any]] = {}
    for record in records:
        vmid = record.get("vmid")
        if vmid is None:
            continue
        try:
            vmid_key = str(int(vmid))
        except (ValueError, TypeError):
            continue
        record_map[vmid_key] = record

    managed_entries: List[Dict[str, Any]] = []
    unmanaged_entries: List[Dict[str, Any]] = []
    allocated_ram = 0.0
    allocated_disk = 0.0
    allocated_cpu = 0.0

    def _process_entries(kind: str, getter: str) -> None:
        nonlocal allocated_ram, allocated_disk, allocated_cpu
        entries = getattr(proxmox.nodes(node), getter).get()
        for entry in entries:
            vmid = entry.get("vmid")
            if vmid is None:
                continue
            vmid_str = str(vmid)
            cfg: Dict[str, Any] = {}
            try:
                cfg = getattr(proxmox.nodes(node), getter)(vmid_str).config.get() or {}
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Failed to load %s config for %s: %s", kind, vmid_str, exc)
                cfg = {}

            record = record_map.get(vmid_str)
            description = cfg.get("description") or ""
            managed = bool(record) or MANAGED_MARKER in description
            hostname = entry.get("name") or cfg.get("hostname") or (record.get("hostname") if record else None)
            storage = _pick_container_storage(kind, cfg, entry)
            disk_gib = _pick_container_disk_gib(kind, cfg, entry)
            ram_mib = _pick_container_ram_mib(kind, cfg, entry)
            cpu_share = _pick_container_cpu_share(kind, cfg, entry)
            reserve_on_boot = _parse_onboot_flag(cfg.get("onboot"))
            matches_default_storage = bool(default_storage and storage and storage.lower() == default_storage.lower())

            base_entry = {
                "type": kind,
                "vmid": vmid_str,
                "hostname": hostname,
                "status": (entry.get("status") or "unknown").lower(),
                "storage": storage,
                "disk_gib": disk_gib,
                "ram_mib": ram_mib,
                "cpu_share": cpu_share,
                "reserve_on_boot": reserve_on_boot,
                "matches_default_storage": matches_default_storage,
                "managed": managed,
            }

            if managed:
                merged = base_entry | {
                    "device_id": record.get("device_id") if record else None,
                    "template": record.get("template") if record else None,
                    "created_at": record.get("created_at") if record else None,
                }
                managed_entries.append(merged)
            else:
                unmanaged_entries.append(base_entry)

            if managed or reserve_on_boot:
                allocated_ram += ram_mib
                allocated_cpu += cpu_share
            if managed or matches_default_storage:
                allocated_disk += disk_gib

    _process_entries("lxc", "lxc")
    _process_entries("qemu", "qemu")

    memory_info = {}
    cpu_info = {}
    try:
        node_status = proxmox.nodes(node).status.get()
        memory_info = node_status.get("memory") or {}
        cpu_info = node_status.get("cpuinfo") or {}
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Unable to read node status for resource totals: %s", exc)

    host_total_ram_mib = _bytes_to_mib(memory_info.get("total"))
    used_ram_mib = _bytes_to_mib(memory_info.get("used"))
    available_ram_mib = max(host_total_ram_mib - used_ram_mib, 0.0) if host_total_ram_mib else None
    host_total_cpu_cores = _safe_float(cpu_info.get("cores"))
    available_cpu_share = max(host_total_cpu_cores - allocated_cpu, 0.0) if host_total_cpu_cores else None

    host_total_disk_gib = None
    available_disk_gib = None
    if default_storage:
        try:
            storage_status = proxmox.nodes(node).storage(default_storage).status.get()
            host_total_disk_gib = _bytes_to_gib(storage_status.get("total"))
            available_disk_gib = _bytes_to_gib(storage_status.get("avail"))
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Unable to read storage status for %s: %s", default_storage, exc)

    summary = base_summary.copy()
    summary["containers"] = managed_entries
    summary["count"] = len(managed_entries)
    summary["total_ram_mib"] = int(sum(entry.get("ram_mib") or 0 for entry in managed_entries))
    summary["total_disk_gib"] = int(sum(entry.get("disk_gib") or 0 for entry in managed_entries))
    summary["total_cpu_share"] = round(sum(entry.get("cpu_share") or 0 for entry in managed_entries), 2)
    summary["unmanaged_containers"] = unmanaged_entries
    summary["allocated_ram_mib"] = round(allocated_ram, 2)
    summary["allocated_disk_gib"] = round(allocated_disk, 2)
    summary["allocated_cpu_share"] = round(allocated_cpu, 2)
    summary["host_total_ram_mib"] = int(host_total_ram_mib) if host_total_ram_mib else None
    summary["host_total_disk_gib"] = host_total_disk_gib
    summary["host_total_cpu_cores"] = host_total_cpu_cores if host_total_cpu_cores else None
    summary["available_ram_mib"] = int(available_ram_mib) if available_ram_mib is not None else None
    summary["available_disk_gib"] = available_disk_gib
    summary["available_cpu_share"] = available_cpu_share if available_cpu_share is not None else None
    return summary


def _pending_totals(pending: Iterable[Dict[str, Any]]) -> Tuple[int, int, float]:
    ram_total = 0
    disk_total = 0
    cpu_total = 0.0
    for entry in pending:
        ram_total += int(entry.get("ram_mib") or 0)
        disk_total += int(entry.get("disk_gib") or 0)
        cpu_total += float(entry.get("cpu_share") or 0.0)
    return ram_total, disk_total, cpu_total


def _compose_managed_containers_summary(
    records: Iterable[Dict[str, Any]],
    pending: Iterable[Dict[str, Any]],
    base_summary: Dict[str, Any],
    initial_totals: Dict[str, Any],
) -> Dict[str, Any]:
    managed_summary = _build_managed_containers_summary(list(records))
    summary = _copy_summary(base_summary)
    base_by_vmid = {
        str(entry.get("vmid")): entry
        for entry in base_summary.get("containers", [])
        if entry.get("vmid") is not None
    }
    default_storage = summary.get("default_storage")
    merged_containers: List[Dict[str, Any]] = []
    for entry in managed_summary["containers"]:
        vmid = str(entry.get("vmid")) if entry.get("vmid") is not None else None
        base_entry = base_by_vmid.get(vmid) if vmid is not None else None
        if base_entry:
            merged = base_entry.copy()
            merged.update(entry)
        else:
            merged = entry.copy()
            merged.setdefault("managed", True)
            merged.setdefault("type", "lxc")
            if default_storage and merged.get("storage"):
                merged["matches_default_storage"] = (
                    str(merged["storage"]).lower() == str(default_storage).lower()
                )
        merged_containers.append(merged)
    summary["updated_at"] = managed_summary["updated_at"]
    summary["count"] = managed_summary["count"]
    summary["total_ram_mib"] = managed_summary["total_ram_mib"]
    summary["total_disk_gib"] = managed_summary["total_disk_gib"]
    summary["total_cpu_share"] = managed_summary["total_cpu_share"]
    summary["containers"] = merged_containers

    pending_ram, pending_disk, pending_cpu = _pending_totals(pending)
    delta_ram = managed_summary["total_ram_mib"] - int(initial_totals.get("ram_mib") or 0)
    delta_disk = managed_summary["total_disk_gib"] - int(initial_totals.get("disk_gib") or 0)
    delta_cpu = managed_summary["total_cpu_share"] - float(initial_totals.get("cpu_share") or 0.0)

    if "allocated_ram_mib" in summary and summary.get("allocated_ram_mib") is not None:
        summary["allocated_ram_mib"] = round(float(summary["allocated_ram_mib"]) + delta_ram + pending_ram, 2)
    if "allocated_disk_gib" in summary and summary.get("allocated_disk_gib") is not None:
        summary["allocated_disk_gib"] = round(float(summary["allocated_disk_gib"]) + delta_disk + pending_disk, 2)
    if "allocated_cpu_share" in summary and summary.get("allocated_cpu_share") is not None:
        summary["allocated_cpu_share"] = round(float(summary["allocated_cpu_share"]) + delta_cpu + pending_cpu, 2)

    if "available_ram_mib" in summary and summary.get("available_ram_mib") is not None:
        available_ram = float(summary["available_ram_mib"]) - delta_ram - pending_ram
        summary["available_ram_mib"] = max(int(available_ram), 0)
    if "available_disk_gib" in summary and summary.get("available_disk_gib") is not None:
        available_disk = float(summary["available_disk_gib"]) - delta_disk - pending_disk
        summary["available_disk_gib"] = max(available_disk, 0.0)
    if "available_cpu_share" in summary and summary.get("available_cpu_share") is not None:
        available_cpu = float(summary["available_cpu_share"]) - delta_cpu - pending_cpu
        summary["available_cpu_share"] = max(available_cpu, 0.0)

    return summary


def _get_managed_containers_summary(force: bool = False) -> Dict[str, Any]:
    _initialize_managed_containers_state(force=force)
    with _MANAGED_CONTAINERS_STATE_LOCK:
        base_summary = _MANAGED_CONTAINERS_STATE.get("base_summary") or {}
        records = list(_MANAGED_CONTAINERS_STATE.get("records", {}).values())
        pending = list(_MANAGED_CONTAINERS_STATE.get("pending", {}).values())
        initial_totals = _MANAGED_CONTAINERS_STATE.get("initial_totals", {})
    if not base_summary:
        return {}
    return _compose_managed_containers_summary(records, pending, base_summary, initial_totals)


def _reserve_container_resources(payload: Dict[str, Any], *, device_id: str, request_id: Optional[str]) -> str:
    _initialize_managed_containers_state()
    ram_mib = int(payload.get("ram_mib") or 0)
    disk_gib = int(payload.get("disk_gib") or 0)
    cpu_share = float(payload.get("cpus") or 0.0)
    reservation_id = secrets.token_hex(8)
    with _MANAGED_CONTAINERS_STATE_LOCK:
        base_summary = _MANAGED_CONTAINERS_STATE.get("base_summary") or {}
        records = list(_MANAGED_CONTAINERS_STATE.get("records", {}).values())
        pending = list(_MANAGED_CONTAINERS_STATE.get("pending", {}).values())
        initial_totals = _MANAGED_CONTAINERS_STATE.get("initial_totals", {})
        summary = _compose_managed_containers_summary(records, pending, base_summary, initial_totals)

        available_ram = summary.get("available_ram_mib")
        if available_ram is not None and ram_mib > available_ram:
            raise RuntimeError("Not enough RAM to create this container.")
        available_disk = summary.get("available_disk_gib")
        if available_disk is not None and disk_gib > available_disk:
            raise RuntimeError("Not enough disk space to create this container.")
        available_cpu = summary.get("available_cpu_share")
        if available_cpu is not None and cpu_share > available_cpu:
            raise RuntimeError("Not enough CPU capacity to create this container.")

        _MANAGED_CONTAINERS_STATE["pending"][reservation_id] = {
            "reservation_id": reservation_id,
            "device_id": device_id,
            "request_id": request_id,
            "ram_mib": ram_mib,
            "disk_gib": disk_gib,
            "cpu_share": cpu_share,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
    return reservation_id


def _release_container_reservation(reservation_id: Optional[str]) -> None:
    if not reservation_id:
        return
    with _MANAGED_CONTAINERS_STATE_LOCK:
        _MANAGED_CONTAINERS_STATE.get("pending", {}).pop(reservation_id, None)


def _format_rootfs(storage: str, disk_gib: int, storage_type: str) -> str:
    if storage_type in ("lvm", "lvmthin"):
        return f"{storage}:{disk_gib}"
    return f"{storage}:{disk_gib}G"


def _get_provisioning_user_info(message: Dict[str, Any]) -> Tuple[str, str, str]:
    user = (message.get("username") or "svcuser").strip() if message else "svcuser"
    user = user or "svcuser"
    password = message.get("password")
    if not password:
        password = secrets.token_urlsafe(10)
    ssh_key = (message.get("ssh_key") or "").strip() if message else ""
    return user, password, ssh_key


def _friendly_step_label(step_name: str) -> str:
    if not step_name:
        return "Step"
    normalized = step_name.replace("_", " ").strip()
    return normalized.capitalize()


_NETWORK_WAIT_CMD = (
    "count=0; "
    "while [ \"$count\" -lt 20 ]; do "
    "  if command -v ip >/dev/null 2>&1 && ip route get 1.1.1.1 >/dev/null 2>&1; then break; fi; "
    "  if [ -f /proc/net/route ] && grep -q '^00000000' /proc/net/route >/dev/null 2>&1; then break; fi; "
    "  sleep 1; "
    "  count=$((count+1)); "
    "done"
)

_PACKAGE_MANAGER_PROFILES: Dict[str, Dict[str, Any]] = {
    "apt": {
        "update_cmd": "apt-get update -y",
        "update_step_name": "apt_update",
        "install_cmd": "apt-get install -y python3 python3-pip sudo --fix-missing",
        "install_step_name": "install_deps",
        "update_retries": 4,
        "install_retries": 5,
    },
    "dnf": {
        "update_cmd": "dnf check-update || true",
        "update_step_name": "dnf_update",
        "install_cmd": "dnf install -y python3 python3-pip sudo",
        "install_step_name": "install_deps",
        "update_retries": 3,
        "install_retries": 5,
    },
    "yum": {
        "update_cmd": "yum makecache",
        "update_step_name": "yum_update",
        "install_cmd": "yum install -y python3 python3-pip sudo",
        "install_step_name": "install_deps",
        "update_retries": 3,
        "install_retries": 5,
    },
    "apk": {
        "update_cmd": "apk update",
        "update_step_name": "apk_update",
        "install_cmd": "apk add --no-cache python3 py3-pip sudo shadow",
        "install_step_name": "install_deps",
        "update_retries": 3,
        "install_retries": 5,
    },
    "pacman": {
        "update_cmd": "pacman -Sy --noconfirm",
        "update_step_name": "pacman_update",
        "install_cmd": "pacman -S --noconfirm python python-pip sudo",
        "install_step_name": "install_deps",
        "update_retries": 3,
        "install_retries": 5,
    },
    "zypper": {
        "update_cmd": "zypper refresh",
        "update_step_name": "zypper_update",
        "install_cmd": "zypper install -y python3 python3-pip sudo",
        "install_step_name": "install_deps",
        "update_retries": 3,
        "install_retries": 5,
    },
}

_UPDATE_RETRY_ON = [
    "Temporary failure resolving",
    "Could not resolve",
    "Failed to fetch",
]

_INSTALL_RETRY_ON = [
    "lock-frontend",
    "Unable to acquire the dpkg frontend lock",
    "Temporary failure resolving",
    "Could not resolve",
    "Failed to fetch",
]


def _build_bootstrap_steps(
    user: str,
    password: str,
    ssh_key: str,
    include_portacode_connect: bool = True,
    package_manager: str = "apt",
) -> List[Dict[str, Any]]:
    profile = _PACKAGE_MANAGER_PROFILES.get(package_manager, _PACKAGE_MANAGER_PROFILES["apt"])
    steps: List[Dict[str, Any]] = [
        {"name": "wait_for_network", "cmd": _NETWORK_WAIT_CMD, "retries": 0},
    ]
    update_cmd = profile.get("update_cmd")
    if update_cmd:
        steps.append(
            {
                "name": profile.get("update_step_name", "package_update"),
                "cmd": update_cmd,
                "retries": profile.get("update_retries", 3),
                "retry_delay_s": 5,
                "retry_on": _UPDATE_RETRY_ON,
            }
        )
    install_cmd = profile.get("install_cmd")
    if install_cmd:
        steps.append(
            {
                "name": profile.get("install_step_name", "install_deps"),
                "cmd": install_cmd,
                "retries": profile.get("install_retries", 5),
                "retry_delay_s": 5,
                "retry_on": _INSTALL_RETRY_ON,
            }
        )
    steps.extend(
        [
            {
                "name": "user_exists",
                "cmd": (
                    f"id -u {user} >/dev/null 2>&1 || "
                    f"(if command -v adduser >/dev/null 2>&1 && adduser --disabled-password --help >/dev/null 2>&1; then "
                    f"    adduser --disabled-password --gecos '' {user}; "
                    "else "
                    f"    useradd -m -s /bin/sh {user}; "
                    "fi)"
                ),
                "retries": 0,
            },
            {
                "name": "add_sudo",
                "cmd": (
                    f"if command -v usermod >/dev/null 2>&1; then "
                    "  if ! getent group sudo >/dev/null 2>&1; then "
                    "    if command -v groupadd >/dev/null 2>&1; then "
                    "      groupadd sudo >/dev/null 2>&1 || true; "
                    "    fi; "
                    "  fi; "
                    f"  usermod -aG sudo {user}; "
                    "else "
                    "  for grp in wheel sudo; do "
                    "    if ! getent group \"$grp\" >/dev/null 2>&1 && command -v groupadd >/dev/null 2>&1; then "
                    "      groupadd \"$grp\" >/dev/null 2>&1 || true; "
                    "    fi; "
                    "    addgroup \"$grp\" >/dev/null 2>&1 || true; "
                    f"    addgroup {user} \"$grp\" >/dev/null 2>&1 || true; "
                    "  done; "
                    "fi"
                ),
                "retries": 0,
            },
        {
            "name": "add_sudoers",
            "cmd": (
                f"printf '%s ALL=(ALL) NOPASSWD:ALL\\n' {shlex.quote(user)} >/etc/sudoers.d/portacode && "
                "chmod 0440 /etc/sudoers.d/portacode"
            ),
            "retries": 0,
        },
        ]
    )
    if password:
        steps.append({"name": "set_password", "cmd": f"echo '{user}:{password}' | chpasswd", "retries": 0})
    if ssh_key:
        steps.append(
            {
                "name": "add_ssh_key",
                "cmd": f"install -d -m 700 /home/{user}/.ssh && echo '{ssh_key}' >> /home/{user}/.ssh/authorized_keys && chown -R {user}:{user} /home/{user}/.ssh",
                "retries": 0,
            }
        )
    steps.extend(
        [
            {"name": "pip_upgrade", "cmd": "python3 -m pip install --upgrade pip", "retries": 0},
            {"name": "install_portacode", "cmd": "python3 -m pip install --upgrade portacode", "retries": 0},
        ]
    )
    if include_portacode_connect:
        steps.append({"name": "portacode_connect", "type": "portacode_connect", "timeout_s": 30})
    return steps


def _guess_package_manager_from_template(template: str) -> str:
    normalized = (template or "").lower()
    if "alpine" in normalized:
        return "apk"
    if "archlinux" in normalized:
        return "pacman"
    if "centos-7" in normalized:
        return "yum"
    if any(keyword in normalized for keyword in ("centos-8", "centos-9", "centos-9-stream", "centos-8-stream")):
        return "dnf"
    if any(keyword in normalized for keyword in ("rockylinux", "almalinux", "fedora")):
        return "dnf"
    if "opensuse" in normalized or "suse" in normalized:
        return "zypper"
    if any(keyword in normalized for keyword in ("debian", "ubuntu", "devuan", "turnkeylinux")):
        return "apt"
    if normalized.startswith("system/") and "linux" in normalized:
        return "apt"
    return "apt"


def _detect_package_manager(vmid: int) -> str:
    candidates = [
        ("apt", "apt-get"),
        ("dnf", "dnf"),
        ("yum", "yum"),
        ("apk", "apk"),
        ("pacman", "pacman"),
        ("zypper", "zypper"),
    ]
    for name, binary in candidates:
        res = _run_pct(vmid, f"command -v {binary} >/dev/null 2>&1")
        if res.get("returncode") == 0:
            logger.debug("Detected package manager %s inside container %s", name, vmid)
            return name
    logger.warning("Unable to detect package manager inside container %s; defaulting to apt", vmid)
    return "apt"


def _get_storage_type(storages: Iterable[Dict[str, Any]], storage_name: str) -> str:
    for entry in storages:
        if entry.get("storage") == storage_name:
            return entry.get("type", "")
    return ""


def _validate_positive_number(value: Any, default: float) -> float:
    try:
        candidate = float(value)
        if candidate > 0:
            return candidate
    except Exception:
        pass
    return float(default)


def _wait_for_task(proxmox: Any, node: str, upid: str) -> Tuple[Dict[str, Any], float]:
    start = time.time()
    while True:
        status = proxmox.nodes(node).tasks(upid).status.get()
        if status.get("status") == "stopped":
            return status, time.time() - start
        time.sleep(1)


def _list_running_managed(proxmox: Any, node: str) -> List[Tuple[str, Dict[str, Any]]]:
    entries = []
    for ct in proxmox.nodes(node).lxc.get():
        if ct.get("status") != "running":
            continue
        vmid = str(ct.get("vmid"))
        cfg = proxmox.nodes(node).lxc(vmid).config.get()
        if cfg and MANAGED_MARKER in (cfg.get("description") or ""):
            entries.append((vmid, cfg))
    return entries


def _start_container(proxmox: Any, node: str, vmid: int) -> Tuple[Dict[str, Any], float]:
    status = proxmox.nodes(node).lxc(vmid).status.current.get()
    if status.get("status") == "running":
        uptime = status.get("uptime", 0)
        logger.info("Container %s already running (%ss)", vmid, uptime)
        return status, 0.0

    upid = proxmox.nodes(node).lxc(vmid).status.start.post()
    return _wait_for_task(proxmox, node, upid)


def _stop_container(proxmox: Any, node: str, vmid: int) -> Tuple[Dict[str, Any], float]:
    status = proxmox.nodes(node).lxc(vmid).status.current.get()
    if status.get("status") != "running":
        return status, 0.0
    upid = proxmox.nodes(node).lxc(vmid).status.stop.post()
    return _wait_for_task(proxmox, node, upid)


def _delete_container(proxmox: Any, node: str, vmid: int) -> Tuple[Dict[str, Any], float]:
    upid = proxmox.nodes(node).lxc(vmid).delete()
    return _wait_for_task(proxmox, node, upid)


def _register_container_record(vmid: int, payload: Dict[str, Any], reservation_id: Optional[str] = None) -> None:
    _initialize_managed_containers_state()
    with _MANAGED_CONTAINERS_STATE_LOCK:
        if reservation_id:
            _MANAGED_CONTAINERS_STATE["pending"].pop(reservation_id, None)
        _MANAGED_CONTAINERS_STATE["records"][str(vmid)] = payload.copy()


def _unregister_container_record(vmid: int) -> None:
    _initialize_managed_containers_state()
    with _MANAGED_CONTAINERS_STATE_LOCK:
        _MANAGED_CONTAINERS_STATE["records"].pop(str(vmid), None)


def _write_container_record(vmid: int, payload: Dict[str, Any], reservation_id: Optional[str] = None) -> None:
    _ensure_containers_dir()
    path = CONTAINERS_DIR / f"ct-{vmid}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _register_container_record(vmid, payload, reservation_id=reservation_id)


def _read_container_record(vmid: int) -> Dict[str, Any]:
    path = CONTAINERS_DIR / f"ct-{vmid}.json"
    if not path.exists():
        raise FileNotFoundError(f"Container record {path} missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _update_container_record(vmid: int, updates: Dict[str, Any]) -> None:
    record = _read_container_record(vmid)
    record.update(updates)
    _write_container_record(vmid, record)


def _remove_container_record(vmid: int) -> None:
    path = CONTAINERS_DIR / f"ct-{vmid}.json"
    if path.exists():
        path.unlink()
    _unregister_container_record(vmid)


def _build_container_payload(message: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    templates = config.get("templates") or []
    default_template = templates[0] if templates else ""
    template = message.get("template") or default_template
    if not template:
        raise ValueError("Container template is required.")

    bridge = config.get("network", {}).get("bridge", DEFAULT_BRIDGE)
    hostname = (message.get("hostname") or "").strip()
    disk_gib = int(max(round(_validate_positive_number(message.get("disk_gib") or message.get("disk"), 3)), 1))
    ram_mib = int(max(round(_validate_positive_number(message.get("ram_mib") or message.get("ram"), 2048)), 1))
    cpus = _validate_positive_number(message.get("cpus"), 0.2)
    storage = message.get("storage") or config.get("default_storage") or ""
    if not storage:
        raise ValueError("Storage pool could not be determined.")

    user, password, ssh_key = _get_provisioning_user_info(message)

    payload = {
        "template": template,
        "storage": storage,
        "disk_gib": disk_gib,
        "ram_mib": ram_mib,
        "cpus": cpus,
        "hostname": hostname,
        "net0": f"name=eth0,bridge={bridge},ip=dhcp",
        "unprivileged": 1,
        "swap_mb": 0,
        "username": user,
        "password": password,
        "ssh_public_key": ssh_key,
        "description": MANAGED_MARKER,
    }
    return payload


def _ensure_infra_configured() -> Dict[str, Any]:
    config = _load_config()
    if not config or not config.get("token_value"):
        raise RuntimeError("Proxmox infrastructure is not configured.")
    return config


def _get_node_from_config(config: Dict[str, Any]) -> str:
    return config.get("node") or DEFAULT_NODE_NAME


def _parse_ctid(message: Dict[str, Any]) -> int:
    for key in ("ctid", "vmid"):
        value = message.get(key)
        if value is not None:
            try:
                return int(str(value).strip())
            except ValueError:
                raise ValueError(f"{key} must be an integer") from None
    raise ValueError("ctid is required")


def _ensure_container_managed(
    proxmox: Any, node: str, vmid: int, *, device_id: Optional[str] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    record = _read_container_record(vmid)
    ct_cfg = proxmox.nodes(node).lxc(str(vmid)).config.get()
    if not ct_cfg or MANAGED_MARKER not in (ct_cfg.get("description") or ""):
        raise RuntimeError(f"Container {vmid} is not managed by Portacode.")
    record_device_id = record.get("device_id")
    if device_id and str(record_device_id or "") != str(device_id):
        raise RuntimeError(
            f"Container {vmid} is managed for device {record_device_id!r}, not {device_id!r}."
        )
    return record, ct_cfg


def _connect_proxmox(config: Dict[str, Any]) -> Any:
    ProxmoxAPI = _ensure_proxmoxer()
    return ProxmoxAPI(
        config.get("host", DEFAULT_HOST),
        user=config.get("user"),
        token_name=config.get("token_name"),
        token_value=config.get("token_value"),
        verify_ssl=config.get("verify_ssl", False),
        timeout=60,
    )


def _run_pct(vmid: int, cmd: str, input_text: Optional[str] = None) -> Dict[str, Any]:
    shell = "/bin/sh"
    full = ["pct", "exec", str(vmid), "--", shell, "-c", cmd]
    start = time.time()
    proc = subprocess.run(full, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=input_text)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "elapsed_s": round(time.time() - start, 2),
    }


def _su_command(user: str, command: str) -> str:
    return f"su - {user} -s /bin/sh -c {shlex.quote(command)}"


def _resolve_portacode_cli_path(vmid: int, user: str) -> str:
    """Resolve the full path to the portacode CLI inside the container."""
    res = _run_pct(vmid, _su_command(user, "command -v portacode"))
    path = (res.get("stdout") or "").strip()
    if path:
        return path
    return "portacode"


def _run_pct_check(vmid: int, cmd: str) -> Dict[str, Any]:
    res = _run_pct(vmid, cmd)
    if res["returncode"] != 0:
        raise RuntimeError(res.get("stderr") or res.get("stdout") or "command failed")
    return res


def _run_pct_exec(vmid: int, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return _call_subprocess(["pct", "exec", str(vmid), "--", *command])


def _run_pct_exec_check(vmid: int, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    res = _run_pct_exec(vmid, command)
    if res.returncode != 0:
        raise RuntimeError(res.stderr or res.stdout or f"pct exec {' '.join(command)} failed")
    return res


def _run_pct_push(vmid: int, src: str, dest: str) -> subprocess.CompletedProcess[str]:
    return _call_subprocess(["pct", "push", str(vmid), src, dest])


def _push_bytes_to_container(
    vmid: int, user: str, path: str, data: bytes, mode: int = 0o600
) -> None:
    logger.debug("Preparing to push %d bytes to container vmid=%s path=%s for user=%s", len(data), vmid, path, user)
    tmp_path: Optional[str] = None
    try:
        parent = Path(path).parent
        parent_str = parent.as_posix()
        if parent_str not in {"", ".", "/"}:
            _run_pct_exec_check(vmid, ["mkdir", "-p", parent_str])
            _run_pct_exec_check(vmid, ["chown", "-R", f"{user}:{user}", parent_str])

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name

        push_res = _run_pct_push(vmid, tmp_path, path)
        if push_res.returncode != 0:
            raise RuntimeError(push_res.stderr or push_res.stdout or f"pct push returned {push_res.returncode}")

        _run_pct_exec_check(vmid, ["chown", f"{user}:{user}", path])
        _run_pct_exec_check(vmid, ["chmod", format(mode, "o"), path])
        logger.debug("Successfully pushed %d bytes to vmid=%s path=%s", len(data), vmid, path)
    except Exception as exc:
        logger.error("Failed to write to container vmid=%s path=%s for user=%s: %s", vmid, path, user, exc)
        raise
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError as cleanup_exc:
                logger.warning("Failed to remove temporary file %s: %s", tmp_path, cleanup_exc)


def _resolve_portacode_key_dir(vmid: int, user: str) -> str:
    data_dir_cmd = _su_command(user, "echo -n ${XDG_DATA_HOME:-$HOME/.local/share}")
    data_home = _run_pct_check(vmid, data_dir_cmd)["stdout"].strip()
    portacode_dir = f"{data_home}/portacode"
    _run_pct_exec_check(vmid, ["mkdir", "-p", portacode_dir])
    _run_pct_exec_check(vmid, ["chown", "-R", f"{user}:{user}", portacode_dir])
    return f"{portacode_dir}/keys"


def _deploy_device_keypair(vmid: int, user: str, private_key: str, public_key: str) -> None:
    key_dir = _resolve_portacode_key_dir(vmid, user)
    priv_path = f"{key_dir}/id_portacode"
    pub_path = f"{key_dir}/id_portacode.pub"
    _push_bytes_to_container(vmid, user, priv_path, private_key.encode(), mode=0o600)
    _push_bytes_to_container(vmid, user, pub_path, public_key.encode(), mode=0o644)


def _portacode_connect_and_read_key(vmid: int, user: str, timeout_s: int = 10) -> Dict[str, Any]:
    su_connect_cmd = _su_command(user, "portacode connect")
    cmd = ["pct", "exec", str(vmid), "--", "/bin/sh", "-c", su_connect_cmd]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = time.time()

    data_dir_cmd = _su_command(user, "echo -n ${XDG_DATA_HOME:-$HOME/.local/share}")
    data_dir = _run_pct_check(vmid, data_dir_cmd)["stdout"].strip()
    key_dir = f"{data_dir}/portacode/keys"
    pub_path = f"{key_dir}/id_portacode.pub"
    priv_path = f"{key_dir}/id_portacode"

    def file_size(path: str) -> Optional[int]:
        stat_cmd = _su_command(user, f"test -s {path} && stat -c %s {path}")
        res = _run_pct(vmid, stat_cmd)
        if res["returncode"] != 0:
            return None
        try:
            return int(res["stdout"].strip())
        except ValueError:
            return None

    last_pub = last_priv = None
    stable = 0
    history: List[Dict[str, Any]] = []

    process_exited = False
    exit_out = exit_err = ""
    while time.time() - start < timeout_s:
        if proc.poll() is not None:
            process_exited = True
            exit_out, exit_err = proc.communicate(timeout=1)
            history.append(
                {
                    "timestamp_s": round(time.time() - start, 2),
                    "status": "process_exited",
                    "returncode": proc.returncode,
                }
            )
            break
        pub_size = file_size(pub_path)
        priv_size = file_size(priv_path)
        if pub_size and priv_size:
            if pub_size == last_pub and priv_size == last_priv:
                stable += 1
            else:
                stable = 0
            last_pub, last_priv = pub_size, priv_size
            if stable >= 1:
                history.append(
                    {
                        "timestamp_s": round(time.time() - start, 2),
                        "pub_size": pub_size,
                        "priv_size": priv_size,
                        "stable": stable,
                    }
                )
                break
        history.append(
            {
                "timestamp_s": round(time.time() - start, 2),
                "pub_size": pub_size,
                "priv_size": priv_size,
                "stable": stable,
            }
        )
        time.sleep(1)

    final_pub = file_size(pub_path)
    final_priv = file_size(priv_path)
    if final_pub and final_priv:
        key_res = _run_pct(vmid, _su_command(user, f"cat {pub_path}"))
        if not process_exited:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        return {
            "ok": True,
            "public_key": key_res["stdout"].strip(),
            "history": history,
        }

    if not process_exited:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        exit_out, exit_err = proc.communicate(timeout=1)
        history.append(
            {
                "timestamp_s": round(time.time() - start, 2),
                "status": "timeout_waiting_for_keys",
            }
        )
        return {
            "ok": False,
            "error": "timed out waiting for portacode key files",
            "stdout": (exit_out or "").strip(),
            "stderr": (exit_err or "").strip(),
            "history": history,
        }

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

    key_res = _run_pct(vmid, _su_command(user, f"cat {pub_path}"))
    return {
        "ok": True,
        "public_key": key_res["stdout"].strip(),
        "history": history,
    }


def _summarize_error(res: Dict[str, Any]) -> str:
    text = f"{res.get('stdout','')}\n{res.get('stderr','')}"
    if "No space left on device" in text:
        return "Disk full inside container; increase rootfs or clean apt cache."
    if "Unable to acquire the dpkg frontend lock" in text or "lock-frontend" in text:
        return "Another apt/dpkg process is running; retry after it finishes."
    if "Temporary failure resolving" in text or "Could not resolve" in text:
        return "DNS/network resolution failed inside container."
    if "Failed to fetch" in text:
        return "Package repo fetch failed; check network and apt sources."
    return "Command failed; see stdout/stderr for details."


def _run_setup_steps(
    vmid: int,
    steps: List[Dict[str, Any]],
    user: str,
    progress_callback: Optional[ProgressCallback] = None,
    start_index: int = 1,
    total_steps: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    results: List[Dict[str, Any]] = []
    computed_total = total_steps if total_steps is not None else start_index + len(steps) - 1
    for offset, step in enumerate(steps):
        step_index = start_index + offset
        if progress_callback:
            progress_callback(step_index, computed_total, step, "in_progress", None)

        if step.get("type") == "portacode_connect":
            res = _portacode_connect_and_read_key(vmid, user, timeout_s=step.get("timeout_s", 10))
            res["name"] = step["name"]
            results.append(res)
            if not res.get("ok"):
                if progress_callback:
                    progress_callback(step_index, computed_total, step, "failed", res)
                return results, False
            if progress_callback:
                progress_callback(step_index, computed_total, step, "completed", res)
            continue

        attempts = 0
        retry_on = step.get("retry_on", [])
        max_attempts = step.get("retries", 0) + 1
        while True:
            attempts += 1
            res = _run_pct(vmid, step["cmd"])
            res["name"] = step["name"]
            res["attempt"] = attempts
            if res["returncode"] != 0:
                res["error_summary"] = _summarize_error(res)
            results.append(res)
            if res["returncode"] == 0:
                if progress_callback:
                    progress_callback(step_index, computed_total, step, "completed", res)
                break

            will_retry = False
            if attempts < max_attempts and retry_on:
                stderr_stdout = (res.get("stderr", "") + res.get("stdout", ""))
                if any(tok in stderr_stdout for tok in retry_on):
                    will_retry = True

            if progress_callback:
                status = "retrying" if will_retry else "failed"
                progress_callback(step_index, computed_total, step, status, res)

            if will_retry:
                time.sleep(step.get("retry_delay_s", 3))
                continue

            return results, False
    return results, True


def _bootstrap_portacode(
    vmid: int,
    user: str,
    password: str,
    ssh_key: str,
    steps: Optional[List[Dict[str, Any]]] = None,
    progress_callback: Optional[ProgressCallback] = None,
    start_index: int = 1,
    total_steps: Optional[int] = None,
    default_public_key: Optional[str] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    if steps is not None:
        actual_steps = steps
    else:
        detected_manager = _detect_package_manager(vmid)
        actual_steps = _build_bootstrap_steps(
            user,
            password,
            ssh_key,
            package_manager=detected_manager,
        )
    results, ok = _run_setup_steps(
        vmid,
        actual_steps,
        user,
        progress_callback=progress_callback,
        start_index=start_index,
        total_steps=total_steps,
    )
    if not ok:
        details = results[-1] if results else {}
        summary = details.get("error_summary") or details.get("stderr") or details.get("stdout") or details.get("name")
        history = details.get("history")
        history_snippet = ""
        if isinstance(history, list) and history:
            history_snippet = f" history={history[-3:]}"
        command = details.get("cmd")
        command_text = ""
        if command:
            if isinstance(command, (list, tuple)):
                command_text = shlex.join(str(entry) for entry in command)
            else:
                command_text = str(command)
        command_suffix = f" command={command_text}" if command_text else ""
        stdout = details.get("stdout")
        stderr = details.get("stderr")
        if stdout or stderr:
            logger.debug(
                "Bootstrap command output%s%s%s",
                f" stdout={stdout!r}" if stdout else "",
                " " if stdout and stderr else "",
                f"stderr={stderr!r}" if stderr else "",
            )
        if summary:
            logger.warning(
                "Portacode bootstrap failure summary=%s%s%s",
                summary,
                f" history_len={len(history)}" if history else "",
                f" command={command_text}" if command_text else "",
            )
        logger.error(
            "Portacode bootstrap command failed%s%s%s",
            f" command={command_text}" if command_text else "",
            f" stdout={stdout!r}" if stdout else "",
            f" stderr={stderr!r}" if stderr else "",
        )
        raise RuntimeError(
            f"Portacode bootstrap steps failed: {summary}{history_snippet}{command_suffix}"
        )
    key_step = next((entry for entry in results if entry.get("name") == "portacode_connect"), None)
    public_key = key_step.get("public_key") if key_step else default_public_key
    if not public_key:
        raise RuntimeError("Portacode connect did not return a public key.")
    return public_key, results


def build_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    network = config.get("network", {})
    base_network = {
        "applied": network.get("applied", False),
        "message": network.get("message"),
        "bridge": network.get("bridge", DEFAULT_BRIDGE),
    }
    if not config:
        return {"configured": False, "network": base_network}
    _ensure_templates_refreshed_on_startup(config)
    return {
        "configured": True,
        "host": config.get("host"),
        "node": config.get("node"),
        "user": config.get("user"),
        "token_name": config.get("token_name"),
        "default_storage": config.get("default_storage"),
        "templates": config.get("templates") or [],
        "last_verified": config.get("last_verified"),
        "network": base_network,
    }


def configure_infrastructure(token_identifier: str, token_value: str, verify_ssl: bool = False) -> Dict[str, Any]:
    ProxmoxAPI = _ensure_proxmoxer()
    user, token_name = _parse_token(token_identifier)
    client = ProxmoxAPI(
        DEFAULT_HOST,
        user=user,
        token_name=token_name,
        token_value=token_value,
        verify_ssl=verify_ssl,
        timeout=30,
    )
    node = _pick_node(client)
    status = client.nodes(node).status.get()
    storages = client.nodes(node).storage.get()
    default_storage = _pick_storage(storages)
    templates = _list_templates(client, node, storages)
    network: Dict[str, Any] = {}
    try:
        network = _ensure_bridge()
        # Wait for network convergence before validating connectivity
        time.sleep(2)
        if not _verify_connectivity():
            raise RuntimeError("Connectivity check failed; bridge reverted")
        network["health"] = "healthy"
    except Exception as exc:
        logger.warning("Bridge setup failed; reverting previous changes: %s", exc)
        _revert_bridge()
        raise
    config = {
        "host": DEFAULT_HOST,
        "node": node,
        "user": user,
        "token_name": token_name,
        "token_value": token_value,
        "verify_ssl": verify_ssl,
        "default_storage": default_storage,
        "last_verified": datetime.utcnow().isoformat() + "Z",
        "templates": templates,
        "templates_last_refreshed": _current_time_iso(),
        "network": network,
        "node_status": status,
    }
    _save_config(config)
    snapshot = build_snapshot(config)
    snapshot["node_status"] = status
    snapshot["managed_containers"] = _get_managed_containers_summary(force=True)
    return snapshot


def get_infra_snapshot() -> Dict[str, Any]:
    config = _load_config()
    snapshot = build_snapshot(config)
    if config.get("node_status"):
        snapshot["node_status"] = config["node_status"]
    snapshot["managed_containers"] = _get_managed_containers_summary()
    return snapshot


def revert_infrastructure() -> Dict[str, Any]:
    _revert_bridge()
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    snapshot = build_snapshot({})
    snapshot["network"] = snapshot.get("network", {})
    snapshot["network"]["applied"] = False
    snapshot["network"]["message"] = "Reverted to previous network state"
    snapshot["network"]["bridge"] = DEFAULT_BRIDGE
    snapshot["managed_containers"] = _get_managed_containers_summary(force=True)
    return snapshot


def _allocate_vmid(proxmox: Any) -> int:
    return int(proxmox.cluster.nextid.get())


def _instantiate_container(
    proxmox: Any, node: str, payload: Dict[str, Any], vmid: Optional[int] = None
) -> Tuple[int, float]:
    from proxmoxer.core import ResourceException

    storage_type = _get_storage_type(proxmox.nodes(node).storage.get(), payload["storage"])
    rootfs = _format_rootfs(payload["storage"], payload["disk_gib"], storage_type)
    vmid = vmid or _allocate_vmid(proxmox)
    if not payload.get("hostname"):
        payload["hostname"] = f"ct{vmid}"
    try:
        upid = proxmox.nodes(node).lxc.create(
            vmid=vmid,
            hostname=payload["hostname"],
            ostemplate=payload["template"],
            rootfs=rootfs,
            memory=int(payload["ram_mib"]),
            swap=int(payload.get("swap_mb", 0)),
            cores=max(int(payload.get("cores", 1)), 1),
            cpulimit=float(payload.get("cpulimit", payload.get("cpus", 1))),
            net0=payload["net0"],
            unprivileged=int(payload.get("unprivileged", 1)),
            description=payload.get("description", MANAGED_MARKER),
            password=payload.get("password") or None,
            ssh_public_keys=payload.get("ssh_public_key") or None,
        )
        status, elapsed = _wait_for_task(proxmox, node, upid)
        exitstatus = (status or {}).get("exitstatus")
        if exitstatus and exitstatus.upper() != "OK":
            msg = status.get("status") or "unknown error"
            details = status.get("error") or status.get("errmsg") or status.get("description") or status
            raise RuntimeError(
                f"Container creation task failed ({exitstatus}): {msg} details={details}"
            )
        return vmid, elapsed
    except ResourceException as exc:
        raise RuntimeError(f"Failed to create container: {exc}") from exc


def _cleanup_failed_container(
    proxmox: Any, node: str, vmid: int, provisioning_id: Optional[str]
) -> None:
    from proxmoxer.core import ResourceException

    try:
        cfg = proxmox.nodes(node).lxc(str(vmid)).config.get()
    except ResourceException as exc:
        msg = str(exc).lower()
        if "does not exist" in msg or "not found" in msg:
            return
        logger.warning("Failed to inspect container %s after create failure: %s", vmid, exc)
        return
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to inspect container %s after create failure: %s", vmid, exc)
        return

    description = (cfg or {}).get("description") or ""
    if provisioning_id and provisioning_id not in description:
        logger.warning(
            "Skipping cleanup for vmid=%s; provisioning marker mismatch", vmid
        )
        return

    try:
        status = proxmox.nodes(node).lxc(str(vmid)).status.current.get()
        if status.get("status") == "running":
            _stop_container(proxmox, node, vmid)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to stop container %s after create failure: %s", vmid, exc)

    try:
        _delete_container(proxmox, node, vmid)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to delete container %s after create failure: %s", vmid, exc)


class CreateProxmoxContainerHandler(SyncHandler):
    """Provision a new managed LXC container via the Proxmox API."""

    @property
    def command_name(self) -> str:
        return "create_proxmox_container"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("create_proxmox_container command received")
        request_id = message.get("request_id")
        raw_device_id = message.get("device_id")
        device_id = str(raw_device_id or "").strip()
        if not device_id:
            raise ValueError("device_id is required to create a container")
        device_public_key = (message.get("device_public_key") or "").strip()
        device_private_key = (message.get("device_private_key") or "").strip()
        has_device_keypair = bool(device_public_key and device_private_key)
        config_guess = _load_config()
        template_candidates = config_guess.get("templates") or []
        template_hint = (message.get("template") or (template_candidates[0] if template_candidates else "")).strip()
        package_manager = _guess_package_manager_from_template(template_hint)
        bootstrap_user, bootstrap_password, bootstrap_ssh_key = _get_provisioning_user_info(message)
        bootstrap_steps = _build_bootstrap_steps(
            bootstrap_user,
            bootstrap_password,
            bootstrap_ssh_key,
            include_portacode_connect=not has_device_keypair,
            package_manager=package_manager,
        )
        total_steps = 3 + len(bootstrap_steps) + 2
        current_step_index = 1

        def _run_lifecycle_step(
            step_name: str,
            step_label: str,
            start_message: str,
            success_message: str,
            action,
        ):
            nonlocal current_step_index
            step_index = current_step_index
            _emit_progress_event(self,
                step_index=step_index,
                total_steps=total_steps,
                step_name=step_name,
                step_label=step_label,
                status="in_progress",
                message=start_message,
                phase="lifecycle",
                request_id=request_id,
                on_behalf_of_device=device_id,
            )
            try:
                result = action()
            except Exception as exc:
                _emit_progress_event(
                    self,
                    step_index=step_index,
                    total_steps=total_steps,
                    step_name=step_name,
                    step_label=step_label,
                    status="failed",
                    message=f"{step_label} failed: {exc}",
                    phase="lifecycle",
                    request_id=request_id,
                    details={"error": str(exc)},
                    on_behalf_of_device=device_id,
                )
                raise
            _emit_progress_event(
                self,
                step_index=step_index,
                total_steps=total_steps,
                step_name=step_name,
                step_label=step_label,
                status="completed",
                message=success_message,
                phase="lifecycle",
                request_id=request_id,
                on_behalf_of_device=device_id,
            )
            current_step_index += 1
            return result

        def _validate_environment():
            if os.geteuid() != 0:
                raise PermissionError("Container creation requires root privileges.")
            config = _load_config()
            if not config or not config.get("token_value"):
                raise ValueError("Proxmox infrastructure is not configured.")
            if not config.get("network", {}).get("applied"):
                raise RuntimeError("Proxmox bridge setup must be applied before creating containers.")
            return config

        config = _run_lifecycle_step(
            "validate_environment",
            "Validating infrastructure",
            "Checking token, permissions, and bridge setup…",
            "Infrastructure validated.",
            _validate_environment,
        )

        node = config.get("node") or DEFAULT_NODE_NAME
        payload = _build_container_payload(message, config)
        payload["cpulimit"] = float(payload["cpus"])
        payload["cores"] = int(max(math.ceil(payload["cpus"]), 1))
        payload["memory"] = int(payload["ram_mib"])
        payload["node"] = node

        reservation_id = _reserve_container_resources(
            payload, device_id=device_id, request_id=request_id
        )
        provisioning_id = secrets.token_hex(6)
        payload["description"] = f"{payload.get('description', MANAGED_MARKER)};provisioning_id={provisioning_id}"

        def _provision_background() -> None:
            nonlocal current_step_index
            proxmox: Any = None
            vmid: Optional[int] = None
            created_record = False
            try:
                def _create_container():
                    nonlocal proxmox, vmid, created_record
                    proxmox = _connect_proxmox(config)
                    logger.debug(
                        "Provisioning container node=%s template=%s ram=%s cpu=%s storage=%s",
                        node,
                        payload["template"],
                        payload["ram_mib"],
                        payload["cpus"],
                        payload["storage"],
                    )
                    try:
                        vmid = _allocate_vmid(proxmox)
                        vmid, _ = _instantiate_container(proxmox, node, payload, vmid=vmid)
                    except Exception:
                        _release_container_reservation(reservation_id)
                        if vmid is not None:
                            _cleanup_failed_container(proxmox, node, vmid, provisioning_id)
                        raise
                    payload["vmid"] = vmid
                    payload["created_at"] = datetime.utcnow().isoformat() + "Z"
                    payload["status"] = "creating"
                    payload["device_id"] = device_id
                    try:
                        _write_container_record(vmid, payload, reservation_id=reservation_id)
                        created_record = True
                    except Exception:
                        _release_container_reservation(reservation_id)
                        _cleanup_failed_container(proxmox, node, vmid, provisioning_id)
                        raise
                    return proxmox, node, vmid, payload

                proxmox, _, vmid, payload_local = _run_lifecycle_step(
                    "create_container",
                    "Creating container",
                    "Provisioning the LXC container…",
                    "Container created.",
                    _create_container,
                )

                def _start_container_step():
                    _start_container(proxmox, node, vmid)

                _run_lifecycle_step(
                    "start_container",
                    "Starting container",
                    "Booting the container…",
                    "Container startup completed.",
                    _start_container_step,
                )
                _update_container_record(vmid, {"status": "running"})

                def _bootstrap_progress_callback(
                    step_index: int,
                    total: int,
                    step: Dict[str, Any],
                    status: str,
                    result: Optional[Dict[str, Any]],
                ):
                    label = step.get("display_name") or _friendly_step_label(step.get("name", "bootstrap"))
                    error_summary = (result or {}).get("error_summary") or (result or {}).get("error")
                    attempt = (result or {}).get("attempt")
                    if status == "in_progress":
                        message_text = f"{label} is running…"
                    elif status == "completed":
                        message_text = f"{label} completed."
                    elif status == "retrying":
                        attempt_desc = f" (attempt {attempt})" if attempt else ""
                        message_text = f"{label} failed{attempt_desc}; retrying…"
                    else:
                        message_text = f"{label} failed"
                        if error_summary:
                            message_text += f": {error_summary}"
                    details: Dict[str, Any] = {}
                    if attempt:
                        details["attempt"] = attempt
                    if error_summary:
                        details["error_summary"] = error_summary
                    _emit_progress_event(
                        self,
                        step_index=step_index,
                        total_steps=total,
                        step_name=step.get("name", "bootstrap"),
                        step_label=label,
                        status=status,
                        message=message_text,
                        phase="bootstrap",
                        request_id=request_id,
                        details=details or None,
                        on_behalf_of_device=device_id,
                    )

                public_key, steps = _bootstrap_portacode(
                    vmid,
                    payload_local["username"],
                    payload_local["password"],
                    payload_local["ssh_public_key"],
                    steps=bootstrap_steps,
                    progress_callback=_bootstrap_progress_callback,
                    start_index=current_step_index,
                    total_steps=total_steps,
                    default_public_key=device_public_key if has_device_keypair else None,
                )
                current_step_index += len(bootstrap_steps)

                service_installed = False
                if has_device_keypair:
                    logger.info(
                        "deploying dashboard-provided Portacode keypair (device_id=%s) into container %s",
                        device_id,
                        vmid,
                    )
                    _deploy_device_keypair(
                        vmid,
                        payload_local["username"],
                        device_private_key,
                        device_public_key,
                    )
                    service_installed = True
                    service_start_index = current_step_index

                    auth_step_name = "setup_device_authentication"
                    auth_label = "Setting up device authentication"
                    _emit_progress_event(
                        self,
                        step_index=service_start_index,
                        total_steps=total_steps,
                        step_name=auth_step_name,
                        step_label=auth_label,
                        status="in_progress",
                        message="Notifying the server of the new device…",
                        phase="service",
                        request_id=request_id,
                        on_behalf_of_device=device_id,
                    )
                    _emit_progress_event(
                        self,
                        step_index=service_start_index,
                        total_steps=total_steps,
                        step_name=auth_step_name,
                        step_label=auth_label,
                        status="completed",
                        message="Authentication metadata recorded.",
                        phase="service",
                        request_id=request_id,
                        on_behalf_of_device=device_id,
                    )

                    install_step = service_start_index + 1
                    install_label = "Launching Portacode service"
                    _emit_progress_event(
                        self,
                        step_index=install_step,
                        total_steps=total_steps,
                        step_name="launch_portacode_service",
                        step_label=install_label,
                        status="in_progress",
                        message="Running sudo portacode service install…",
                        phase="service",
                        request_id=request_id,
                        on_behalf_of_device=device_id,
                    )

                    cli_path = _resolve_portacode_cli_path(vmid, payload_local["username"])
                    cmd = _su_command(
                        payload_local["username"],
                        f"sudo -S {shlex.quote(cli_path)} service install",
                    )
                    res = _run_pct(vmid, cmd, input_text=payload_local["password"] + "\n")

                    if res["returncode"] != 0:
                        _emit_progress_event(
                            self,
                            step_index=install_step,
                            total_steps=total_steps,
                            step_name="launch_portacode_service",
                            step_label=install_label,
                            status="failed",
                            message=f"{install_label} failed: {res.get('stderr') or res.get('stdout')}",
                            phase="service",
                            request_id=request_id,
                            details={
                                "stderr": res.get("stderr"),
                                "stdout": res.get("stdout"),
                            },
                            on_behalf_of_device=device_id,
                        )
                        raise RuntimeError(res.get("stderr") or res.get("stdout") or "Service install failed")

                    _emit_progress_event(
                        self,
                        step_index=install_step,
                        total_steps=total_steps,
                        step_name="launch_portacode_service",
                        step_label=install_label,
                        status="completed",
                        message="Portacode service install finished.",
                        phase="service",
                        request_id=request_id,
                        on_behalf_of_device=device_id,
                    )

                    logger.info(
                        "create_proxmox_container: portacode service install completed inside ct %s", vmid
                    )

                    current_step_index += 2

                _emit_host_event(
                    self,
                    {
                        "event": "proxmox_container_created",
                        "success": True,
                        "message": f"Container {vmid} is ready and Portacode key captured.",
                        "ctid": str(vmid),
                        "public_key": public_key,
                        "container": {
                            "vmid": vmid,
                            "hostname": payload_local["hostname"],
                            "template": payload_local["template"],
                            "storage": payload_local["storage"],
                            "disk_gib": payload_local["disk_gib"],
                            "ram_mib": payload_local["ram_mib"],
                            "cpus": payload_local["cpus"],
                        },
                        "setup_steps": steps,
                        "device_id": device_id,
                        "on_behalf_of_device": device_id,
                        "service_installed": service_installed,
                        "request_id": request_id,
                    },
                )
            except Exception as exc:
                if reservation_id and not created_record:
                    _release_container_reservation(reservation_id)
                if vmid is not None and proxmox and node:
                    _cleanup_failed_container(proxmox, node, vmid, provisioning_id)
                    _remove_container_record(vmid)
                _emit_host_event(
                    self,
                    {
                        "event": "error",
                        "message": str(exc),
                        "device_id": device_id,
                        "request_id": request_id,
                    },
                )

        threading.Thread(target=_provision_background, daemon=True).start()

        return {
            "event": "proxmox_container_accepted",
            "success": True,
            "message": "Provisioning accepted; resources reserved.",
            "device_id": device_id,
            "request_id": request_id,
        }


class StartPortacodeServiceHandler(SyncHandler):
    """Start the Portacode service inside a newly created container."""

    @property
    def command_name(self) -> str:
        return "start_portacode_service"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        ctid = message.get("ctid")
        if not ctid:
            raise ValueError("ctid is required")
        try:
            vmid = int(ctid)
        except ValueError:
            raise ValueError("ctid must be an integer")

        record = _read_container_record(vmid)
        user = record.get("username")
        password = record.get("password")
        if not user or not password:
            raise RuntimeError("Container credentials unavailable")
        on_behalf_of_device = record.get("device_id")
        if on_behalf_of_device:
            on_behalf_of_device = str(on_behalf_of_device)

        start_index = int(message.get("step_index", 1))
        total_steps = int(message.get("total_steps", start_index + 2))
        request_id = message.get("request_id")

        auth_step_name = "setup_device_authentication"
        auth_label = "Setting up device authentication"
        _emit_progress_event(
            self,
            step_index=start_index,
            total_steps=total_steps,
            step_name=auth_step_name,
            step_label=auth_label,
            status="in_progress",
            message="Notifying the server of the new device…",
            phase="service",
            request_id=request_id,
            on_behalf_of_device=on_behalf_of_device,
        )
        _emit_progress_event(
            self,
            step_index=start_index,
            total_steps=total_steps,
            step_name=auth_step_name,
            step_label=auth_label,
            status="completed",
            message="Authentication metadata recorded.",
            phase="service",
            request_id=request_id,
            on_behalf_of_device=on_behalf_of_device,
        )

        install_step = start_index + 1
        install_label = "Launching Portacode service"
        _emit_progress_event(
            self,
            step_index=install_step,
            total_steps=total_steps,
            step_name="launch_portacode_service",
            step_label=install_label,
            status="in_progress",
            message="Running sudo portacode service install…",
            phase="service",
            request_id=request_id,
            on_behalf_of_device=on_behalf_of_device,
        )

        cli_path = _resolve_portacode_cli_path(vmid, user)
        cmd = _su_command(user, f"sudo -S {shlex.quote(cli_path)} service install")
        res = _run_pct(vmid, cmd, input_text=password + "\n")

        if res["returncode"] != 0:
            _emit_progress_event(
                self,
                step_index=install_step,
                total_steps=total_steps,
                step_name="launch_portacode_service",
                step_label=install_label,
                status="failed",
                message=f"{install_label} failed: {res.get('stderr') or res.get('stdout')}",
                phase="service",
                request_id=request_id,
                details={
                    "stderr": res.get("stderr"),
                    "stdout": res.get("stdout"),
                },
                on_behalf_of_device=on_behalf_of_device,
            )
            raise RuntimeError(res.get("stderr") or res.get("stdout") or "Service install failed")

        _emit_progress_event(
            self,
            step_index=install_step,
            total_steps=total_steps,
            step_name="launch_portacode_service",
            step_label=install_label,
            status="completed",
            message="Portacode service install finished.",
            phase="service",
            request_id=request_id,
            on_behalf_of_device=on_behalf_of_device,
        )

        return {
            "event": "proxmox_service_started",
            "success": True,
            "message": "Portacode service install completed",
            "ctid": str(vmid),
        }


class StartProxmoxContainerHandler(SyncHandler):
    """Start a managed container via the Proxmox API."""

    @property
    def command_name(self) -> str:
        return "start_proxmox_container"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        vmid = _parse_ctid(message)
        child_device_id = (message.get("child_device_id") or "").strip()
        if not child_device_id:
            raise ValueError("child_device_id is required for start_proxmox_container")
        config = _ensure_infra_configured()
        proxmox = _connect_proxmox(config)
        node = _get_node_from_config(config)
        _ensure_container_managed(proxmox, node, vmid, device_id=child_device_id)

        status, elapsed = _start_container(proxmox, node, vmid)
        _update_container_record(vmid, {"status": "running"})

        infra = get_infra_snapshot()
        return {
            "event": "proxmox_container_action",
            "action": "start",
            "success": True,
            "ctid": str(vmid),
            "message": f"Started container {vmid} in {elapsed:.1f}s.",
            "details": {"exitstatus": status.get("exitstatus")},
            "status": status.get("status"),
            "infra": infra,
        }


class StopProxmoxContainerHandler(SyncHandler):
    """Stop a managed container via the Proxmox API."""

    @property
    def command_name(self) -> str:
        return "stop_proxmox_container"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        vmid = _parse_ctid(message)
        child_device_id = (message.get("child_device_id") or "").strip()
        if not child_device_id:
            raise ValueError("child_device_id is required for stop_proxmox_container")
        config = _ensure_infra_configured()
        proxmox = _connect_proxmox(config)
        node = _get_node_from_config(config)
        _ensure_container_managed(proxmox, node, vmid, device_id=child_device_id)

        status, elapsed = _stop_container(proxmox, node, vmid)
        final_status = status.get("status") or "stopped"
        _update_container_record(vmid, {"status": final_status})

        infra = get_infra_snapshot()
        message_text = (
            f"Container {vmid} is already stopped."
            if final_status != "running" and elapsed == 0.0
            else f"Stopped container {vmid} in {elapsed:.1f}s."
        )
        return {
            "event": "proxmox_container_action",
            "action": "stop",
            "success": True,
            "ctid": str(vmid),
            "message": message_text,
            "details": {"exitstatus": status.get("exitstatus")},
            "status": final_status,
            "infra": infra,
        }


class RemoveProxmoxContainerHandler(SyncHandler):
    """Delete a managed container via the Proxmox API."""

    @property
    def command_name(self) -> str:
        return "remove_proxmox_container"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        vmid = _parse_ctid(message)
        child_device_id = (message.get("child_device_id") or "").strip()
        if not child_device_id:
            raise ValueError("child_device_id is required for remove_proxmox_container")
        config = _ensure_infra_configured()
        proxmox = _connect_proxmox(config)
        node = _get_node_from_config(config)
        _ensure_container_managed(proxmox, node, vmid, device_id=child_device_id)

        stop_status, stop_elapsed = _stop_container(proxmox, node, vmid)
        delete_status, delete_elapsed = _delete_container(proxmox, node, vmid)
        _remove_container_record(vmid)

        infra = get_infra_snapshot()
        return {
            "event": "proxmox_container_action",
            "action": "remove",
            "success": True,
            "ctid": str(vmid),
            "message": f"Deleted container {vmid} in {delete_elapsed:.1f}s.",
            "details": {
                "stop_exitstatus": stop_status.get("exitstatus"),
                "delete_exitstatus": delete_status.get("exitstatus"),
            },
            "status": "deleted",
            "infra": infra,
        }


class ConfigureProxmoxInfraHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "setup_proxmox_infra"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        token_identifier = message.get("token_identifier")
        token_value = message.get("token_value")
        verify_ssl = bool(message.get("verify_ssl"))
        if not token_identifier or not token_value:
            raise ValueError("token_identifier and token_value are required")
        snapshot = configure_infrastructure(token_identifier, token_value, verify_ssl=verify_ssl)
        return {
            "event": "proxmox_infra_configured",
            "success": True,
            "message": "Proxmox infrastructure configured",
            "infra": snapshot,
        }


class RevertProxmoxInfraHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "revert_proxmox_infra"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = revert_infrastructure()
        return {
            "event": "proxmox_infra_reverted",
            "success": True,
            "message": "Proxmox infrastructure configuration reverted",
            "infra": snapshot,
        }
