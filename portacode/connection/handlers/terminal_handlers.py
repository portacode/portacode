"""Terminal command handlers."""

import asyncio
import logging
from typing import Any, Dict, Optional

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
        project_id = message.get("project_id")
        
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        session_info = await session_manager.create_session(shell=shell, cwd=cwd, project_id=project_id)
        
        # Start background watcher for process exit
        asyncio.create_task(self._watch_process_exit(session_info["terminal_id"]))
        
        return {
            "event": "terminal_started",
            "terminal_id": session_info["terminal_id"],
            "channel": session_info["channel"],
            "pid": session_info["pid"],
            "shell": session_info.get("shell"),
            "cwd": session_info.get("cwd"),
            "project_id": session_info.get("project_id"),
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
        
        await self.send_response({
            "event": "terminal_exit",
            "terminal_id": terminal_id,
            "returncode": session.proc.returncode,
            "project_id": session.project_id,
        }, project_id=session.project_id)
        
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
            asyncio.create_task(self._send_not_found_completion(terminal_id, None))
            return {
                "event": "terminal_stopped",
                "terminal_id": terminal_id,
                "status": "not_found",
                "message": "Terminal was not found or already stopped",
                "project_id": None,
            }
        
        logger.info("terminal_stop: Found session for terminal %s (PID: %s), starting background stop process", 
                   terminal_id, getattr(session.proc, 'pid', 'unknown'))
        
        # Start stop process in background without blocking the control channel
        asyncio.create_task(self._stop_session_safely(session, terminal_id, session.project_id))
        
        return {
            "event": "terminal_stopped",
            "terminal_id": terminal_id,
            "status": "stopping",
            "message": "Terminal stop process initiated",
            "project_id": session.project_id,
        }
    
    async def _stop_session_safely(self, session, terminal_id: str, project_id: Optional[str] = None) -> None:
        """Safely stop a session in the background with timeout and error handling."""
        logger.info("terminal_stop: Starting background stop process for terminal %s", terminal_id)
        
        try:
            # Attempt graceful stop with timeout
            await asyncio.wait_for(session.stop(), timeout=10.0)
            logger.info("terminal_stop: Successfully stopped terminal %s", terminal_id)
            
            # Send success notification
            await self.send_response({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "success",
                "message": "Terminal stopped successfully",
                "project_id": project_id,
            }, project_id=project_id)
            
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
            await self.send_response({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "timeout",
                "message": "Terminal stop timed out, process was force killed",
                "project_id": project_id,
            }, project_id=project_id)
            
        except Exception as exc:
            logger.exception("terminal_stop: Error stopping terminal %s: %s", terminal_id, exc)
            
            # Send error notification
            await self.send_response({
                "event": "terminal_stop_completed",
                "terminal_id": terminal_id,
                "status": "error",
                "message": f"Error stopping terminal: {str(exc)}",
                "project_id": project_id,
            }, project_id=project_id)

    async def _send_not_found_completion(self, terminal_id: str, project_id: Optional[str] = None) -> None:
        """Send completion event for not found terminals."""
        await self.send_response({
            "event": "terminal_stop_completed",
            "terminal_id": terminal_id,
            "status": "not_found",
            "message": "Terminal was not found or already stopped",
            "project_id": project_id,
        }, project_id=project_id)


class TerminalListHandler(AsyncHandler):
    """Handler for listing terminal sessions."""
    
    @property
    def command_name(self) -> str:
        return "terminal_list"
    
    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle the command by executing it and sending the response to the requesting client session."""
        logger.info("handler: Processing command %s with reply_channel=%s", 
                   self.command_name, reply_channel)
        
        try:
            response = await self.execute(message)
            logger.info("handler: Command %s executed successfully", self.command_name)
            
            # Get the source client session from the message
            source_client_session = message.get("source_client_session")
            project_id = response.get("project_id")
            
            logger.info("handler: %s response project_id=%s, source_client_session=%s", 
                       self.command_name, project_id, source_client_session)
            
            # Send response only to the requesting client session
            if source_client_session:
                # Add client_sessions field to target only the requesting session
                response["client_sessions"] = [source_client_session]
                await self.control_channel.send(response)
            else:
                # Fallback to original behavior if no source_client_session
                await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("handler: Error in command %s: %s", self.command_name, exc)
            await self.send_error(str(exc), reply_channel, message.get("project_id"))
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """List all terminal sessions."""
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        # Accept project_id argument: None (default) = only no project, 'all' = all, else = filter by project_id
        requested_project_id = message.get("project_id")

        if requested_project_id == "all":
            sessions = session_manager.list_sessions(project_id="all")
        else:
            sessions = session_manager.list_sessions(project_id=requested_project_id)
        
        return {
            "event": "terminal_list",
            "sessions": sessions,
            "project_id": requested_project_id,
        } 