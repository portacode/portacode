"""Request handlers for project state management operations.

This module contains all the AsyncHandler classes that handle different
project state operations like folder expansion/collapse, file operations,
tab management, and git operations.
"""

import logging
from typing import Any, Dict, List

from ..base import AsyncHandler
from .manager import get_or_create_project_state_manager

logger = logging.getLogger(__name__)


class ProjectStateFolderExpandHandler(AsyncHandler):
    """Handler for expanding project folders."""
    
    @property
    def command_name(self) -> str:
        return "project_state_folder_expand"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Expand a folder in project state."""
        logger.info("ProjectStateFolderExpandHandler.execute called with message: %s", message)
        
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        folder_path = message.get("folder_path")
        source_client_session = message.get("source_client_session")  # This is our key
        
        logger.info("Extracted server_project_id: %s, folder_path: %s, source_client_session: %s", 
                   server_project_id, folder_path, source_client_session)
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not folder_path:
            raise ValueError("folder_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        logger.info("Getting project state manager...")
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        logger.info("Got manager: %s", manager)
        
        # With the new design, client session ID maps directly to project state
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            response = {
                "event": "project_state_folder_expand_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
            logger.error("Returning error response: %s", response)
            return response
        
        logger.info("Found project state for client session: %s", source_client_session)
        
        logger.info("Calling manager.expand_folder...")
        success = await manager.expand_folder(source_client_session, folder_path)
        logger.info("expand_folder returned: %s", success)
        
        if success:
            # Send updated state
            logger.info("Sending project state update...")
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
            logger.info("Project state update sent")
        
        response = {
            "event": "project_state_folder_expand_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "folder_path": folder_path,
            "success": success
        }
        
        logger.info("Returning response: %s", response)
        return response


class ProjectStateFolderCollapseHandler(AsyncHandler):
    """Handler for collapsing project folders."""
    
    @property
    def command_name(self) -> str:
        return "project_state_folder_collapse"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Collapse a folder in project state."""
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        folder_path = message.get("folder_path")
        source_client_session = message.get("source_client_session")  # This is our key
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not folder_path:
            raise ValueError("folder_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_folder_collapse_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.collapse_folder(source_client_session, folder_path)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
        
        return {
            "event": "project_state_folder_collapse_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "folder_path": folder_path,
            "success": success
        }


class ProjectStateFileOpenHandler(AsyncHandler):
    """Handler for opening files in project state."""
    
    @property
    def command_name(self) -> str:
        return "project_state_file_open"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Open a file in project state."""
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        file_path = message.get("file_path")
        source_client_session = message.get("source_client_session")  # This is our key
        set_active = message.get("set_active", True)
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_file_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "success": False,
                "set_active": set_active,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.open_file(source_client_session, file_path, set_active)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
        
        return {
            "event": "project_state_file_open_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "file_path": file_path,
            "success": success,
            "set_active": set_active
        }


class ProjectStateTabCloseHandler(AsyncHandler):
    """Handler for closing tabs in project state."""
    
    @property
    def command_name(self) -> str:
        return "project_state_tab_close"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Close a tab in project state."""
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        tab_id = message.get("tab_id")
        source_client_session = message.get("source_client_session")  # This is our key
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not tab_id:
            raise ValueError("tab_id is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_tab_close_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.close_tab(source_client_session, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
        
        return {
            "event": "project_state_tab_close_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "tab_id": tab_id,
            "success": success
        }


class ProjectStateSetActiveTabHandler(AsyncHandler):
    """Handler for setting active tab in project state."""
    
    @property
    def command_name(self) -> str:
        return "project_state_set_active_tab"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Set active tab in project state."""
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        tab_id = message.get("tab_id")  # Can be None to clear active tab
        source_client_session = message.get("source_client_session")  # This is our key
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_set_active_tab_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.set_active_tab(source_client_session, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
        
        return {
            "event": "project_state_set_active_tab_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "tab_id": tab_id,
            "success": success
        }


class ProjectStateDiffOpenHandler(AsyncHandler):
    """Handler for opening diff tabs based on git timeline references."""
    
    @property
    def command_name(self) -> str:
        return "project_state_diff_open"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Open a diff tab comparing file versions at different git timeline points."""
        server_project_id = message.get("project_id")  # Server-side UUID (for response)
        file_path = message.get("file_path")
        from_ref = message.get("from_ref")  # 'head', 'staged', 'working', 'commit'
        to_ref = message.get("to_ref")  # 'head', 'staged', 'working', 'commit'
        from_hash = message.get("from_hash")  # Optional commit hash for from_ref='commit'
        to_hash = message.get("to_hash")  # Optional commit hash for to_ref='commit'
        source_client_session = message.get("source_client_session")  # This is our key
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not from_ref:
            raise ValueError("from_ref is required")
        if not to_ref:
            raise ValueError("to_ref is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        # Validate reference types
        valid_refs = {'head', 'staged', 'working', 'commit'}
        if from_ref not in valid_refs:
            raise ValueError(f"Invalid from_ref: {from_ref}. Must be one of {valid_refs}")
        if to_ref not in valid_refs:
            raise ValueError(f"Invalid to_ref: {to_ref}. Must be one of {valid_refs}")
        
        # Validate commit hashes are provided when needed
        if from_ref == 'commit' and not from_hash:
            raise ValueError("from_hash is required when from_ref='commit'")
        if to_ref == 'commit' and not to_hash:
            raise ValueError("to_hash is required when to_ref='commit'")
        
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_diff_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.open_diff_tab(
            source_client_session, file_path, from_ref, to_ref, from_hash, to_hash
        )
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
            await manager._send_project_state_update(project_state, server_project_id)
        
        return {
            "event": "project_state_diff_open_response",
            "project_id": server_project_id,  # Return the server-side project ID
            "file_path": file_path,
            "from_ref": from_ref,
            "to_ref": to_ref,
            "from_hash": from_hash,
            "to_hash": to_hash,
            "success": success
        }


class ProjectStateGitStageHandler(AsyncHandler):
    """Handler for staging files in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_stage"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Stage a file in git for a project."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        logger.info("Staging file %s for project %s (client session: %s)", 
                   file_path, server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Stage the file
        success = git_manager.stage_file(file_path)
        
        if success:
            # Refresh entire project state to ensure consistency
            await manager._refresh_project_state(source_client_session)
        
        return {
            "event": "project_state_git_stage_response",
            "project_id": server_project_id,
            "file_path": file_path,
            "success": success
        }


class ProjectStateGitUnstageHandler(AsyncHandler):
    """Handler for unstaging files in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_unstage"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Unstage a file in git for a project."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        logger.info("Unstaging file %s for project %s (client session: %s)", 
                   file_path, server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Unstage the file
        success = git_manager.unstage_file(file_path)
        
        if success:
            # Refresh entire project state to ensure consistency
            await manager._refresh_project_state(source_client_session)
        
        return {
            "event": "project_state_git_unstage_response",
            "project_id": server_project_id,
            "file_path": file_path,
            "success": success
        }


class ProjectStateGitRevertHandler(AsyncHandler):
    """Handler for reverting files in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_revert"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Revert a file in git for a project."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        logger.info("Reverting file %s for project %s (client session: %s)", 
                   file_path, server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Revert the file
        success = git_manager.revert_file(file_path)
        
        if success:
            # Refresh entire project state to ensure consistency
            await manager._refresh_project_state(source_client_session)
        
        return {
            "event": "project_state_git_revert_response",
            "project_id": server_project_id,
            "file_path": file_path,
            "success": success
        }


# Handler for explicit client session cleanup
async def handle_client_session_cleanup(handler, payload: Dict[str, Any], source_client_session: str) -> Dict[str, Any]:
    """Handle explicit cleanup of a client session when server notifies of permanent disconnection."""
    client_session_id = payload.get('client_session_id')
    
    if not client_session_id:
        logger.error("client_session_id is required for client session cleanup")
        return {
            "event": "client_session_cleanup_response",
            "success": False,
            "error": "client_session_id is required"
        }
    
    logger.info("Handling explicit cleanup for client session: %s", client_session_id)
    
    # Get the project state manager
    manager = get_or_create_project_state_manager(handler.context, handler.control_channel)
    
    # Clean up the client session's project state
    manager.cleanup_projects_by_client_session(client_session_id)
    
    logger.info("Client session cleanup completed: %s", client_session_id)
    
    return {
        "event": "client_session_cleanup_response",
        "client_session_id": client_session_id,
        "success": True
    }