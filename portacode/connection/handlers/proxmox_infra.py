"""Proxmox infrastructure configuration handler."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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

ProgressCallback = Callable[[int, int, Dict[str, Any], str, Optional[Dict[str, Any]]], None]


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


def _pick_storage(storages: Iterable[Dict[str, Any]]) -> str:
    candidates = [s for s in storages if "rootdir" in s.get("content", "") and s.get("avail", 0) > 0]
    if not candidates:
        candidates = [s for s in storages if "rootdir" in s.get("content", "")]
    if not candidates:
        return ""
    candidates.sort(key=lambda entry: entry.get("avail", 0), reverse=True)
    return candidates[0].get("storage", "")


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
        _call_subprocess([apt, "update"], check=True)
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


def _build_bootstrap_steps(user: str, password: str, ssh_key: str) -> List[Dict[str, Any]]:
    steps = [
        {
            "name": "apt_update",
            "cmd": "apt-get update -y",
            "retries": 4,
            "retry_delay_s": 5,
            "retry_on": [
                "Temporary failure resolving",
                "Could not resolve",
                "Failed to fetch",
            ],
        },
        {
            "name": "install_deps",
            "cmd": "apt-get install -y python3 python3-pip sudo --fix-missing",
            "retries": 5,
            "retry_delay_s": 5,
            "retry_on": [
                "lock-frontend",
                "Unable to acquire the dpkg frontend lock",
                "Temporary failure resolving",
                "Could not resolve",
                "Failed to fetch",
            ],
        },
        {"name": "user_exists", "cmd": f"id -u {user} >/dev/null 2>&1 || adduser --disabled-password --gecos '' {user}", "retries": 0},
        {"name": "add_sudo", "cmd": f"usermod -aG sudo {user}", "retries": 0},
    ]
    if password:
        steps.append({"name": "set_password", "cmd": f"echo '{user}:{password}' | chpasswd", "retries": 0})
    if ssh_key:
        steps.append({
            "name": "add_ssh_key",
            "cmd": f"install -d -m 700 /home/{user}/.ssh && echo '{ssh_key}' >> /home/{user}/.ssh/authorized_keys && chown -R {user}:{user} /home/{user}/.ssh",
            "retries": 0,
        })
    steps.extend([
        {"name": "pip_upgrade", "cmd": "python3 -m pip install --upgrade pip", "retries": 0},
        {"name": "install_portacode", "cmd": "python3 -m pip install --upgrade portacode", "retries": 0},
        {"name": "portacode_connect", "type": "portacode_connect", "timeout_s": 30},
    ])
    return steps


def _get_storage_type(storages: Iterable[Dict[str, Any]], storage_name: str) -> str:
    for entry in storages:
        if entry.get("storage") == storage_name:
            return entry.get("type", "")
    return ""


def _validate_positive_int(value: Any, default: int) -> int:
    try:
        candidate = int(value)
        if candidate > 0:
            return candidate
    except Exception:
        pass
    return default


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

    node_status = proxmox.nodes(node).status.get()
    mem_total_mb = int(node_status.get("memory", {}).get("total", 0) // (1024**2))
    cores_total = int(node_status.get("cpuinfo", {}).get("cores", 0))

    running = _list_running_managed(proxmox, node)
    used_mem_mb = sum(int(cfg.get("memory", 0)) for _, cfg in running)
    used_cores = sum(int(cfg.get("cores", 0)) for _, cfg in running)

    target_cfg = proxmox.nodes(node).lxc(vmid).config.get()
    target_mem_mb = int(target_cfg.get("memory", 0))
    target_cores = int(target_cfg.get("cores", 0))

    if mem_total_mb and used_mem_mb + target_mem_mb > mem_total_mb:
        raise RuntimeError("Not enough RAM to start this container safely.")
    if cores_total and used_cores + target_cores > cores_total:
        raise RuntimeError("Not enough CPU cores to start this container safely.")

    upid = proxmox.nodes(node).lxc(vmid).status.start.post()
    return _wait_for_task(proxmox, node, upid)


def _write_container_record(vmid: int, payload: Dict[str, Any]) -> None:
    _ensure_containers_dir()
    path = CONTAINERS_DIR / f"ct-{vmid}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_container_record(vmid: int) -> Dict[str, Any]:
    path = CONTAINERS_DIR / f"ct-{vmid}.json"
    if not path.exists():
        raise FileNotFoundError(f"Container record {path} missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_container_payload(message: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    templates = config.get("templates") or []
    default_template = templates[0] if templates else ""
    template = message.get("template") or default_template
    if not template:
        raise ValueError("Container template is required.")

    bridge = config.get("network", {}).get("bridge", DEFAULT_BRIDGE)
    hostname = (message.get("hostname") or "").strip()
    disk_gib = _validate_positive_int(message.get("disk_gib") or message.get("disk"), 32)
    ram_mib = _validate_positive_int(message.get("ram_mib") or message.get("ram"), 2048)
    cpus = _validate_positive_int(message.get("cpus"), 1)
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
    full = ["pct", "exec", str(vmid), "--", "bash", "-lc", cmd]
    start = time.time()
    proc = subprocess.run(full, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=input_text)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "elapsed_s": round(time.time() - start, 2),
    }


def _run_pct_check(vmid: int, cmd: str) -> Dict[str, Any]:
    res = _run_pct(vmid, cmd)
    if res["returncode"] != 0:
        raise RuntimeError(res.get("stderr") or res.get("stdout") or "command failed")
    return res


def _portacode_connect_and_read_key(vmid: int, user: str, timeout_s: int = 10) -> Dict[str, Any]:
    cmd = ["pct", "exec", str(vmid), "--", "bash", "-lc", f"su - {user} -c 'portacode connect'"]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = time.time()

    data_dir_cmd = f"su - {user} -c 'echo -n ${{XDG_DATA_HOME:-$HOME/.local/share}}'"
    data_dir = _run_pct_check(vmid, data_dir_cmd)["stdout"].strip()
    key_dir = f"{data_dir}/portacode/keys"
    pub_path = f"{key_dir}/id_portacode.pub"
    priv_path = f"{key_dir}/id_portacode"

    def file_size(path: str) -> Optional[int]:
        stat_cmd = f"su - {user} -c 'test -s {path} && stat -c %s {path}'"
        res = _run_pct(vmid, stat_cmd)
        if res["returncode"] != 0:
            return None
        try:
            return int(res["stdout"].strip())
        except ValueError:
            return None

    last_pub = last_priv = None
    stable = 0
    while time.time() - start < timeout_s:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=1)
            return {
                "ok": False,
                "error": "portacode connect exited before keys were created",
                "stdout": (out or "").strip(),
                "stderr": (err or "").strip(),
            }
        pub_size = file_size(pub_path)
        priv_size = file_size(priv_path)
        if pub_size and priv_size:
            if pub_size == last_pub and priv_size == last_priv:
                stable += 1
            else:
                stable = 0
            last_pub, last_priv = pub_size, priv_size
            if stable >= 1:
                break
        time.sleep(1)

    if stable < 1:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        out, err = proc.communicate(timeout=1)
        return {
            "ok": False,
            "error": "timed out waiting for portacode key files",
            "stdout": (out or "").strip(),
            "stderr": (err or "").strip(),
        }

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

    key_res = _run_pct(vmid, f"su - {user} -c 'cat {pub_path}'")
    return {
        "ok": True,
        "public_key": key_res["stdout"].strip(),
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
) -> Tuple[str, List[Dict[str, Any]]]:
    actual_steps = steps if steps is not None else _build_bootstrap_steps(user, password, ssh_key)
    results, ok = _run_setup_steps(
        vmid,
        actual_steps,
        user,
        progress_callback=progress_callback,
        start_index=start_index,
        total_steps=total_steps,
    )
    if not ok:
        raise RuntimeError("Portacode bootstrap steps failed.")
    key_step = next((entry for entry in results if entry.get("name") == "portacode_connect"), None)
    public_key = key_step.get("public_key") if key_step else None
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
        if _verify_connectivity():
            network["health"] = "healthy"
        else:
            network = {"applied": False, "bridge": DEFAULT_BRIDGE, "message": "Connectivity check failed; bridge reverted"}
            _revert_bridge()
    except PermissionError as exc:
        network = {"applied": False, "message": str(exc), "bridge": DEFAULT_BRIDGE}
        logger.warning("Bridge setup skipped: %s", exc)
    except Exception as exc:  # pragma: no cover - best effort
        network = {"applied": False, "message": str(exc), "bridge": DEFAULT_BRIDGE}
        logger.warning("Bridge setup failed: %s", exc)
    config = {
        "host": DEFAULT_HOST,
        "node": node,
        "user": user,
        "token_name": token_name,
        "token_value": token_value,
        "verify_ssl": verify_ssl,
        "default_storage": default_storage,
        "templates": templates,
        "last_verified": datetime.utcnow().isoformat() + "Z",
        "network": network,
        "node_status": status,
    }
    _save_config(config)
    snapshot = build_snapshot(config)
    snapshot["node_status"] = status
    return snapshot


def get_infra_snapshot() -> Dict[str, Any]:
    config = _load_config()
    snapshot = build_snapshot(config)
    if config.get("node_status"):
        snapshot["node_status"] = config["node_status"]
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
    return snapshot


def _allocate_vmid(proxmox: Any) -> int:
    return int(proxmox.cluster.nextid.get())


def _instantiate_container(proxmox: Any, node: str, payload: Dict[str, Any]) -> Tuple[int, float]:
    from proxmoxer.core import ResourceException

    storage_type = _get_storage_type(proxmox.nodes(node).storage.get(), payload["storage"])
    rootfs = _format_rootfs(payload["storage"], payload["disk_gib"], storage_type)
    vmid = _allocate_vmid(proxmox)
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
            cores=int(payload.get("cpus", 1)),
            cpuunits=int(payload.get("cpuunits", 256)),
            net0=payload["net0"],
            unprivileged=int(payload.get("unprivileged", 1)),
            description=payload.get("description", MANAGED_MARKER),
            password=payload.get("password") or None,
            ssh_public_keys=payload.get("ssh_public_key") or None,
        )
        status, elapsed = _wait_for_task(proxmox, node, upid)
        return vmid, elapsed
    except ResourceException as exc:
        raise RuntimeError(f"Failed to create container: {exc}") from exc


class CreateProxmoxContainerHandler(SyncHandler):
    """Provision a new managed LXC container via the Proxmox API."""

    @property
    def command_name(self) -> str:
        return "create_proxmox_container"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("create_proxmox_container command received")
        request_id = message.get("request_id")
        bootstrap_user, bootstrap_password, bootstrap_ssh_key = _get_provisioning_user_info(message)
        bootstrap_steps = _build_bootstrap_steps(bootstrap_user, bootstrap_password, bootstrap_ssh_key)
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

        def _create_container():
            proxmox = _connect_proxmox(config)
            node = config.get("node") or DEFAULT_NODE_NAME
            payload = _build_container_payload(message, config)
            payload["cpuunits"] = max(int(payload["cpus"] * 1024), 10)
            payload["memory"] = int(payload["ram_mib"])
            payload["node"] = node
            logger.debug(
                "Provisioning container node=%s template=%s ram=%s cpu=%s storage=%s",
                node,
                payload["template"],
                payload["ram_mib"],
                payload["cpus"],
                payload["storage"],
            )
            vmid, _ = _instantiate_container(proxmox, node, payload)
            payload["vmid"] = vmid
            payload["created_at"] = datetime.utcnow().isoformat() + "Z"
            _write_container_record(vmid, payload)
            return proxmox, node, vmid, payload

        proxmox, node, vmid, payload = _run_lifecycle_step(
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
            )

        public_key, steps = _bootstrap_portacode(
            vmid,
            payload["username"],
            payload["password"],
            payload["ssh_public_key"],
            steps=bootstrap_steps,
            progress_callback=_bootstrap_progress_callback,
            start_index=current_step_index,
            total_steps=total_steps,
        )
        current_step_index += len(bootstrap_steps)

        return {
            "event": "proxmox_container_created",
            "success": True,
            "message": f"Container {vmid} is ready and Portacode key captured.",
            "ctid": str(vmid),
            "public_key": public_key,
            "container": {
                "vmid": vmid,
                "hostname": payload["hostname"],
                "template": payload["template"],
                "storage": payload["storage"],
                "disk_gib": payload["disk_gib"],
                "ram_mib": payload["ram_mib"],
                "cpus": payload["cpus"],
            },
            "setup_steps": steps,
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
        )

        cmd = f"su - {user} -c 'sudo -S portacode service install'"
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
        )

        return {
            "event": "proxmox_service_started",
            "success": True,
            "message": "Portacode service install completed",
            "ctid": str(vmid),
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
