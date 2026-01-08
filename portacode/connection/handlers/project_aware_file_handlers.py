"""Project-aware file operation handlers that integrate with project state management."""

import os
import logging
from typing import Any, Dict
from pathlib import Path

from .base import SyncHandler
from .project_state.manager import get_or_create_project_state_manager

logger = logging.getLogger(__name__)


class ProjectAwareFileWriteHandler(SyncHandler):
    """Handler for writing file contents that updates project state tabs."""
    
    @property
    def command_name(self) -> str:
        return "file_write"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Write file contents and update project state tabs."""
        file_path = message.get("path")
        content = message.get("content", "")
        # Optimistic lock: ensure the client saw the correct file state
        expected_mtime = message.get("expected_mtime")
        if expected_mtime is not None:
            try:
                current_mtime = os.path.getmtime(file_path)
            except FileNotFoundError:
                raise ValueError(f"File not found: {file_path}")
            if current_mtime != expected_mtime:
                raise ValueError(
                    f"File was modified on disk (current {current_mtime} != expected {expected_mtime})"
                )
        
        if not file_path:
            raise ValueError("path parameter is required")
        
        try:
            # Create parent directories if they don't exist
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            
            # Write the file
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Update project state tabs that have this file open
            try:
                manager = get_or_create_project_state_manager(self.context, self.control_channel)
                
                # Update all project states that have tabs open for this file
                for client_session_id, project_state in manager.projects.items():
                    tabs_updated = False
                    
                    # Check if any tabs have this file path
                    for tab_id, tab in project_state.openTabs.items():
                        if tab.get('file_path') == file_path:
                            # Update tab content to match what was just saved
                            tab['content'] = content
                            tab['is_dirty'] = False
                            tab['originalContent'] = content
                            tabs_updated = True
                            logger.info(f"Updated tab {tab_id} content for file {file_path} in project state {client_session_id}")
                    
                    # Broadcast updated project state if we made changes
                    if tabs_updated:
                        logger.info(f"Broadcasting project state update for client session {client_session_id}")
                        manager.broadcast_project_state(client_session_id)
                        
            except Exception as e:
                logger.warning(f"Failed to update project state after file write: {e}")
                # Don't fail the file write just because project state update failed
            
            return {
                "event": "file_write_response",
                "path": file_path,
                "bytes_written": len(content.encode('utf-8')),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {file_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to write file: {e}")


class ProjectAwareFileCreateHandler(SyncHandler):
    """Handler for creating new files that updates project state."""
    
    @property
    def command_name(self) -> str:
        return "file_create"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new file and refresh project state."""
        parent_path = message.get("parent_path")
        file_name = message.get("file_name")
        content = message.get("content", "")
        
        if not parent_path:
            raise ValueError("parent_path parameter is required")
        if not file_name:
            raise ValueError("file_name parameter is required")
        
        # Validate file name (no path separators or special chars)
        if "/" in file_name or "\\" in file_name or file_name in [".", ".."]:
            raise ValueError("Invalid file name")
        
        try:
            # Ensure parent directory exists
            parent_dir = Path(parent_path)
            if not parent_dir.exists():
                raise ValueError(f"Parent directory does not exist: {parent_path}")
            if not parent_dir.is_dir():
                raise ValueError(f"Parent path is not a directory: {parent_path}")
            
            # Create the full file path
            file_path = parent_dir / file_name
            
            # Check if file already exists
            if file_path.exists():
                raise ValueError(f"File already exists: {file_name}")
            
            # Create the file
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Trigger project state refresh
            try:
                manager = get_or_create_project_state_manager(self.context, self.control_channel)
                
                # Schedule the refresh (don't await since this is sync handler)
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(manager.refresh_project_state_for_file_change(str(file_path)))
                        logger.info(f"Scheduled project state refresh after file creation: {file_path}")
                except Exception as e:
                    logger.warning(f"Could not schedule project state refresh: {e}")
                        
            except Exception as e:
                logger.warning(f"Failed to refresh project state after file creation: {e}")
                # Don't fail the file creation just because project state refresh failed
            
            return {
                "event": "file_create_response",
                "parent_path": parent_path,
                "file_name": file_name,
                "file_path": str(file_path),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {parent_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to create file: {e}")


class ProjectAwareFolderCreateHandler(SyncHandler):
    """Handler for creating new folders that updates project state."""
    
    @property
    def command_name(self) -> str:
        return "folder_create"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new folder and refresh project state."""
        parent_path = message.get("parent_path")
        folder_name = message.get("folder_name")
        
        if not parent_path:
            raise ValueError("parent_path parameter is required")
        if not folder_name:
            raise ValueError("folder_name parameter is required")
        
        # Validate folder name (no path separators or special chars)
        if "/" in folder_name or "\\" in folder_name or folder_name in [".", ".."]:
            raise ValueError("Invalid folder name")
        
        try:
            # Ensure parent directory exists
            parent_dir = Path(parent_path)
            if not parent_dir.exists():
                raise ValueError(f"Parent directory does not exist: {parent_path}")
            if not parent_dir.is_dir():
                raise ValueError(f"Parent path is not a directory: {parent_path}")
            
            # Create the full folder path
            folder_path = parent_dir / folder_name
            
            # Check if folder already exists
            if folder_path.exists():
                raise ValueError(f"Folder already exists: {folder_name}")
            
            # Create the folder
            folder_path.mkdir(parents=False, exist_ok=False)
            
            # Trigger project state refresh
            try:
                manager = get_or_create_project_state_manager(self.context, self.control_channel)
                
                # Schedule the refresh (don't await since this is sync handler)
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(manager.refresh_project_state_for_file_change(str(folder_path)))
                        logger.info(f"Scheduled project state refresh after folder creation: {folder_path}")
                except Exception as e:
                    logger.warning(f"Could not schedule project state refresh: {e}")
                        
            except Exception as e:
                logger.warning(f"Failed to refresh project state after folder creation: {e}")
                # Don't fail the folder creation just because project state refresh failed
            
            return {
                "event": "folder_create_response",
                "parent_path": parent_path,
                "folder_name": folder_name,
                "folder_path": str(folder_path),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {parent_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to create folder: {e}")