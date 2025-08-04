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
import time
from typing import Any, Dict, Optional, List

from .multiplex import Multiplexer, Channel
from .handlers import (
    CommandRegistry,
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
    SystemInfoHandler,
    FileReadHandler,
    FileWriteHandler,
    DirectoryListHandler,
    FileInfoHandler,
    FileDeleteHandler,
    ProjectStateFolderExpandHandler,
    ProjectStateFolderCollapseHandler,
    ProjectStateFileOpenHandler,
    ProjectStateTabCloseHandler,
    ProjectStateSetActiveTabHandler,
    ProjectStateDiffOpenHandler,
)
from .handlers.session import SessionManager

logger = logging.getLogger(__name__)

class ClientSessionManager:
    """Manages connected client sessions for the device."""
    
    def __init__(self):
        self._client_sessions = {}
        self._debug_file_path = os.path.join(os.getcwd(), "client_sessions.json")
        logger.info("ClientSessionManager initialized")
    
    def update_sessions(self, sessions: List[Dict]) -> List[str]:
        """Update the client sessions with new data from server.
        
        Returns:
            List of channel_names for newly added sessions
        """
        old_sessions = set(self._client_sessions.keys())
        self._client_sessions = {}
        for session in sessions:
            channel_name = session.get("channel_name")
            if channel_name:
                self._client_sessions[channel_name] = session
        
        new_sessions = set(self._client_sessions.keys())
        newly_added_sessions = list(new_sessions - old_sessions)
        disconnected_sessions = list(old_sessions - new_sessions)
        
        logger.info(f"Updated client sessions: {len(self._client_sessions)} sessions, {len(newly_added_sessions)} newly added, {len(disconnected_sessions)} disconnected")
        if newly_added_sessions:
            logger.info(f"Newly added sessions: {newly_added_sessions}")
        if disconnected_sessions:
            logger.info(f"Disconnected sessions: {disconnected_sessions}")
            # Trigger cleanup for disconnected sessions
            self._cleanup_disconnected_sessions(disconnected_sessions)
        
        self._write_debug_file()
        return newly_added_sessions
    
    def _cleanup_disconnected_sessions(self, disconnected_sessions: List[str]):
        """Clean up resources for disconnected client sessions."""
        logger.info("Cleaning up resources for %d disconnected sessions", len(disconnected_sessions))
        
        # Import here to avoid circular imports
        from .handlers.project_state_handlers import _get_or_create_project_state_manager
        
        # Get the project state manager from context if it exists
        # We need to get the context from the terminal manager
        if hasattr(self, '_terminal_manager') and self._terminal_manager:
            context = getattr(self._terminal_manager, '_context', {})
            if context:
                # Get or create the project state manager
                control_channel = getattr(self._terminal_manager, '_control_channel', None)
                if control_channel:
                    project_manager = _get_or_create_project_state_manager(context, control_channel)
                    
                    # Clean up project states for each disconnected session
                    for client_session_id in disconnected_sessions:
                        logger.info("Cleaning up project states for disconnected session: %s", client_session_id)
                        project_manager.cleanup_projects_by_client_session(client_session_id)
                else:
                    logger.warning("No control channel available for project state cleanup")
            else:
                logger.warning("No context available for project state cleanup")
        else:
            logger.warning("No terminal manager available for project state cleanup")
    
    def set_terminal_manager(self, terminal_manager):
        """Set reference to terminal manager for cleanup purposes."""
        self._terminal_manager = terminal_manager
    
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
            project_id: If specified, only include sessions for this project.
                       Dashboard sessions only receive events when project_id is None/empty.
            
        Returns:
            List of channel_names to target
        """
        if not self._client_sessions:
            return []
        
        target_sessions = []
        for session in self._client_sessions.values():
            # Dashboard sessions only receive events when project_id is None/empty
            if session.get("connection_type") == "dashboard":
                if project_id is None:
                    target_sessions.append(session.get("channel_name"))
                continue
            
            # For project sessions, filter by project_id if specified
            if project_id and session.get("project_id") != project_id:
                continue
            target_sessions.append(session.get("channel_name"))
        
        return [s for s in target_sessions if s]  # Filter out None values
    
    def get_target_sessions_for_new_clients(self, new_session_names: List[str], project_id: str = None) -> List[str]:
        """Get target sessions for newly added client sessions.
        
        Args:
            new_session_names: List of newly added session channel names
            project_id: If specified, only include sessions for this project.
                       Dashboard sessions only receive events when project_id is None/empty.
            
        Returns:
            List of channel_names to target from newly added sessions
        """
        if not new_session_names or not self._client_sessions:
            return []
        
        target_sessions = []
        for channel_name in new_session_names:
            session = self._client_sessions.get(channel_name)
            if not session:
                continue
                
            # Dashboard sessions only receive events when project_id is None/empty
            if session.get("connection_type") == "dashboard":
                if project_id is None:
                    target_sessions.append(channel_name)
                continue
            
            # For project sessions, filter by project_id if specified
            if project_id and session.get("project_id") != project_id:
                continue
            target_sessions.append(channel_name)
        
        return target_sessions
    
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

    def __init__(self, mux: Multiplexer, debug: bool = False):
        self.mux = mux
        self.debug = debug
        self._session_manager = None  # Initialize as None first
        self._client_session_manager = ClientSessionManager()  # Initialize client session manager
        self._client_session_manager.set_terminal_manager(self)  # Set reference for cleanup
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
            self._session_manager = SessionManager(mux, terminal_manager=self)
            logger.info("Created new SessionManager")
        else:
            # Update existing session manager's mux and terminal_manager references
            self._session_manager.mux = mux
            self._session_manager.terminal_manager = self
            logger.info("Preserved existing SessionManager with %d sessions", len(self._session_manager._sessions))
        
        # Create context for handlers
        self._context = {
            "session_manager": self._session_manager,
            "client_session_manager": self._client_session_manager,
            "mux": mux,
            "debug": self.debug,
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
        # File operation handlers
        self._command_registry.register(FileReadHandler)
        self._command_registry.register(FileWriteHandler)
        self._command_registry.register(DirectoryListHandler)
        self._command_registry.register(FileInfoHandler)
        self._command_registry.register(FileDeleteHandler)
        # Project state handlers
        self._command_registry.register(ProjectStateFolderExpandHandler)
        self._command_registry.register(ProjectStateFolderCollapseHandler)
        self._command_registry.register(ProjectStateFileOpenHandler)
        self._command_registry.register(ProjectStateTabCloseHandler)
        self._command_registry.register(ProjectStateSetActiveTabHandler)
        self._command_registry.register(ProjectStateDiffOpenHandler)

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
                    logger.info("terminal_manager: 🔔 RECEIVED client_sessions_update with %d sessions", len(sessions))
                    logger.debug("terminal_manager: Session details: %s", sessions)
                    newly_added_sessions = self._client_session_manager.update_sessions(sessions)
                    logger.info("terminal_manager: ✅ Updated client sessions (%d sessions)", len(sessions))
                    
                    # Auto-send initial data only to newly added clients
                    if newly_added_sessions:
                        logger.info("terminal_manager: 🚀 Triggering auto-send of initial data to newly added clients")
                        await self._send_initial_data_to_clients(newly_added_sessions)
                    else:
                        logger.info("terminal_manager: ℹ️ No new sessions to send data to")
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

    async def _send_initial_data_to_clients(self, newly_added_sessions: List[str] = None):
        """Send initial system info and terminal list to connected clients.
        
        Args:
            newly_added_sessions: If provided, only send data to these specific sessions
        """
        if newly_added_sessions:
            logger.info("terminal_manager: 📤 Starting to send initial data to newly added clients: %s", newly_added_sessions)
        else:
            logger.info("terminal_manager: 📤 Starting to send initial data to all connected clients")
        
        try:
            # Send system_info (always broadcasts to all clients for now)
            logger.info("terminal_manager: 📊 Dispatching system_info command")
            await self._command_registry.dispatch("system_info", {}, None)
            logger.info("terminal_manager: ✅ system_info dispatch completed")
            
            # Send terminal_list only to newly added clients or to all if not specified
            logger.info("terminal_manager: 📋 Preparing to send terminal_list to clients")
            
            if newly_added_sessions:
                # Get unique project IDs from the newly added sessions
                project_ids = set()
                all_sessions = self._client_session_manager.get_sessions()
                
                for session_name in newly_added_sessions:
                    session = all_sessions.get(session_name)
                    if session:
                        project_id = session.get("project_id")
                        connection_type = session.get("connection_type", "unknown")
                        logger.debug(f"terminal_manager: New session {session_name}: project_id={project_id}, type={connection_type}")
                        if project_id:
                            project_ids.add(project_id)
                
                logger.info(f"terminal_manager: Found {len(project_ids)} unique project IDs from new sessions: {list(project_ids)}")
                
                # Initialize project states for sessions with project_folder_path
                await self._initialize_project_states_for_new_sessions(newly_added_sessions, all_sessions)
                
                # Send terminal_list for each project to interested new sessions
                for project_id in project_ids:
                    target_sessions = self._client_session_manager.get_target_sessions_for_new_clients(newly_added_sessions, project_id)
                    if target_sessions:
                        logger.info(f"terminal_manager: 📋 Sending terminal_list for project {project_id} to sessions: {target_sessions}")
                        await self._send_targeted_terminal_list({"project_id": project_id}, target_sessions)
                        logger.info(f"terminal_manager: ✅ Project {project_id} terminal_list sent to new sessions")
                
                # Also send general terminal_list for dashboard connections (project_id=None)
                dashboard_targets = self._client_session_manager.get_target_sessions_for_new_clients(newly_added_sessions, None)
                if dashboard_targets:
                    logger.info("terminal_manager: 📋 Sending general terminal_list to new dashboard sessions: %s", dashboard_targets)
                    await self._send_targeted_terminal_list({}, dashboard_targets)
                    logger.info("terminal_manager: ✅ General terminal_list sent to new dashboard sessions")
            else:
                # Original behavior for all clients
                # Get unique project IDs from connected clients
                project_ids = set()
                all_sessions = self._client_session_manager.get_sessions()
                logger.info(f"terminal_manager: Analyzing {len(all_sessions)} client sessions for project IDs")
                
                for session in all_sessions.values():
                    project_id = session.get("project_id")
                    connection_type = session.get("connection_type", "unknown")
                    logger.debug(f"terminal_manager: Session {session.get('channel_name')}: project_id={project_id}, type={connection_type}")
                    if project_id:
                        project_ids.add(project_id)
                
                logger.info(f"terminal_manager: Found {len(project_ids)} unique project IDs: {list(project_ids)}")
                
                # Send terminal_list for each project, plus one without project_id for general sessions
                if not project_ids:
                    # No specific projects, send general terminal_list
                    logger.info("terminal_manager: 📋 Dispatching general terminal_list (no specific projects)")
                    await self._command_registry.dispatch("terminal_list", {}, None)
                    logger.info("terminal_manager: ✅ General terminal_list dispatch completed")
                else:
                    # Send terminal_list for each project
                    for project_id in project_ids:
                        logger.info(f"terminal_manager: 📋 Dispatching terminal_list for project {project_id}")
                        await self._command_registry.dispatch("terminal_list", {"project_id": project_id}, None)
                        logger.info(f"terminal_manager: ✅ Project {project_id} terminal_list dispatch completed")
                        
                    # Also send general terminal_list for dashboard connections
                    logger.info("terminal_manager: 📋 Dispatching general terminal_list for dashboard connections")
                    await self._command_registry.dispatch("terminal_list", {}, None)
                    logger.info("terminal_manager: ✅ General terminal_list for dashboard dispatch completed")
            
            logger.info("terminal_manager: 🎉 All initial data sent successfully")
                    
        except Exception as exc:
            logger.exception("terminal_manager: ❌ Error sending initial data to clients: %s", exc)
    
    async def _initialize_project_states_for_new_sessions(self, newly_added_sessions: List[str], all_sessions: Dict[str, Dict]):
        """Initialize project states for new sessions that have project_folder_path."""
        logger.info("terminal_manager: 🌳 Initializing project states for new sessions")
        
        try:
            # Import here to avoid circular imports
            from .handlers.project_state_handlers import _get_or_create_project_state_manager
            
            # Get or create the project state manager
            manager = _get_or_create_project_state_manager(self._context, self._control_channel)
            
            for session_name in newly_added_sessions:
                session = all_sessions.get(session_name)
                if not session:
                    continue
                
                project_folder_path = session.get("project_folder_path")
                if not project_folder_path:
                    logger.debug(f"terminal_manager: 🌳 Session {session_name} has no project_folder_path, skipping")
                    continue
                
                logger.info(f"terminal_manager: 🌳 Initializing project state for session {session_name} with folder: {project_folder_path}")
                
                try:
                    # Initialize project state
                    project_state = await manager.initialize_project_state(session_name, project_folder_path)
                    
                    # Send initial project state to the client
                    initial_state_payload = {
                        "event": "project_state_initialized",
                        "project_folder_path": project_state.project_folder_path,
                        "is_git_repo": project_state.is_git_repo,
                        "git_branch": project_state.git_branch,
                        "git_status_summary": project_state.git_status_summary,
                        "open_tabs": [manager._serialize_tab_info(tab) for tab in project_state.open_tabs],
                        "active_tab": manager._serialize_tab_info(project_state.active_tab) if project_state.active_tab else None,
                        "items": [manager._serialize_file_item(item) for item in project_state.items],
                        "timestamp": time.time(),
                        "client_sessions": [session_name]  # Target this specific session
                    }
                    
                    await self._control_channel.send(initial_state_payload)
                    logger.info(f"terminal_manager: ✅ Project state initialized and sent for session {session_name}")
                    
                except Exception as exc:
                    logger.error(f"terminal_manager: ❌ Failed to initialize project state for session {session_name}: {exc}")
                    
        except Exception as exc:
            logger.exception("terminal_manager: Error initializing project states for new sessions: %s", exc)
    
    async def _send_targeted_terminal_list(self, message: Dict[str, Any], target_sessions: List[str]) -> None:
        """Send terminal_list command to specific client sessions.
        
        Args:
            message: The terminal_list command message
            target_sessions: List of client session channel names to target
        """
        try:
            # Get the terminal list from session manager
            session_manager = self._session_manager
            if not session_manager:
                logger.error("terminal_manager: Session manager not available for targeted terminal_list")
                return
            
            requested_project_id = message.get("project_id")
            if requested_project_id == "all":
                sessions = session_manager.list_sessions(project_id="all")
            else:
                sessions = session_manager.list_sessions(project_id=requested_project_id)
            
            # Build the response payload
            response = {
                "event": "terminal_list",
                "sessions": sessions,
                "project_id": requested_project_id,
                "client_sessions": target_sessions
            }
            
            logger.debug("terminal_manager: Sending targeted terminal_list: %s", response)
            await self._control_channel.send(response)
        except Exception as exc:
            logger.exception("terminal_manager: Error sending targeted terminal_list: %s", exc)

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