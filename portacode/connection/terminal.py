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
from dataclasses import asdict
from typing import Any, Dict, Optional, List

from websockets.exceptions import ConnectionClosedError

from .multiplex import Multiplexer, Channel
from .handlers import (
    CommandRegistry,
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
    SystemInfoHandler,
    FileReadHandler,
    DirectoryListHandler,
    FileInfoHandler,
    FileDeleteHandler,
    FileSearchHandler,
    FileRenameHandler,
    ContentRequestHandler,
    FileApplyDiffHandler,
    FilePreviewDiffHandler,
    ProjectStateFolderExpandHandler,
    ProjectStateFolderCollapseHandler,
    ProjectStateFileOpenHandler,
    ProjectStateTabCloseHandler,
    ProjectStateSetActiveTabHandler,
    ProjectStateDiffOpenHandler,
    ProjectStateDiffContentHandler,
    ProjectStateGitStageHandler,
    ProjectStateGitUnstageHandler,
    ProjectStateGitRevertHandler,
    ProjectStateGitCommitHandler,
    UpdatePortacodeHandler,
    ConfigureProxmoxInfraHandler,
    CreateProxmoxContainerHandler,
    RevertProxmoxInfraHandler,
    StartPortacodeServiceHandler,
    StartProxmoxContainerHandler,
    StopProxmoxContainerHandler,
    RemoveProxmoxContainerHandler,
)
from .handlers.project_aware_file_handlers import (
    ProjectAwareFileWriteHandler,
    ProjectAwareFileCreateHandler,
    ProjectAwareFolderCreateHandler,
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
            # NOTE: Not automatically cleaning up project states for disconnected sessions
            # to handle temporary disconnections gracefully. Project states will be cleaned
            # up only when explicitly notified by the server of permanent disconnection.
            logger.info("Project states preserved for potential reconnection of these sessions")
        
        # Handle project state management based on session changes
        if newly_added_sessions or disconnected_sessions:
            # Schedule project state management to run asynchronously
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule both cleanup and initialization
                    loop.create_task(self._manage_project_states_for_session_changes(newly_added_sessions, sessions))
                else:
                    logger.debug("No event loop running, skipping project state management")
            except Exception as e:
                logger.debug("Could not schedule project state management: %s", e)
        
        self._write_debug_file()
        return newly_added_sessions
    
    async def cleanup_client_session_explicitly(self, client_session_id: str):
        """Explicitly clean up resources for a client session when notified by server."""
        logger.info("Explicitly cleaning up resources for client session: %s", client_session_id)
        
        # Import here to avoid circular imports
        from .handlers.project_state_handlers import _get_or_create_project_state_manager
        
        # Get the project state manager from context if it exists
        if hasattr(self, '_terminal_manager') and self._terminal_manager:
            context = getattr(self._terminal_manager, '_context', {})
            if context:
                # Get or create the project state manager
                control_channel = getattr(self._terminal_manager, '_control_channel', None)
                if control_channel:
                    project_manager = _get_or_create_project_state_manager(context, control_channel)
                    logger.info("Cleaning up project state for client session: %s", client_session_id)
                    await project_manager.cleanup_projects_by_client_session(client_session_id)
                else:
                    logger.warning("No control channel available for project state cleanup")
            else:
                logger.warning("No context available for project state cleanup")
        else:
            logger.warning("No terminal manager available for project state cleanup")
            
    async def _manage_project_states_for_session_changes(self, newly_added_sessions: List[str], all_sessions: List[Dict]):
        """Comprehensive project state management for session changes."""
        try:
            # Import here to avoid circular imports
            from .handlers.project_state_handlers import _get_or_create_project_state_manager
            
            if not hasattr(self, '_terminal_manager') or not self._terminal_manager:
                logger.warning("No terminal manager available for project state management")
                return
            
            context = getattr(self._terminal_manager, '_context', {})
            if not context:
                logger.warning("No context available for project state management")
                return
            
            control_channel = getattr(self._terminal_manager, '_control_channel', None)
            if not control_channel:
                logger.warning("No control channel available for project state management")
                return
            
            project_manager = _get_or_create_project_state_manager(context, control_channel)
            
            # Convert sessions list to dict for easier lookup
            sessions_dict = {session.get('channel_name'): session for session in all_sessions if session.get('channel_name')}
            
            # First, clean up project states for sessions that are no longer project sessions or don't exist
            current_project_sessions = set()
            for session in all_sessions:
                channel_name = session.get('channel_name')
                project_id = session.get('project_id')
                project_folder_path = session.get('project_folder_path')
                
                if channel_name and project_id is not None and project_folder_path:
                    current_project_sessions.add(channel_name)
                    logger.debug(f"Active project session: {channel_name} -> {project_folder_path} (project_id: {project_id})")
            
            # Clean up project states that don't match current project sessions
            existing_project_states = list(project_manager.projects.keys())
            for session_id in existing_project_states:
                if session_id not in current_project_sessions:
                    logger.info(f"Cleaning up project state for session {session_id} (no longer a project session)")
                    await project_manager.cleanup_project(session_id)
            
            # Initialize project states for new project sessions
            for session_name in newly_added_sessions:
                session = sessions_dict.get(session_name)
                if not session:
                    continue
                
                project_id = session.get('project_id')
                project_folder_path = session.get('project_folder_path')
                
                if project_id is not None and project_folder_path:
                    if session_name in project_manager.projects:
                        logger.info("Project state already exists for session %s, skipping re-init", session_name)
                        continue
                    logger.info(f"Initializing project state for new project session {session_name}: {project_folder_path}")
                    
                    try:
                        # Initialize project state (this includes migration logic)
                        project_state = await project_manager.initialize_project_state(session_name, project_folder_path)
                        logger.info(f"Successfully initialized project state for {session_name}")
                        
                        # Send initial project state to the client
                        # (implementation can be added here if needed)
                        
                    except Exception as e:
                        logger.error(f"Failed to initialize project state for {session_name}: {e}")
        except Exception as e:
            logger.error("Error managing project states for session changes: %s", e)
    
    async def _cleanup_orphaned_project_states(self):
        """Clean up project states that don't match any current client session."""
        try:
            # Import here to avoid circular imports
            from .handlers.project_state_handlers import _get_or_create_project_state_manager
            
            # Get current client session IDs
            current_sessions = list(self._client_sessions.keys())
            
            if hasattr(self, '_terminal_manager') and self._terminal_manager:
                context = getattr(self._terminal_manager, '_context', {})
                if context:
                    control_channel = getattr(self._terminal_manager, '_control_channel', None)
                    if control_channel:
                        project_manager = _get_or_create_project_state_manager(context, control_channel)
                        await project_manager.cleanup_orphaned_project_states(current_sessions)
                    else:
                        logger.warning("No control channel available for orphaned project state cleanup")
                else:
                    logger.warning("No context available for orphaned project state cleanup")
            else:
                logger.warning("No terminal manager available for orphaned project state cleanup")
                
        except Exception as e:
            logger.error("Error cleaning up orphaned project states: %s", e)
    
    def _cleanup_disconnected_sessions(self, disconnected_sessions: List[str]):
        """Legacy method - now just logs disconnections without cleanup."""
        logger.info("Sessions disconnected (but preserving project states): %s", disconnected_sessions)
        # Project states are preserved to handle reconnections gracefully
    
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
            "use_content_caching": True,  # Enable content caching optimization
            "debug": self.debug,
            "event_loop": asyncio.get_running_loop(),
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
        
        # Start periodic system info sender
        if getattr(self, "_system_info_task", None):
            try:
                self._system_info_task.cancel()
            except Exception:
                pass
        self._system_info_task = asyncio.create_task(self._periodic_system_info())
        
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
        self._command_registry.register(ProjectAwareFileWriteHandler)  # Use project-aware version
        self._command_registry.register(DirectoryListHandler)
        self._command_registry.register(FileInfoHandler)
        self._command_registry.register(FileDeleteHandler)
        self._command_registry.register(ProjectAwareFileCreateHandler)  # Use project-aware version
        self._command_registry.register(ProjectAwareFolderCreateHandler)  # Use project-aware version
        self._command_registry.register(FileRenameHandler)
        self._command_registry.register(FileSearchHandler)
        self._command_registry.register(ContentRequestHandler)
        self._command_registry.register(FileApplyDiffHandler)
        self._command_registry.register(FilePreviewDiffHandler)
        # Project state handlers
        self._command_registry.register(ProjectStateFolderExpandHandler)
        self._command_registry.register(ProjectStateFolderCollapseHandler)
        self._command_registry.register(ProjectStateFileOpenHandler)
        self._command_registry.register(ProjectStateTabCloseHandler)
        self._command_registry.register(ProjectStateSetActiveTabHandler)
        self._command_registry.register(ProjectStateDiffOpenHandler)
        self._command_registry.register(ProjectStateDiffContentHandler)
        self._command_registry.register(ProjectStateGitStageHandler)
        self._command_registry.register(ProjectStateGitUnstageHandler)
        self._command_registry.register(ProjectStateGitRevertHandler)
        self._command_registry.register(ProjectStateGitCommitHandler)
        # System management handlers
        self._command_registry.register(ConfigureProxmoxInfraHandler)
        self._command_registry.register(CreateProxmoxContainerHandler)
        self._command_registry.register(StartPortacodeServiceHandler)
        self._command_registry.register(StartProxmoxContainerHandler)
        self._command_registry.register(StopProxmoxContainerHandler)
        self._command_registry.register(RemoveProxmoxContainerHandler)
        self._command_registry.register(RevertProxmoxInfraHandler)
        self._command_registry.register(UpdatePortacodeHandler)

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
                    # Create a background task so it doesn't block the control loop
                    if newly_added_sessions:
                        logger.info("terminal_manager: 🚀 Triggering auto-send of initial data to newly added clients (non-blocking)")
                        asyncio.create_task(self._send_initial_data_to_clients(newly_added_sessions))
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

    async def _periodic_system_info(self) -> None:
        """Send system_info event every 10 seconds when clients are connected."""
        while True:
            try:
                await asyncio.sleep(10)
                if self._client_session_manager.has_interested_clients():
                    from .handlers.system_handlers import SystemInfoHandler
                    handler = SystemInfoHandler(self._control_channel, self._context)
                    system_info = handler.execute({})
                    await self._send_session_aware(system_info)
            except Exception as exc:
                logger.exception("Error in periodic system info: %s", exc)
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
                
                project_id = session.get("project_id")
                project_folder_path = session.get("project_folder_path")
                if project_id is None or not project_folder_path:
                    logger.debug(f"terminal_manager: 🌳 Session {session_name} has no project_id or project_folder_path, skipping")
                    continue
                
                logger.info(f"terminal_manager: 🌳 Initializing project state for session {session_name} with folder: {project_folder_path}")
                
                try:
                    # Initialize project state
                    project_state = await manager.initialize_project_state(session_name, project_folder_path)
                    await self._restore_tabs_from_session_metadata(manager, project_state, session)
                    
                    # Send initial project state to the client
                    initial_state_payload = {
                        "event": "project_state_initialized",
                        "project_id": project_state.client_session_id,  # Add missing project_id field
                        "project_folder_path": project_state.project_folder_path,
                        "is_git_repo": project_state.is_git_repo,
                        "git_branch": project_state.git_branch,
                        "git_status_summary": project_state.git_status_summary,
                        "git_detailed_status": asdict(project_state.git_detailed_status) if project_state.git_detailed_status and hasattr(project_state.git_detailed_status, '__dataclass_fields__') else None,  # Add missing git_detailed_status field
                        "open_tabs": [manager._serialize_tab_info(tab) for tab in project_state.open_tabs.values()],  # Fix to use .values() for dict
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

    async def _restore_tabs_from_session_metadata(self, manager, project_state, session):
        """Restore open tabs/active tab from client session metadata if available."""
        if not session or not project_state:
            return

        descriptors = session.get("open_tabs") or []
        if not descriptors:
            return

        session_id = project_state.client_session_id
        logger.info("terminal_manager: 🧭 Restoring %d tabs from metadata for session %s", len(descriptors), session_id)

        for descriptor in descriptors:
            parsed = self._parse_tab_descriptor(descriptor)
            if not parsed:
                continue

            tab_type = parsed.get("tab_type")
            file_path = parsed.get("file_path")
            metadata = parsed.get("metadata", {})

            if tab_type == "file" and file_path:
                try:
                    await manager.open_file(session_id, file_path, set_active=False)
                except Exception as exc:
                    logger.warning("terminal_manager: Failed to restore file tab %s for session %s: %s", file_path, session_id, exc)
                continue

            if tab_type == "diff" and file_path:
                from_ref = metadata.get("from") or metadata.get("from_ref")
                to_ref = metadata.get("to") or metadata.get("to_ref")
                if not from_ref or not to_ref:
                    logger.warning("terminal_manager: Skipping diff tab %s for session %s because from/to references are missing", file_path, session_id)
                    continue
                from_hash = metadata.get("from_hash") or metadata.get("fromHash")
                to_hash = metadata.get("to_hash") or metadata.get("toHash")
                try:
                    await manager.open_diff_tab(session_id, file_path, from_ref, to_ref, from_hash=from_hash, to_hash=to_hash)
                except Exception as exc:
                    logger.warning("terminal_manager: Failed to restore diff tab %s for session %s: %s", file_path, session_id, exc)
                continue

            logger.debug("terminal_manager: Unknown tab descriptor ignored for session %s: %s", session_id, descriptor)

        active_index = session.get("active_tab")
        try:
            active_index_int = int(active_index) if active_index is not None else None
        except (TypeError, ValueError):
            active_index_int = None

        if active_index_int is not None and active_index_int >= 0:
            current_tabs = list(project_state.open_tabs.values())
            if 0 <= active_index_int < len(current_tabs):
                try:
                    await manager.set_active_tab(session_id, current_tabs[active_index_int].tab_id)
                except Exception as exc:
                    logger.warning("terminal_manager: Failed to set active tab for session %s: %s", session_id, exc)
            else:
                logger.debug("terminal_manager: Active tab index %s out of range for session %s", active_index_int, session_id)

    def _parse_tab_descriptor(self, descriptor: str) -> Optional[Dict[str, Any]]:
        """Parse a URL-friendly tab descriptor string."""
        if not descriptor:
            return None

        try:
            parts = descriptor.split("|")
            tab_type = parts[0] if parts else None
            file_path = parts[1] if len(parts) > 1 else None
            metadata = {}
            for part in parts[2:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    metadata[key] = value
            return {"tab_type": tab_type, "file_path": file_path, "metadata": metadata}
        except Exception as exc:
            logger.warning("terminal_manager: Failed to parse tab descriptor '%s': %s", descriptor, exc)
            return None
    
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
        event_type = payload.get("event", "unknown")
        
        # Check if there are any interested clients
        if not self._client_session_manager.has_interested_clients():
            logger.info("terminal_manager: No interested clients for %s event, skipping send", event_type)
            return
        
        # Get target sessions
        target_sessions = self._client_session_manager.get_target_sessions(project_id)
        if not target_sessions:
            logger.info("terminal_manager: No target sessions found for %s event (project_id=%s), skipping send", event_type, project_id)
            return
        
        # Add session targeting information
        enhanced_payload = dict(payload)
        enhanced_payload["client_sessions"] = target_sessions
        
        # Add backward compatibility reply_channel (first session)
        reply_channel = self._client_session_manager.get_reply_channel_for_compatibility()
        if reply_channel and "reply_channel" not in enhanced_payload:
            enhanced_payload["reply_channel"] = reply_channel
        
        # Log all event dispatches at INFO level, with data size for terminal_data
        if event_type == "terminal_data":
            data_size = len(payload.get("data", ""))
            terminal_id = payload.get("channel", "unknown")
            logger.info("terminal_manager: Dispatching %s event (terminal_id=%s, data_size=%d bytes) to %d client sessions", 
                       event_type, terminal_id, data_size, len(target_sessions))
        # else:
        #     logger.info("terminal_manager: Dispatching %s event to %d client sessions", 
        #                event_type, len(target_sessions))
        
        try:
            await self._control_channel.send(enhanced_payload)
        except ConnectionClosedError as exc:
            logger.warning("terminal_manager: Connection closed (%s); skipping %s event", exc, event_type)
            return

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
