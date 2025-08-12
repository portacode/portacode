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