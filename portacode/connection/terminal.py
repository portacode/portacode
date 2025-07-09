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
from typing import Any, Dict, Optional, List

from .multiplex import Multiplexer, Channel
from .handlers import (
    CommandRegistry,
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
    SystemInfoHandler,
)
from .handlers.session import SessionManager

logger = logging.getLogger(__name__)

__all__ = [
    "TerminalManager",
]

class TerminalManager:
    """Manage command processing through a modular handler system."""

    CONTROL_CHANNEL_ID = 0  # messages with JSON commands/events

    def __init__(self, mux: Multiplexer):
        self.mux = mux
        self._session_manager = None  # Initialize as None first
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
            # No existing sessions, just send empty terminal list
            asyncio.create_task(self._send_terminal_list())

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

    def _register_default_handlers(self) -> None:
        """Register the default command handlers."""
        self._command_registry.register(TerminalStartHandler)
        self._command_registry.register(TerminalSendHandler)
        self._command_registry.register(TerminalStopHandler)
        self._command_registry.register(TerminalListHandler)
        self._command_registry.register(SystemInfoHandler)

    # ---------------------------------------------------------------------
    # Control loop – receives commands from gateway
    # ---------------------------------------------------------------------

    async def _control_loop(self) -> None:
        while True:
            message = await self._control_channel.recv()
            # Older parts of the system may send *raw* str. Ensure dict.
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except Exception:
                    logger.warning("Discarding non-JSON control frame: %s", message)
                    continue
            if not isinstance(message, dict):
                logger.warning("Invalid control frame type: %r", type(message))
                continue
            cmd = message.get("cmd")
            if not cmd:
                # Ignore frames that are *events* coming from the remote side
                if message.get("event"):
                    continue
                logger.warning("Missing 'cmd' in control frame: %s", message)
                continue
            reply_chan = message.get("reply_channel")
            
            # Dispatch command through registry
            handled = await self._command_registry.dispatch(cmd, message, reply_chan)
            if not handled:
                await self._send_error(f"Unknown cmd: {cmd}", reply_chan)

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
        await self._control_channel.send(payload)

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
            await self._control_channel.send(payload)
        except Exception as exc:
            logger.warning("Failed to send terminal list: %s", exc)

    async def _handle_reconnection(self) -> None:
        """Handle the async reconnection sequence."""
        try:
            # First, reattach all sessions to new multiplexer
            await self._session_manager.reattach_sessions(self.mux)
            logger.info("Terminal session reattachment completed")
            
            # Then send updated terminal list to server
            await self._send_terminal_list()
            logger.info("Terminal list sent to server after reconnection")
        except Exception as exc:
            logger.error("Failed to handle reconnection: %s", exc) 