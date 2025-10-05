"""Base handler classes for command processing."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING
from portacode.utils.ntp_clock import ntp_clock

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
        # Add device_send timestamp if trace present
        if "trace" in payload and "request_id" in payload:
            device_send_time = ntp_clock.now_ms()
            if device_send_time is not None:
                payload["trace"]["device_send"] = device_send_time
                # Update ping to show total time from client_send
                if "client_send" in payload["trace"]:
                    payload["trace"]["ping"] = device_send_time - payload["trace"]["client_send"]
                logger.info(f"📤 Device sending traced response: {payload['request_id']}")

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

        # Add handler_dispatch timestamp if trace present
        if "trace" in message and "request_id" in message:
            handler_dispatch_time = ntp_clock.now_ms()
            if handler_dispatch_time is not None:
                message["trace"]["handler_dispatch"] = handler_dispatch_time
                # Update ping to show total time from client_send
                if "client_send" in message["trace"]:
                    message["trace"]["ping"] = handler_dispatch_time - message["trace"]["client_send"]
                logger.info(f"🔧 Handler dispatching: {message['request_id']} ({self.command_name})")

        try:
            response = await self.execute(message)
            logger.info("handler: Command %s executed successfully", self.command_name)

            # Handle cases where execute() sends responses directly and returns None
            if response is not None:
                # Automatically copy request_id if present in the incoming message
                if "request_id" in message and "request_id" not in response:
                    response["request_id"] = message["request_id"]

                # Pass through trace from request to response (add to existing trace, don't create new one)
                if "trace" in message and "request_id" in message:
                    response["trace"] = dict(message["trace"])
                    handler_complete_time = ntp_clock.now_ms()
                    if handler_complete_time is not None:
                        response["trace"]["handler_complete"] = handler_complete_time
                        # Update ping to show total time from client_send
                        if "client_send" in response["trace"]:
                            response["trace"]["ping"] = handler_complete_time - response["trace"]["client_send"]
                        logger.info(f"✅ Handler completed: {message['request_id']} ({self.command_name})")

                # Extract project_id from response for session targeting
                project_id = response.get("project_id")
                logger.info("handler: %s response project_id=%s, response=%s",
                           self.command_name, project_id, response)
                await self.send_response(response, reply_channel, project_id)
            else:
                logger.info("handler: %s handled response transmission directly", self.command_name)
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
        # Add handler_dispatch timestamp if trace present
        if "trace" in message and "request_id" in message:
            handler_dispatch_time = ntp_clock.now_ms()
            if handler_dispatch_time is not None:
                message["trace"]["handler_dispatch"] = handler_dispatch_time
                # Update ping to show total time from client_send
                if "client_send" in message["trace"]:
                    message["trace"]["ping"] = handler_dispatch_time - message["trace"]["client_send"]
                logger.info(f"🔧 Handler dispatching: {message['request_id']} ({self.command_name})")

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, self.execute, message)

            # Automatically copy request_id if present in the incoming message
            if "request_id" in message and "request_id" not in response:
                response["request_id"] = message["request_id"]

            # Pass through trace from request to response (add to existing trace, don't create new one)
            if "trace" in message and "request_id" in message:
                response["trace"] = dict(message["trace"])
                handler_complete_time = ntp_clock.now_ms()
                if handler_complete_time is not None:
                    response["trace"]["handler_complete"] = handler_complete_time
                    logger.info(f"✅ Handler completed: {message['request_id']} ({self.command_name})")

            # Extract project_id from response for session targeting
            project_id = response.get("project_id")
            await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("Error in sync handler %s: %s", self.command_name, exc)
            # Extract project_id from original message for error targeting
            project_id = message.get("project_id")
            await self.send_error(str(exc), reply_channel, project_id) 