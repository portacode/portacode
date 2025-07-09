"""System command handlers."""

import logging
from pathlib import Path
from typing import Any, Dict

import psutil

from .base import SyncHandler

logger = logging.getLogger(__name__)


class SystemInfoHandler(SyncHandler):
    """Handler for getting system information."""
    
    @property
    def command_name(self) -> str:
        return "system_info"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Get system information."""
        info = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory": psutil.virtual_memory()._asdict(),
            "disk": psutil.disk_usage(str(Path.home()))._asdict(),
        }
        
        return {
            "event": "system_info",
            "info": info,
        } 