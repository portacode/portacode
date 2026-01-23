"""System command handlers."""
from __future__ import annotations

import concurrent.futures
import getpass
import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from portacode import __version__
import psutil

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - py<3.8
    import importlib_metadata

from .base import SyncHandler
from .proxmox_infra import get_infra_snapshot

logger = logging.getLogger(__name__)

# Global CPU monitoring
_cpu_percent = 0.0
_cpu_thread = None
_cpu_lock = threading.Lock()

# Cgroup v2 tracking
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_cgroup_path: Optional[Path] = None
_cgroup_v2_supported: Optional[bool] = None
_CGROUP_CPU_STAT = "cpu.stat"
_CGROUP_CPU_MAX = "cpu.max"
_last_cgroup_usage: Optional[int] = None
_last_cgroup_time: Optional[float] = None

def _cpu_monitor():
    """Background thread to update CPU usage every 5 seconds."""
    global _cpu_percent
    while True:
        percent = _get_cgroup_cpu_percent()
        if percent is None:
            # Fall back to psutil when cgroup stats are not available yet.
            percent = psutil.cpu_percent(interval=5.0)
        else:
            time.sleep(5.0)
        _cpu_percent = percent

def _ensure_cpu_thread():
    """Ensure CPU monitoring thread is running (singleton)."""
    global _cpu_thread
    with _cpu_lock:
        if _cpu_thread is None or not _cpu_thread.is_alive():
            _cpu_thread = threading.Thread(target=_cpu_monitor, daemon=True)
            _cpu_thread.start()


def _get_user_context() -> Dict[str, Any]:
    """Gather current CLI user plus permission hints."""
    context = {}
    login_source = "os.getlogin"
    try:
        username = os.getlogin()
    except Exception:
        login_source = "getpass"
        username = getpass.getuser()

    context["username"] = username
    context["username_source"] = login_source
    context["home"] = str(Path.home())

    uid = getattr(os, "getuid", None)
    euid = getattr(os, "geteuid", None)
    context["uid"] = uid() if uid else None
    context["euid"] = euid() if euid else context["uid"]
    if os.name == "nt":
        try:
            import ctypes

            context["is_root"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            context["is_root"] = None
    else:
        context["is_root"] = context["euid"] == 0 if context["euid"] is not None else False

    context["has_sudo"] = shutil.which("sudo") is not None
    context["sudo_user"] = os.environ.get("SUDO_USER")
    context["is_sudo_session"] = bool(os.environ.get("SUDO_UID"))
    return context


def _get_playwright_info() -> Dict[str, Any]:
    """Return Playwright presence, version, and browser binaries if available."""
    result: Dict[str, Any] = {
        "installed": False,
        "version": None,
        "browsers": {},
        "error": None,
    }

    if importlib.util.find_spec("playwright") is None:
        return result

    result["installed"] = True
    try:
        result["version"] = importlib_metadata.version("playwright")
    except Exception as exc:
        logger.debug("Unable to read Playwright version metadata: %s", exc)

    def _inspect_browsers() -> Dict[str, Any]:
        from playwright.sync_api import sync_playwright

        browsers_data: Dict[str, Any] = {}
        with sync_playwright() as p:
            for name in ("chromium", "firefox", "webkit"):
                browser_type = getattr(p, name, None)
                if browser_type is None:
                    continue
                exec_path = getattr(browser_type, "executable_path", None)
                browsers_data[name] = {
                    "available": bool(exec_path),
                    "executable_path": exec_path,
                }
        return browsers_data

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_inspect_browsers)
            browsers = future.result(timeout=5)
            result["browsers"] = browsers
    except concurrent.futures.TimeoutError:
        msg = "Playwright inspection timed out"
        logger.warning(msg)
        result["error"] = msg
    except Exception as exc:
        logger.warning("Playwright browser inspection failed: %s", exc)
        result["error"] = str(exc)

    return result


