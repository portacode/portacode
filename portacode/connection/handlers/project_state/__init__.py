"""Project State Management Package

This package provides a modular architecture for managing project state in the
Portacode application, including file system monitoring, git integration,
tab management, and real-time state synchronization.

The package is organized into the following modules:

- models: Data structures and models (ProjectState, FileItem, TabInfo, etc.)
- git_manager: Git operations and repository management
- file_system_watcher: File system change monitoring
- manager: Central project state coordinator
- handlers: Request handlers for various operations
- utils: Utility functions and helpers

Usage:
    from project_state.manager import get_or_create_project_state_manager
    from project_state.handlers import ProjectStateFolderExpandHandler
    from project_state.models import ProjectState, FileItem
"""

# Public API exports
from .models import (
    ProjectState,
    FileItem,
    TabInfo,
    MonitoredFolder,
    GitFileChange,
    GitDetailedStatus
)

from .manager import (
    ProjectStateManager,
    get_or_create_project_state_manager,
    reset_global_project_state_manager,
    debug_global_manager_state
)

from .git_manager import GitManager
from .file_system_watcher import FileSystemWatcher

from .handlers import (
    ProjectStateFolderExpandHandler,
    ProjectStateFolderCollapseHandler,
    ProjectStateFileOpenHandler,
    ProjectStateTabCloseHandler,
    ProjectStateSetActiveTabHandler,
    ProjectStateDiffOpenHandler,
    ProjectStateGitStageHandler,
    ProjectStateGitUnstageHandler,
    ProjectStateGitRevertHandler,
    ProjectStateGitCommitHandler,
    handle_client_session_cleanup
)

from .utils import generate_tab_key

__all__ = [
    # Models
    'ProjectState',
    'FileItem',
    'TabInfo',
    'MonitoredFolder',
    'GitFileChange',
    'GitDetailedStatus',
    
    # Core classes
    'ProjectStateManager',
    'GitManager',
    'FileSystemWatcher',
    
    # Manager functions
    'get_or_create_project_state_manager',
    'reset_global_project_state_manager',
    'debug_global_manager_state',
    
    # Handlers
    'ProjectStateFolderExpandHandler',
    'ProjectStateFolderCollapseHandler',
    'ProjectStateFileOpenHandler',
    'ProjectStateTabCloseHandler',
    'ProjectStateSetActiveTabHandler',
    'ProjectStateDiffOpenHandler',
    'ProjectStateGitStageHandler',
    'ProjectStateGitUnstageHandler',
    'ProjectStateGitRevertHandler',
    'ProjectStateGitCommitHandler',
    'handle_client_session_cleanup',
    
    # Utils
    'generate_tab_key'
]