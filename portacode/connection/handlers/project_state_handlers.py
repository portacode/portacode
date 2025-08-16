"""Project state handlers - modular architecture.

This module serves as a compatibility layer that imports all the project state
handlers from the new modular structure. This ensures existing code continues
to work while providing access to the new architecture.

The original monolithic file has been broken down into a modular structure
located in the project_state/ subdirectory. All functionality, logging, and
documentation has been preserved while improving maintainability.

For detailed information about the new structure, see:
project_state/README.md
"""

# Import everything from the modular structure for backward compatibility
from .project_state import *

# Ensure all handlers are available at module level for existing imports
from .project_state.handlers import (
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
    handle_client_session_cleanup
)

from .project_state.manager import (
    get_or_create_project_state_manager,
    reset_global_project_state_manager,
    debug_global_manager_state
)

from .project_state.utils import generate_tab_key

# Re-export with the old private function names for backward compatibility
_get_or_create_project_state_manager = get_or_create_project_state_manager
_reset_global_project_state_manager = reset_global_project_state_manager
_debug_global_manager_state = debug_global_manager_state
