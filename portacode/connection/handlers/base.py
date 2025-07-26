"""Base handler classes for command processing."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..multiplex import Channel

logger = logging.getLogger(__name__)


class BaseHandler(ABC):
    """Base class for all command handlers."""
    
    def __init__(self, control_channel: "Channel", context: Dict[str, Any]):
        """Initialize the handler.
        
        Args:
            control_channel: The control channel for sending responses
            context: Shared context containing terminal manager state
        """
        self.control_channel = control_channel
        self.context = context
        
    @property
    @abstractmethod
    def command_name(self) -> str:
        """Return the command name this handler processes."""
        pass
    
    @abstractmethod
    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle the command message.
        
        Args:
            message: The command message dict
            reply_channel: Optional reply channel for responses
        """
        pass
    
    async def send_response(self, payload: Dict[str, Any], reply_channel: Optional[str] = None, project_id: str = None) -> None:
        """Send a response back to the gateway with client session awareness.
        
        Args:
            payload: Response payload
            reply_channel: Optional reply channel for backward compatibility
            project_id: Optional project filter for targeting specific sessions
        """
        # Get client session manager from context
        client_session_manager = self.context.get("client_session_manager")
        
        if client_session_manager and client_session_manager.has_interested_clients():
            # Get target sessions
            target_sessions = client_session_manager.get_target_sessions(project_id)
            if not target_sessions:
                logger.debug("handler: No target sessions found, skipping response send")
                return
            
            # Add session targeting information
            enhanced_payload = dict(payload)
            enhanced_payload["client_sessions"] = target_sessions
            
            # Add backward compatibility reply_channel (first session if not provided)
            if not reply_channel:
                reply_channel = client_session_manager.get_reply_channel_for_compatibility()
            if reply_channel:
                enhanced_payload["reply_channel"] = reply_channel
            
            logger.debug("handler: Sending response to %d client sessions: %s", 
                        len(target_sessions), target_sessions)
            
            await self.control_channel.send(enhanced_payload)
        else:
            # Fallback to original behavior if no client session manager or no clients
            if reply_channel:
                payload["reply_channel"] = reply_channel
            await self.control_channel.send(payload)
    
    async def send_error(self, message: str, reply_channel: Optional[str] = None, project_id: str = None) -> None:
        """Send an error response with client session awareness.
        
        Args:
            message: Error message
            reply_channel: Optional reply channel for backward compatibility
            project_id: Optional project filter for targeting specific sessions
        """
        payload = {"event": "error", "message": message}
        await self.send_response(payload, reply_channel, project_id)


class AsyncHandler(BaseHandler):
    """Base class for asynchronous command handlers."""
    
    @abstractmethod
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the command logic asynchronously.
        
        Args:
            message: The command message dict
            
        Returns:
            Response payload dict
        """
        pass
    
    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle the command by executing it and sending the response."""
        logger.info("handler: Processing command %s with reply_channel=%s", 
                   self.command_name, reply_channel)
        
        try:
            response = await self.execute(message)
            logger.info("handler: Command %s executed successfully", self.command_name)
            
            # Extract project_id from response for session targeting
            project_id = response.get("project_id")
            logger.info("handler: %s response project_id=%s, response=%s", 
                       self.command_name, project_id, response)
            await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("handler: Error in async handler %s: %s", self.command_name, exc)
            # Extract project_id from original message for error targeting
            project_id = message.get("project_id")
            await self.send_error(str(exc), reply_channel, project_id)


class SyncHandler(BaseHandler):
    """Base class for synchronous command handlers."""
    
    @abstractmethod
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the command logic synchronously.
        
        Args:
            message: The command message dict
            
        Returns:
            Response payload dict
        """
        pass
    
    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle the command by executing it in an executor and sending the response."""
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, self.execute, message)
            
            # Extract project_id from response for session targeting
            project_id = response.get("project_id")
            await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("Error in sync handler %s: %s", self.command_name, exc)
            # Extract project_id from original message for error targeting
            project_id = message.get("project_id")
            await self.send_error(str(exc), reply_channel, project_id) 