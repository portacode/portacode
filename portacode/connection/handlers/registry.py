"""Command registry for managing handler dispatch."""

import logging
from typing import Dict, Type, Any, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..multiplex import Channel
    from .base import BaseHandler

logger = logging.getLogger(__name__)


class CommandRegistry:
    """Registry for managing command handlers."""
    
    def __init__(self, control_channel: "Channel", context: Dict[str, Any]):
        """Initialize the command registry.
        
        Args:
            control_channel: The control channel for handlers
            context: Shared context for handlers
        """
        self.control_channel = control_channel
        self.context = context
        self._handlers: Dict[str, "BaseHandler"] = {}
        
    def register(self, handler_class: Type["BaseHandler"]) -> None:
        """Register a command handler.
        
        Args:
            handler_class: The handler class to register
        """
        handler_instance = handler_class(self.control_channel, self.context)
        command_name = handler_instance.command_name
        
        if command_name in self._handlers:
            logger.warning("Overriding existing handler for command: %s", command_name)
        
        self._handlers[command_name] = handler_instance
        logger.debug("Registered handler for command: %s", command_name)
        
    def unregister(self, command_name: str) -> None:
        """Unregister a command handler.
        
        Args:
            command_name: The command name to unregister
        """
        if command_name in self._handlers:
            del self._handlers[command_name]
            logger.debug("Unregistered handler for command: %s", command_name)
        else:
            logger.warning("Attempted to unregister non-existent handler: %s", command_name)
    
    def get_handler(self, command_name: str) -> Optional["BaseHandler"]:
        """Get a handler by command name.
        
        Args:
            command_name: The command name
            
        Returns:
            The handler instance or None if not found
        """
        return self._handlers.get(command_name)
    
    def list_commands(self) -> List[str]:
        """List all registered command names.
        
        Returns:
            List of command names
        """
        return list(self._handlers.keys())
    
    async def dispatch(self, command_name: str, message: Dict[str, Any], reply_channel: Optional[str] = None) -> bool:
        """Dispatch a command to its handler.
        
        Args:
            command_name: The command name
            message: The command message
            reply_channel: Optional reply channel
            
        Returns:
            True if handler was found and executed, False otherwise
        """
        handler = self.get_handler(command_name)
        if handler is None:
            logger.warning("No handler found for command: %s", command_name)
            return False
        
        try:
            await handler.handle(message, reply_channel)
            return True
        except Exception as exc:
            logger.exception("Error dispatching command %s: %s", command_name, exc)
            # Send error response
            error_payload = {"event": "error", "message": str(exc)}
            if reply_channel:
                error_payload["reply_channel"] = reply_channel
            await self.control_channel.send(error_payload)
            return False
    
    def update_context(self, context: Dict[str, Any]) -> None:
        """Update the shared context for all handlers.
        
        Args:
            context: New context dict
        """
        self.context.update(context)
        
        # Update context for all existing handlers
        for handler in self._handlers.values():
            handler.context = self.context 