def _resolve_cgroup_path() -> Path:
    global _cgroup_path
    if _cgroup_path is not None and _cgroup_path.exists():
        return _cgroup_path
    path = _CGROUP_ROOT
    cgroup_file = Path("/proc/self/cgroup")
    if cgroup_file.exists():
        try:
            contents = cgroup_file.read_text()
        except OSError:
            pass
        else:
            for line in contents.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                rel_path = parts[-1].lstrip("/")
                candidate = _CGROUP_ROOT / rel_path
                # Fallback to root path if the relative path is empty
                candidate = candidate if rel_path else _CGROUP_ROOT
                if candidate.exists():
                    path = candidate
                    break
    _cgroup_path = path
    return _cgroup_path


def _cgroup_file(name: str) -> Path:
    return _resolve_cgroup_path() / name


def _detect_cgroup_v2() -> bool:
    global _cgroup_v2_supported
    if _cgroup_v2_supported is not None:
        return _cgroup_v2_supported
    controllers = _cgroup_file("cgroup.controllers")
    cpu_stat = _cgroup_file(_CGROUP_CPU_STAT)
    _cgroup_v2_supported = controllers.exists() and cpu_stat.exists()
    return _cgroup_v2_supported


def _read_cgroup_cpu_usage() -> Optional[int]:
    path = _cgroup_file(_CGROUP_CPU_STAT)
    try:
        data = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for line in data.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "usage_usec":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _read_cgroup_cpu_limit() -> Optional[float]:
    """Return the allowed CPU (cores) for this cgroup, if limited."""
    path = _cgroup_file(_CGROUP_CPU_MAX)
    if not path.exists():
        return None
    try:
        data = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    parts = data.split()
    if len(parts) < 2:
        return None
    quota, period = parts[0], parts[1]
    if quota == "max":
        return None
    try:
        quota_value = int(quota)
        period_value = int(period)
    except ValueError:
        return None
    if period_value <= 0:
        return None
    return quota_value / period_value


def _get_cgroup_cpu_percent() -> Optional[float]:
    if not _detect_cgroup_v2():
        return None
    usage = _read_cgroup_cpu_usage()
    if usage is None:
        return None
    now = time.monotonic()
    global _last_cgroup_usage, _last_cgroup_time
    prev_usage = _last_cgroup_usage
    prev_time = _last_cgroup_time
    _last_cgroup_usage = usage
    _last_cgroup_time = now
    if prev_usage is None or prev_time is None:
        return None
    delta_usage = usage - prev_usage
    delta_time = now - prev_time
    if delta_time <= 0 or delta_usage < 0:
        return None
    cpu_ratio = (delta_usage / 1_000_000) / delta_time
    limit_cpus = _read_cgroup_cpu_limit()
    if limit_cpus and limit_cpus > 0:
        percent = (cpu_ratio / limit_cpus) * 100.0
    else:
        cpu_count = psutil.cpu_count(logical=True) or 1
        percent = (cpu_ratio / cpu_count) * 100.0
    return max(0.0, min(percent, 100.0))


