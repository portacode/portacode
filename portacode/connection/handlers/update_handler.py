"""Update handler for Portacode CLI."""

import logging
from typing import Any, Dict, Optional

from portacode.updater import build_pip_install_command, run_pip_install_command
from .base import AsyncHandler

logger = logging.getLogger(__name__)


class UpdatePortacodeHandler(AsyncHandler):
    """Handler for updating Portacode CLI."""

    @property
    def command_name(self) -> str:
        return "update_portacode_cli"

    async def execute(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update Portacode package and restart process."""
        try:
            logger.info("Starting Portacode CLI update...")
            pip_cmd = build_pip_install_command()
            result = run_pip_install_command(
                pip_cmd,
                allow_sudo_fallback=True,
                interactive_sudo=False,
            )

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

            from portacode.restart import request_restart

            # Consistent restart behavior across CLI and websocket updates.
            # In-service context: prefer supervisor restart (or best-effort service restart).
            request_restart(method="auto", in_service=True)
            return None

        except Exception as e:
            logger.exception("Update failed with exception")
            return {
                "event": "update_portacode_response",
                "success": False,
                "error": str(e),
            }
