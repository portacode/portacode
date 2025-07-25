from __future__ import annotations

"""Terminal session management for Portacode client.

This module provides a modular command handling system for the Portacode gateway.
Commands are processed through a registry system that allows for easy extension
and modification without changing the core terminal manager.

The system uses a **control channel 0** for JSON commands and responses, with
dedicated channels for terminal I/O streams.

For detailed information about adding new handlers, see the README.md file
in the handlers directory.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, List

from .multiplex import Multiplexer, Channel
from .handlers import (
    CommandRegistry,
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
    SystemInfoHandler,
    DirectoryListHandler,
)
from .handlers.session import SessionManager

logger = logging.getLogger(__name__)

class ClientSessionManager:
    """Manages connected client sessions for the device."""
    
    def __init__(self):
        self._client_sessions = {}
        self._debug_file_path = os.path.join(os.getcwd(), "client_sessions.json")
        logger.info("ClientSessionManager initialized")
    
    def update_sessions(self, sessions: List[Dict]) -> None:
        """Update the client sessions with new data from server."""
        self._client_sessions = {}
        for session in sessions:
            channel_name = session.get("channel_name")
            if channel_name:
                self._client_sessions[channel_name] = session
        
        logger.info(f"Updated client sessions: {len(self._client_sessions)} sessions")
        self._write_debug_file()
    
    def get_sessions(self) -> Dict[str, Dict]:
        """Get all current client sessions."""
        return self._client_sessions.copy()
    
    def get_session_by_channel(self, channel_name: str) -> Optional[Dict]:
        """Get a specific client session by channel name."""
        return self._client_sessions.get(channel_name)
    
    def get_sessions_for_project(self, project_id: str) -> List[Dict]:
        """Get all client sessions for a specific project."""
        return [
            session for session in self._client_sessions.values()
            if session.get("project_id") == project_id
        ]
    
    def get_sessions_for_user(self, user_id: int) -> List[Dict]:
        """Get all client sessions for a specific user."""
        return [
            session for session in self._client_sessions.values()
            if session.get("user_id") == user_id
        ]
    
    def has_interested_clients(self) -> bool:
        """Check if there are any connected clients interested in this device."""
        return len(self._client_sessions) > 0
    
    def get_target_sessions(self, project_id: str = None) -> List[str]:
        """Get list of channel_names for target client sessions.
        
        Args:
            project_id: If specified, only include sessions for this project
            
        Returns:
            List of channel_names to target
        """
        if not self._client_sessions:
            return []
        
        target_sessions = []
        for session in self._client_sessions.values():
            # If project_id specified, filter by project
            if project_id and session.get("project_id") != project_id:
                continue
            target_sessions.append(session.get("channel_name"))
        
        return [s for s in target_sessions if s]  # Filter out None values
    
    def get_reply_channel_for_compatibility(self) -> Optional[str]:
        """Get the first session's channel_name for backward compatibility.
        
        Returns:
            First available channel_name or None
        """
        if not self._client_sessions:
            return None
        return next(iter(self._client_sessions.keys()), None)
    
    def _write_debug_file(self) -> None:
        """Write current client sessions to debug JSON file."""
        try:
            with open(self._debug_file_path, 'w') as f:
                json.dump(list(self._client_sessions.values()), f, indent=2, default=str)
            logger.debug(f"Updated client sessions debug file: {self._debug_file_path}")
        except Exception as e:
            logger.error(f"Failed to write client sessions debug file: {e}")

__all__ = [
    "TerminalManager",
    "ClientSessionManager",
]

class TerminalManager:
    """Manage command processing through a modular handler system."""

    CONTROL_CHANNEL_ID = 0  # messages with JSON commands/events

    def __init__(self, mux: Multiplexer):
        self.mux = mux
        self._session_manager = None  # Initialize as None first
        self._client_session_manager = ClientSessionManager()  # Initialize client session manager
        self._set_mux(mux, is_initial=True)

    # ------------------------------------------------------------------
    # Mux attach/detach helpers (for reconnection resilience)
    # ------------------------------------------------------------------

    def attach_mux(self, mux: Multiplexer) -> None:
        """Attach a *new* Multiplexer after a reconnect, re-binding channels."""
        old_session_manager = self._session_manager
        
        # Set up new mux but preserve existing session manager
        self._set_mux(mux, is_initial=False)
        
        # Re-attach sessions to new mux if we had existing sessions
        if old_session_manager and old_session_manager._sessions:
            logger.info("Preserving %d terminal sessions across reconnection", len(old_session_manager._sessions))
            # Transfer sessions from old manager to new manager
            self._session_manager._sessions = old_session_manager._sessions
            # Start async reattachment and reconciliation
            asyncio.create_task(self._handle_reconnection())
        else:
            # No existing sessions, send empty terminal list and request client sessions
            asyncio.create_task(self._initial_connection_setup())

    def _set_mux(self, mux: Multiplexer, is_initial: bool = False) -> None:
        self.mux = mux
        self._control_channel = self.mux.get_channel(self.CONTROL_CHANNEL_ID)
        
        # Only create new session manager on initial setup, preserve existing one on reconnection
        if is_initial or self._session_manager is None:
            self._session_manager = SessionManager(mux)
            logger.info("Created new SessionManager")
        else:
            # Update existing session manager's mux reference
            self._session_manager.mux = mux
            logger.info("Preserved existing SessionManager with %d sessions", len(self._session_manager._sessions))
        
        # Create context for handlers
        self._context = {
            "session_manager": self._session_manager,
            "client_session_manager": self._client_session_manager,
            "mux": mux,
        }
        
        # Initialize command registry
        self._command_registry = CommandRegistry(self._control_channel, self._context)
        
        # Register default handlers
        self._register_default_handlers()
        
        # Start control loop task
        if getattr(self, "_ctl_task", None):
            try:
                self._ctl_task.cancel()
            except Exception:
                pass
        self._ctl_task = asyncio.create_task(self._control_loop())
        
        # For initial connections, request client sessions after control loop starts
        if is_initial:
            asyncio.create_task(self._initial_connection_setup())

    def _register_default_handlers(self) -> None:
        """Register the default command handlers."""
        self._command_registry.register(TerminalStartHandler)
        self._command_registry.register(TerminalSendHandler)
        self._command_registry.register(TerminalStopHandler)
        self._command_registry.register(TerminalListHandler)
        self._command_registry.register(SystemInfoHandler)
        self._command_registry.register(DirectoryListHandler)

    # ---------------------------------------------------------------------
    # Control loop – receives commands from gateway
    # ---------------------------------------------------------------------

    async def _control_loop(self) -> None:
        logger.info("terminal_manager: Starting control loop")
        while True:
            try:
                message = await self._control_channel.recv()
                logger.debug("terminal_manager: Received message: %s", message)
                
                # Older parts of the system may send *raw* str. Ensure dict.
                if isinstance(message, str):
                    try:
                        message = json.loads(message)
                        logger.debug("terminal_manager: Parsed string message to dict")
                    except Exception:
                        logger.warning("terminal_manager: Discarding non-JSON control frame: %s", message)
                        continue
                if not isinstance(message, dict):
                    logger.warning("terminal_manager: Invalid control frame type: %r", type(message))
                    continue
                cmd = message.get("cmd")
                if not cmd:
                    # Ignore frames that are *events* coming from the remote side
                    if message.get("event"):
                        logger.debug("terminal_manager: Ignoring event message: %s", message.get("event"))
                        continue
                    logger.warning("terminal_manager: Missing 'cmd' in control frame: %s", message)
                    continue
                reply_chan = message.get("reply_channel")
                
                logger.info("terminal_manager: Processing command '%s' with reply_channel=%s", cmd, reply_chan)
                logger.debug("terminal_manager: Full message: %s", message)
                
                # Handle client sessions update directly (special case)
                if cmd == "client_sessions_update":
                    sessions = message.get("sessions", [])
                    logger.info("terminal_manager: Handling client_sessions_update with %d sessions", len(sessions))
                    self._client_session_manager.update_sessions(sessions)
                    logger.info("terminal_manager: Updated client sessions (%d sessions)", len(sessions))
                    continue
                
                # Dispatch command through registry
                handled = await self._command_registry.dispatch(cmd, message, reply_chan)
                if not handled:
                    logger.warning("terminal_manager: Command '%s' was not handled by any handler", cmd)
                    await self._send_error(f"Unknown cmd: {cmd}", reply_chan)
                    
            except Exception as exc:
                logger.exception("terminal_manager: Error in control loop: %s", exc)
                # Continue processing other messages
                continue

    # ------------------------------------------------------------------
    # Extension API
    # ------------------------------------------------------------------

    def register_handler(self, handler_class) -> None:
        """Register a custom command handler.
        
        Args:
            handler_class: Handler class that inherits from BaseHandler
        """
        self._command_registry.register(handler_class)

    def unregister_handler(self, command_name: str) -> None:
        """Unregister a command handler.
        
        Args:
            command_name: The command name to unregister
        """
        self._command_registry.unregister(command_name)

    def list_commands(self) -> List[str]:
        """List all registered command names.
        
        Returns:
            List of command names
        """
        return self._command_registry.list_commands()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_error(self, message: str, reply_channel: Optional[str] = None) -> None:
        payload = {"event": "error", "message": message}
        if reply_channel:
            payload["reply_channel"] = reply_channel
        await self._send_session_aware(payload)
    
    async def _send_session_aware(self, payload: dict, project_id: str = None) -> None:
        """Send a message with client session awareness.
        
        Args:
            payload: The message payload to send
            project_id: Optional project filter for targeting specific sessions
        """
        # Check if there are any interested clients
        if not self._client_session_manager.has_interested_clients():
            logger.debug("terminal_manager: No interested clients, skipping message send")
            return
        
        # Get target sessions
        target_sessions = self._client_session_manager.get_target_sessions(project_id)
        if not target_sessions:
            logger.debug("terminal_manager: No target sessions found, skipping message send")
            return
        
        # Add session targeting information
        enhanced_payload = dict(payload)
        enhanced_payload["client_sessions"] = target_sessions
        
        # Add backward compatibility reply_channel (first session)
        reply_channel = self._client_session_manager.get_reply_channel_for_compatibility()
        if reply_channel and "reply_channel" not in enhanced_payload:
            enhanced_payload["reply_channel"] = reply_channel
        
        logger.debug("terminal_manager: Sending to %d client sessions: %s", 
                    len(target_sessions), target_sessions)
        
        await self._control_channel.send(enhanced_payload)

    async def _send_terminal_list(self) -> None:
        """Send terminal list for reconnection reconciliation."""
        try:
            sessions = self._session_manager.list_sessions()
            if sessions:
                logger.info("Sending terminal list with %d sessions to server", len(sessions))
            payload = {
                "event": "terminal_list",
                "sessions": sessions,
            }
            await self._send_session_aware(payload)
        except Exception as exc:
            logger.warning("Failed to send terminal list: %s", exc)
    
    async def _request_client_sessions(self) -> None:
        """Request current client sessions from server."""
        try:
            payload = {
                "event": "request_client_sessions"
            }
            # This is a special case - always send regardless of current client sessions
            # because we're trying to get the client sessions list
            await self._control_channel.send(payload)
            logger.info("Requested client sessions from server")
        except Exception as exc:
            logger.warning("Failed to request client sessions: %s", exc)

    async def _initial_connection_setup(self) -> None:
        """Handle initial connection setup sequence."""
        try:
            # Send empty terminal list
            await self._send_terminal_list()
            logger.info("Initial terminal list sent to server")
            
            # Request current client sessions
            await self._request_client_sessions()
            logger.info("Initial client session request sent")
        except Exception as exc:
            logger.error("Failed to handle initial connection setup: %s", exc)

    async def _handle_reconnection(self) -> None:
        """Handle the async reconnection sequence."""
        try:
            # First, reattach all sessions to new multiplexer
            await self._session_manager.reattach_sessions(self.mux)
            logger.info("Terminal session reattachment completed")
            
            # Then send updated terminal list to server
            await self._send_terminal_list()
            logger.info("Terminal list sent to server after reconnection")
            
            # Request current client sessions
            await self._request_client_sessions()
            logger.info("Client session request sent after reconnection")
        except Exception as exc:
            logger.error("Failed to handle reconnection: %s", exc) 