def _run_probe_command(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def _parse_pveversion(output: str) -> str | None:
    first_token = output.split(None, 1)[0] if output else ""
    if not first_token:
        return None
    if "/" in first_token:
        return first_token.split("/", 1)[1]
    return first_token


def _parse_dpkg_version(output: str) -> str | None:
    for line in output.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _get_proxmox_version() -> str | None:
    release_file = Path("/etc/proxmox-release")
    if release_file.exists():
        try:
            return release_file.read_text().strip()
        except Exception:
            pass
    value = _run_probe_command(["pveversion"])
    parsed = _parse_pveversion(value or "")
    if parsed:
        return parsed
    for pkg in ("pve-manager", "proxmox-ve"):
        pkg_output = _run_probe_command(["dpkg", "-s", pkg])
        parsed = _parse_dpkg_version(pkg_output or "")
        if parsed:
            return parsed
    return None


def _get_proxmox_info() -> Dict[str, Any]:
    """Detect if the current host is a Proxmox node."""
    info: Dict[str, Any] = {"is_proxmox_node": False, "version": None}
    if Path("/etc/proxmox-release").exists() or Path("/etc/pve").exists():
        info["is_proxmox_node"] = True
    version = _get_proxmox_version()
    if version:
        info["version"] = version
    info["infra"] = get_infra_snapshot()
    return info


def _get_os_info() -> Dict[str, Any]:
    """Get operating system information with robust error handling."""
    try:
        system = platform.system()
        logger.debug("Detected system: %s", system)
        
        if system == "Linux":
            os_type = "Linux"
            default_shell = os.environ.get('SHELL', '/bin/bash')
            default_cwd = os.path.expanduser('~')
            
            # Try to get more specific Linux distribution info
            try:
                import distro
                os_version = f"{distro.name()} {distro.version()}"
                logger.debug("Using distro package for OS version: %s", os_version)
            except ImportError:
                logger.debug("distro package not available, trying /etc/os-release")
                # Fallback to basic platform info
                try:
                    with open('/etc/os-release', 'r') as f:
                        for line in f:
                            if line.startswith('PRETTY_NAME='):
                                os_version = line.split('=')[1].strip().strip('"')
                                logger.debug("Found OS version from /etc/os-release: %s", os_version)
                                break
                        else:
                            os_version = f"{system} {platform.release()}"
                            logger.debug("Using platform.release() for OS version: %s", os_version)
                except FileNotFoundError:
                    os_version = f"{system} {platform.release()}"
                    logger.debug("Using platform.release() fallback for OS version: %s", os_version)
                    
        elif system == "Darwin":  # macOS
            os_type = "macOS"
            os_version = f"macOS {platform.mac_ver()[0]}"
            default_shell = os.environ.get('SHELL', '/bin/bash')
            default_cwd = os.path.expanduser('~')
            
        elif system == "Windows":
            os_type = "Windows"
            os_version = f"{platform.system()} {platform.release()}"
            default_shell = os.environ.get('COMSPEC', 'cmd.exe')
            default_cwd = os.path.expanduser('~')
            
        else:
            os_type = system
            os_version = f"{system} {platform.release()}"
            default_shell = "/bin/sh"  # Safe fallback
            default_cwd = os.path.expanduser('~')
        
        result = {
            "os_type": os_type,
            "os_version": os_version,
            "architecture": platform.machine(),
            "default_shell": default_shell,
            "default_cwd": default_cwd,
        }
        
        logger.debug("Successfully collected OS info: %s", result)
        return result
        
    except Exception as e:
        logger.error("Failed to collect OS info: %s", e, exc_info=True)
        # Return minimal fallback info instead of failing completely
        return {
            "os_type": "Unknown",
            "os_version": "Unknown",
            "architecture": platform.machine() if hasattr(platform, 'machine') else "Unknown",
            "default_shell": "/bin/bash",  # Safe fallback
            "default_cwd": os.path.expanduser('~') if hasattr(os.path, 'expanduser') else "",
        }


class SystemInfoHandler(SyncHandler):
    """Handler for getting system information."""
    
    @property
    def command_name(self) -> str:
        return "system_info"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Get system information including OS details."""
        logger.debug("Collecting system information...")
        
        # Ensure CPU monitoring thread is running
        _ensure_cpu_thread()
        
        # Collect basic system metrics
        info = {}
        
        info["cpu_percent"] = _cpu_percent
            
        try:
            info["memory"] = psutil.virtual_memory()._asdict()
            logger.debug("Memory usage: %s%%", info["memory"].get("percent", "N/A"))
        except Exception as e:
            logger.warning("Failed to get memory info: %s", e)
            info["memory"] = {"percent": 0.0}
            
        try:
            info["disk"] = psutil.disk_usage(str(Path.home()))._asdict()
            logger.debug("Disk usage: %s%%", info["disk"].get("percent", "N/A"))
        except Exception as e:
            logger.warning("Failed to get disk info: %s", e)
            info["disk"] = {"percent": 0.0}
        
        # Add OS information - this is critical for proper shell detection
        info["os_info"] = _get_os_info()
        info["user_context"] = _get_user_context()
        info["playwright"] = _get_playwright_info()
        info["proxmox"] = _get_proxmox_info()
        # logger.info("System info collected successfully with OS info: %s", info.get("os_info", {}).get("os_type", "Unknown"))
        
        info["portacode_version"] = __version__

        return {
            "event": "system_info",
            "info": info,
        } 
