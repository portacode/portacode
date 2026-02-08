"""Update handler for Portacode CLI."""

import logging
import subprocess
import sys
from typing import Any, Dict

from portacode.updater import build_pip_install_command
from .base import AsyncHandler

logger = logging.getLogger(__name__)


class UpdatePortacodeHandler(AsyncHandler):
    """Handler for updating Portacode CLI."""

    @property
    def command_name(self) -> str:
        return "update_portacode_cli"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Update Portacode package and restart process."""
        try:
            logger.info("Starting Portacode CLI update...")
            pip_cmd = build_pip_install_command()
            result = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                logger.error("Update failed: %s", error_msg)
                return {
                    "event": "update_portacode_response",
                    "success": False,
                    "error": f"Update failed: {error_msg}",
                }

            logger.info("Update successful, restarting process...")

            await self.send_response(
                {
                    "event": "update_portacode_response",
                    "success": True,
                    "message": "Update completed. Process restarting...",
                }
            )

            sys.exit(42)

        except subprocess.TimeoutExpired:
            return {
                "event": "update_portacode_response",
                "success": False,
                "error": "Update timed out after 120 seconds",
            }
        except Exception as e:
            logger.exception("Update failed with exception")
            return {
                "event": "update_portacode_response",
                "success": False,
                "error": str(e),
            }
