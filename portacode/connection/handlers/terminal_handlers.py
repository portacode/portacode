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
        
        # Cleanup session
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
            raise ValueError("terminal_id is required")
        
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        session = session_manager.remove_session(terminal_id)
        if not session:
            raise ValueError(f"terminal_id {terminal_id} not found")
        
        await session.stop()
        
        return {
            "event": "terminal_stopped",
            "terminal_id": terminal_id,
        }


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