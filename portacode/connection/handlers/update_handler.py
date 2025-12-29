"""Update handler for Portacode CLI."""

import subprocess
import sys
import logging
from typing import Any, Dict
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
            
            # Update the package
            result = subprocess.run([
                sys.executable, "-m", "pip", "install", "--upgrade", "portacode"
            ], capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                logger.error("Update failed: %s", result.stderr)
                return {
                    "event": "update_portacode_response",
                    "success": False,
                    "error": f"Update failed: {result.stderr}"
                }
            
            logger.info("Update successful, restarting process...")
            
            # Send success response before exit
            await self.send_response({
                "event": "update_portacode_response",
                "success": True,
                "message": "Update completed. Process restarting..."
            })
            
            # Exit with special code to trigger restart
            sys.exit(42)
            
        except subprocess.TimeoutExpired:
            return {
                "event": "update_portacode_response",
                "success": False,
                "error": "Update timed out after 120 seconds"
            }
        except Exception as e:
            logger.exception("Update failed with exception")
            return {
                "event": "update_portacode_response",
                "success": False,
                "error": str(e)
            }
