"""Terminal command handlers."""

import asyncio
import logging
from typing import Any, Dict

from .base import AsyncHandler
from .session import SessionManager

logger = logging.getLogger(__name__)


class TerminalStartHandler(AsyncHandler):
    """Handler for starting new terminal sessions."""
    
    @property
    def command_name(self) -> str:
        return "terminal_start"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Start a new terminal session."""
        shell = message.get("shell")
        cwd = message.get("cwd")
        
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        session_info = await session_manager.create_session(shell=shell, cwd=cwd)
        
        # Start background watcher for process exit
        asyncio.create_task(self._watch_process_exit(session_info["terminal_id"]))
        
        return {
            "event": "terminal_started",
            "terminal_id": session_info["terminal_id"],
            "channel": session_info["channel"],
        }
    
    async def _watch_process_exit(self, terminal_id: str) -> None:
        """Watch for process exit and send notification."""
        session_manager = self.context.get("session_manager")
        if not session_manager:
            return
        
        session = session_manager.get_session(terminal_id)
        if not session:
            return
        
        await session.proc.wait()
        
        await self.control_channel.send({
            "event": "terminal_exit",
            "terminal_id": terminal_id,
            "returncode": session.proc.returncode,
        })
        
        # Only cleanup session if it still exists (not already removed by stop handler)
        if session_manager.get_session(terminal_id):
            session_manager.remove_session(terminal_id)


class TerminalSendHandler(AsyncHandler):
    """Handler for sending data to terminal sessions."""
    
    @property
    def command_name(self) -> str:
        return "terminal_send"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Send data to a terminal session."""
        terminal_id = message.get("terminal_id")
        data = message.get("data", "")
        
        if not terminal_id:
            raise ValueError("terminal_id is required")
        
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        session = session_manager.get_session(terminal_id)
        if not session:
            raise ValueError(f"terminal_id {terminal_id} not found")
        
        await session.write(data)
        
        # No response expected for terminal_send
        return {"event": "terminal_send_ack"}


class TerminalStopHandler(AsyncHandler):
    """Handler for stopping terminal sessions."""
    
    @property
    def command_name(self) -> str:
        return "terminal_stop"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Stop a terminal session."""
        terminal_id = message.get("terminal_id")
        
        if not terminal_id:
            logger.error("terminal_stop: Missing terminal_id in message")
            raise ValueError("terminal_id is required")
        
        logger.info("terminal_stop: Processing stop request for terminal_id=%s", terminal_id)
        
        session_manager = self.context.get("session_manager")
        if not session_manager:
            logger.error("terminal_stop: Session manager not available in context")
            raise RuntimeError("Session manager not available")
        
        # Remove session from manager first
        session = session_manager.remove_session(terminal_id)
        if not session:
            logger.warning("terminal_stop: Terminal %s not found, may have already been stopped", terminal_id)
            # Send completion event immediately for not found terminals
            asyncio.create_task(self._send_not_found_completion(terminal_id))
            return {
                "event": "terminal_stopped",
                "terminal_id": terminal_id,
                "status": "not_found",
                "message": "Terminal was not found or already stopped"
            }
        
        logger.info("terminal_stop: Found session for terminal %s (PID: %s), starting background stop process", 
                   terminal_id, getattr(session.proc, 'pid', 'unknown'))
        
        # Start stop process in background without blocking the control channel
        asyncio.create_task(self._stop_session_safely(session, terminal_id))
        
        return {
            "event": "terminal_stopped",
            "terminal_id": terminal_id,
            "status": "stopping",
            "message": "Terminal stop process initiated"
        }
    
    async def _stop_session_safely(self, session, terminal_id: str) -> None:
        """Safely stop a session in the background with timeout and error handling."""
        logger.info("terminal_stop: Starting background stop process for terminal %s", terminal_id)
        
        try:
            # Attempt graceful stop with timeout
            await asyncio.wait_for(session.stop(), timeout=10.0)
            logger.info("terminal_stop: Successfully stopped terminal %s", terminal_id)
            
            # Send success notification
            await self.control_channel.send({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "success",
                "message": "Terminal stopped successfully"
            })
            
        except asyncio.TimeoutError:
            logger.warning("terminal_stop: Stop timeout for terminal %s, forcing kill", terminal_id)
            
            # Force kill the process
            try:
                if hasattr(session.proc, 'kill'):
                    session.proc.kill()
                    logger.info("terminal_stop: Force killed terminal %s", terminal_id)
                elif hasattr(session.proc, 'terminate'):
                    session.proc.terminate()
                    logger.info("terminal_stop: Force terminated terminal %s", terminal_id)
            except Exception as kill_exc:
                logger.error("terminal_stop: Failed to force kill terminal %s: %s", terminal_id, kill_exc)
            
            # Send timeout notification
            await self.control_channel.send({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "timeout",
                "message": "Terminal stop timed out, process was force killed"
            })
            
        except Exception as exc:
            logger.exception("terminal_stop: Error stopping terminal %s: %s", terminal_id, exc)
            
            # Send error notification
            await self.control_channel.send({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "error",
                "message": f"Error stopping terminal: {str(exc)}"
            })

    async def _send_not_found_completion(self, terminal_id: str) -> None:
        """Send completion event for not found terminals."""
        await self.control_channel.send({
            "event": "terminal_stop_completed",
            "terminal_id": terminal_id,
            "status": "not_found",
            "message": "Terminal was not found or already stopped"
        })


class TerminalListHandler(AsyncHandler):
    """Handler for listing terminal sessions."""
    
    @property
    def command_name(self) -> str:
        return "terminal_list"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """List all terminal sessions."""
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        sessions = session_manager.list_sessions()
        
        return {
            "event": "terminal_list",
            "sessions": sessions,
        } 