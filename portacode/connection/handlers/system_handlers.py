"""System command handlers."""

import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict
from portacode import __version__
import psutil

from .base import SyncHandler

logger = logging.getLogger(__name__)


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
        
        # Collect basic system metrics
        info = {}
        
        try:
            info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            logger.debug("CPU usage: %s%%", info["cpu_percent"])
        except Exception as e:
            logger.warning("Failed to get CPU info: %s", e)
            info["cpu_percent"] = 0.0
            
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
        logger.info("System info collected successfully with OS info: %s", info.get("os_info", {}).get("os_type", "Unknown"))
        
        info["portacode_version"] = __version__

        return {
            "event": "system_info",
            "info": info,
        } 