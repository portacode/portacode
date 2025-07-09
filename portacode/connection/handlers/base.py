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
    
    async def send_response(self, payload: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Send a response back to the gateway.
        
        Args:
            payload: Response payload
            reply_channel: Optional reply channel
        """
        if reply_channel:
            payload["reply_channel"] = reply_channel
        await self.control_channel.send(payload)
    
    async def send_error(self, message: str, reply_channel: Optional[str] = None) -> None:
        """Send an error response.
        
        Args:
            message: Error message
            reply_channel: Optional reply channel
        """
        payload = {"event": "error", "message": message}
        if reply_channel:
            payload["reply_channel"] = reply_channel
        await self.control_channel.send(payload)


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
        try:
            response = await self.execute(message)
            await self.send_response(response, reply_channel)
        except Exception as exc:
            logger.exception("Error in async handler %s: %s", self.command_name, exc)
            await self.send_error(str(exc), reply_channel)


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
            await self.send_response(response, reply_channel)
        except Exception as exc:
            logger.exception("Error in sync handler %s: %s", self.command_name, exc)
            await self.send_error(str(exc), reply_channel) 