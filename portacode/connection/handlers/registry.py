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
        logger.info("registry: Dispatching command '%s' with reply_channel=%s", command_name, reply_channel)
        
        handler = self.get_handler(command_name)
        if handler is None:
            logger.warning("registry: No handler found for command: %s", command_name)
            return False
        
        try:
            await handler.handle(message, reply_channel)
            logger.info("registry: Successfully dispatched command '%s'", command_name)
            return True
        except Exception as exc:
            logger.exception("registry: Error dispatching command %s: %s", command_name, exc)
            # Send session-aware error response
            await self._send_session_aware_error(str(exc), reply_channel, message.get("project_id"))
            return False
    
    async def _send_session_aware_error(self, message: str, reply_channel: Optional[str] = None, project_id: str = None) -> None:
        """Send an error response with client session awareness."""
        error_payload = {"event": "error", "message": message}
        
        # Get client session manager from context
        client_session_manager = self.context.get("client_session_manager")
        
        if client_session_manager and client_session_manager.has_interested_clients():
            # Get target sessions
            target_sessions = client_session_manager.get_target_sessions(project_id)
            if not target_sessions:
                logger.debug("registry: No target sessions found, skipping error send")
                return
            
            # Add session targeting information
            error_payload["client_sessions"] = target_sessions
            
            # Add backward compatibility reply_channel (first session if not provided)
            if not reply_channel:
                reply_channel = client_session_manager.get_reply_channel_for_compatibility()
            if reply_channel:
                error_payload["reply_channel"] = reply_channel
            
            logger.debug("registry: Sending error to %d client sessions: %s", 
                        len(target_sessions), target_sessions)
        else:
            # Fallback to original behavior
            if reply_channel:
                error_payload["reply_channel"] = reply_channel
        
        await self.control_channel.send(error_payload)

    def update_context(self, context: Dict[str, Any]) -> None:
        """Update the shared context for all handlers.
        
        Args:
            context: New context dict
        """
        self.context.update(context)
        
        # Update context for all existing handlers
        for handler in self._handlers.values():
            handler.context = self.context 