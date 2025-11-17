"""Request handlers for project state management operations.

This module contains all the AsyncHandler classes that handle different
project state operations like folder expansion/collapse, file operations,
tab management, and git operations.
"""

import logging
from typing import Any, Dict, List

from ..base import AsyncHandler
from ..chunked_content import create_chunked_response
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
        """Stage file(s) in git for a project. Supports both single file and bulk operations."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")  # Single file (backward compatibility)
        file_paths = message.get("file_paths")  # Multiple files (bulk operation)
        stage_all = message.get("stage_all", False)  # Stage all changes
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        # Determine operation mode
        if stage_all:
            operation_desc = "staging all changes"
            file_paths_to_stage = []
        elif file_paths:
            operation_desc = f"staging {len(file_paths)} files"
            file_paths_to_stage = file_paths
        elif file_path:
            operation_desc = f"staging file {file_path}"
            file_paths_to_stage = [file_path]
        else:
            raise ValueError("Either file_path, file_paths, or stage_all must be provided")
        
        logger.info("%s for project %s (client session: %s)", 
                   operation_desc.capitalize(), server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Perform the staging operation
        if stage_all:
            success = git_manager.stage_all_changes()
        elif len(file_paths_to_stage) == 1:
            success = git_manager.stage_file(file_paths_to_stage[0])
        else:
            success = git_manager.stage_files(file_paths_to_stage)

        if success:
            # Refresh git status only (no filesystem changes from staging)
            await manager._refresh_project_state(
                source_client_session,
                git_only=True,
                reason="git_stage",
            )
        
        # Build response
        response = {
            "event": "project_state_git_stage_response",
            "project_id": server_project_id,
            "success": success
        }
        
        # Include appropriate file information in response for backward compatibility
        if file_path:
            response["file_path"] = file_path
        if file_paths:
            response["file_paths"] = file_paths
        if stage_all:
            response["stage_all"] = True
            
        return response


class ProjectStateGitUnstageHandler(AsyncHandler):
    """Handler for unstaging files in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_unstage"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Unstage file(s) in git for a project. Supports both single file and bulk operations."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")  # Single file (backward compatibility)
        file_paths = message.get("file_paths")  # Multiple files (bulk operation)
        unstage_all = message.get("unstage_all", False)  # Unstage all changes
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        # Determine operation mode
        if unstage_all:
            operation_desc = "unstaging all changes"
            file_paths_to_unstage = []
        elif file_paths:
            operation_desc = f"unstaging {len(file_paths)} files"
            file_paths_to_unstage = file_paths
        elif file_path:
            operation_desc = f"unstaging file {file_path}"
            file_paths_to_unstage = [file_path]
        else:
            raise ValueError("Either file_path, file_paths, or unstage_all must be provided")
        
        logger.info("%s for project %s (client session: %s)", 
                   operation_desc.capitalize(), server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Perform the unstaging operation
        if unstage_all:
            success = git_manager.unstage_all_changes()
        elif len(file_paths_to_unstage) == 1:
            success = git_manager.unstage_file(file_paths_to_unstage[0])
        else:
            success = git_manager.unstage_files(file_paths_to_unstage)

        if success:
            # Refresh git status only (no filesystem changes from unstaging)
            await manager._refresh_project_state(
                source_client_session,
                git_only=True,
                reason="git_unstage",
            )
        
        # Build response
        response = {
            "event": "project_state_git_unstage_response",
            "project_id": server_project_id,
            "success": success
        }
        
        # Include appropriate file information in response for backward compatibility
        if file_path:
            response["file_path"] = file_path
        if file_paths:
            response["file_paths"] = file_paths
        if unstage_all:
            response["unstage_all"] = True
            
        return response


class ProjectStateGitRevertHandler(AsyncHandler):
    """Handler for reverting files in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_revert"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Revert file(s) in git for a project. Supports both single file and bulk operations."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")  # Single file (backward compatibility)
        file_paths = message.get("file_paths")  # Multiple files (bulk operation)
        revert_all = message.get("revert_all", False)  # Revert all changes
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        # Determine operation mode
        if revert_all:
            operation_desc = "reverting all changes"
            file_paths_to_revert = []
        elif file_paths:
            operation_desc = f"reverting {len(file_paths)} files"
            file_paths_to_revert = file_paths
        elif file_path:
            operation_desc = f"reverting file {file_path}"
            file_paths_to_revert = [file_path]
        else:
            raise ValueError("Either file_path, file_paths, or revert_all must be provided")
        
        logger.info("%s for project %s (client session: %s)", 
                   operation_desc.capitalize(), server_project_id, source_client_session)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Perform the revert operation
        if revert_all:
            success = git_manager.revert_all_changes()
        elif len(file_paths_to_revert) == 1:
            success = git_manager.revert_file(file_paths_to_revert[0])
        else:
            success = git_manager.revert_files(file_paths_to_revert)
        
        if success:
            # Refresh entire project state to ensure consistency
            await manager._refresh_project_state(
                source_client_session,
                reason="git_revert",
            )
        
        # Build response
        response = {
            "event": "project_state_git_revert_response",
            "project_id": server_project_id,
            "success": success
        }
        
        # Include appropriate file information in response for backward compatibility
        if file_path:
            response["file_path"] = file_path
        if file_paths:
            response["file_paths"] = file_paths
        if revert_all:
            response["revert_all"] = True
            
        return response


class ProjectStateGitCommitHandler(AsyncHandler):
    """Handler for committing staged changes in git for a project."""
    
    @property
    def command_name(self) -> str:
        return "project_state_git_commit"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Commit staged changes with the given commit message."""
        server_project_id = message.get("project_id")
        commit_message = message.get("commit_message")
        source_client_session = message.get("source_client_session")
        
        if not server_project_id:
            raise ValueError("project_id is required")
        if not commit_message:
            raise ValueError("commit_message is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        logger.info("Committing changes for project %s (client session: %s) with message: %s", 
                   server_project_id, source_client_session, commit_message[:50] + "..." if len(commit_message) > 50 else commit_message)
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get git manager for the client session
        git_manager = manager.git_managers.get(source_client_session)
        if not git_manager:
            raise ValueError("No git repository found for this project")
        
        # Commit the staged changes
        success = False
        error_message = None
        commit_hash = None
        
        try:
            success = git_manager.commit_changes(commit_message)
            if success:
                # Get the commit hash of the new commit
                commit_hash = git_manager.get_head_commit_hash()

                # Refresh git status only (no filesystem changes from commit)
                await manager._refresh_project_state(
                    source_client_session,
                    git_only=True,
                    reason="git_commit",
                )
        except Exception as e:
            error_message = str(e)
            logger.error("Error during commit: %s", error_message)
        
        return {
            "event": "project_state_git_commit_response",
            "project_id": server_project_id,
            "commit_message": commit_message,
            "success": success,
            "error": error_message,
            "commit_hash": commit_hash
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
    await manager.cleanup_projects_by_client_session(client_session_id)
    
    logger.info("Client session cleanup completed: %s", client_session_id)
    
    return {
        "event": "client_session_cleanup_response",
        "client_session_id": client_session_id,
        "success": True
    }


class ProjectStateDiffContentHandler(AsyncHandler):
    """Handler for requesting specific diff content for diff tabs."""
    
    @property
    def command_name(self) -> str:
        return "project_state_diff_content_request"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Request specific content for a diff tab (original, modified, or html_diff)."""
        server_project_id = message.get("project_id")
        file_path = message.get("file_path")
        from_ref = message.get("from_ref")
        to_ref = message.get("to_ref")
        from_hash = message.get("from_hash")
        to_hash = message.get("to_hash")
        content_type = message.get("content_type")  # 'original', 'modified', 'html_diff'
        source_client_session = message.get("source_client_session")

        # Validate required fields
        if not server_project_id:
            raise ValueError("project_id is required")
        if not file_path:
            raise ValueError("file_path is required")
        if not from_ref:
            raise ValueError("from_ref is required")
        if not to_ref:
            raise ValueError("to_ref is required")
        if not content_type:
            raise ValueError("content_type is required")
        if not source_client_session:
            raise ValueError("source_client_session is required")
        
        # Validate content_type
        valid_content_types = ["original", "modified", "html_diff", "all"]
        if content_type not in valid_content_types:
            raise ValueError(f"content_type must be one of: {valid_content_types}")
        
        # Get the project state manager
        manager = get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Get the project state for this client session
        if source_client_session not in manager.projects:
            raise ValueError(f"No project state found for client session: {source_client_session}")
        
        project_state = manager.projects[source_client_session]
        
        try:
            # Find the diff tab with matching parameters
            matching_tab = None
            for tab in project_state.open_tabs.values():
                if tab.tab_type == "diff" and tab.file_path == file_path:
                    # Get diff parameters from metadata
                    tab_metadata = getattr(tab, 'metadata', {}) or {}
                    tab_from_ref = tab_metadata.get('from_ref')
                    tab_to_ref = tab_metadata.get('to_ref')
                    tab_from_hash = tab_metadata.get('from_hash')
                    tab_to_hash = tab_metadata.get('to_hash')
                    
                    if (tab_from_ref == from_ref and 
                        tab_to_ref == to_ref and
                        tab_from_hash == from_hash and
                        tab_to_hash == to_hash):
                        matching_tab = tab
                        break
            
            if not matching_tab:
                # Debug information
                logger.error(f"No diff tab found for file_path={file_path}, from_ref={from_ref}, to_ref={to_ref}")
                logger.error(f"Available diff tabs: {[(tab.file_path, getattr(tab, 'metadata', {})) for tab in project_state.open_tabs.values() if tab.tab_type == 'diff']}")
                raise ValueError(f"No diff tab found matching the specified parameters: file_path={file_path}, from_ref={from_ref}, to_ref={to_ref}")
            
            # Get the requested content based on type
            content = None
            if content_type == "original":
                content = matching_tab.original_content
            elif content_type == "modified":
                content = matching_tab.modified_content
            elif content_type == "html_diff":
                # For html_diff, we need to get the HTML diff versions from metadata
                html_diff_versions = getattr(matching_tab, 'metadata', {}).get('html_diff_versions')
                if html_diff_versions:
                    import json
                    content = json.dumps(html_diff_versions)
            elif content_type == "all":
                # Return all content types as a JSON object
                html_diff_versions = getattr(matching_tab, 'metadata', {}).get('html_diff_versions')
                import json
                content = json.dumps({
                    "original_content": matching_tab.original_content,
                    "modified_content": matching_tab.modified_content,
                    "html_diff_versions": html_diff_versions
                })
            
            # If content is None or incomplete for "all", regenerate if needed
            if content is None or (content_type == "all" and not all([matching_tab.original_content, matching_tab.modified_content])):
                if content_type in ["original", "modified", "all"]:
                    # Re-generate the diff content if needed
                    await manager.open_diff_tab(
                        source_client_session,
                        file_path,
                        from_ref,
                        to_ref,
                        from_hash,
                        to_hash
                    )
                    
                    # Try to get content again after regeneration (use same matching logic)
                    updated_tab = None
                    for tab in project_state.open_tabs.values():
                        if tab.tab_type == "diff" and tab.file_path == file_path:
                            tab_metadata = getattr(tab, 'metadata', {}) or {}
                            if (tab_metadata.get('from_ref') == from_ref and 
                                tab_metadata.get('to_ref') == to_ref and
                                tab_metadata.get('from_hash') == from_hash and
                                tab_metadata.get('to_hash') == to_hash):
                                updated_tab = tab
                                break
                    
                    if updated_tab:
                        if content_type == "original":
                            content = updated_tab.original_content
                        elif content_type == "modified":
                            content = updated_tab.modified_content
                        elif content_type == "html_diff":
                            html_diff_versions = getattr(updated_tab, 'metadata', {}).get('html_diff_versions')
                            if html_diff_versions:
                                import json
                                content = json.dumps(html_diff_versions)
                        elif content_type == "all":
                            html_diff_versions = getattr(updated_tab, 'metadata', {}).get('html_diff_versions')
                            import json
                            content = json.dumps({
                                "original_content": updated_tab.original_content,
                                "modified_content": updated_tab.modified_content,
                                "html_diff_versions": html_diff_versions
                            })
            
            success = content is not None
            base_response = {
                "event": "project_state_diff_content_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "content_type": content_type,
                "success": success
            }

            # Add request_id if present in original message
            if "request_id" in message:
                base_response["request_id"] = message["request_id"]
            
            if from_hash:
                base_response["from_hash"] = from_hash
            if to_hash:
                base_response["to_hash"] = to_hash
            
            if success:
                # Create chunked responses for large content
                responses = create_chunked_response(base_response, "content", content)
                
                # Send all responses
                for response in responses:
                    await self.send_response(response, project_id=server_project_id)
                
                logger.info(f"Sent diff content response in {len(responses)} chunk(s) for {content_type} content")
            else:
                base_response["error"] = f"Failed to load {content_type} content for diff"
                base_response["chunked"] = False
                await self.send_response(base_response, project_id=server_project_id)
            
            return  # AsyncHandler doesn't return responses, it sends them
            
        except Exception as e:
            logger.error("Error processing diff content request: %s", e)
            error_response = {
                "event": "project_state_diff_content_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "content_type": content_type,
                "success": False,
                "error": str(e),
                "chunked": False
            }

            # Add request_id if present in original message
            if "request_id" in message:
                error_response["request_id"] = message["request_id"]
            await self.send_response(error_response, project_id=server_project_id)
