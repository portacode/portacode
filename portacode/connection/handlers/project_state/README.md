# Project State Management - Modular Architecture

This document explains the modular architecture of the project state management system, which was refactored from a single 3000+ line file into a well-organized, maintainable structure.

## Overview

The project state management system handles:
- File system monitoring and change detection
- Git repository integration and operations  
- Tab management (file tabs, diff tabs, etc.)
- Real-time project state synchronization
- Client session management

## Architecture

The original monolithic `project_state_handlers.py` file has been broken down into the following modules:

```
project_state/
├── __init__.py           # Public API exports
├── models.py             # Data structures and models
├── git_manager.py        # Git operations and repository management
├── file_system_watcher.py # File system change monitoring
├── manager.py            # Central project state coordinator
├── handlers.py           # Request handlers for various operations
├── utils.py              # Utility functions and helpers
└── README.md             # This documentation
```

## Module Details

### 1. `models.py` - Data Structures

Contains all the dataclasses and models used throughout the system:

- **`ProjectState`**: Complete state of a project (files, git status, tabs, etc.)
- **`FileItem`**: Represents a file or directory with metadata
- **`TabInfo`**: Represents an editor tab with content and metadata
- **`MonitoredFolder`**: Represents a folder being monitored for changes
- **`GitFileChange`**: Represents a single file change in git
- **`GitDetailedStatus`**: Detailed git status with file hashes

**Key Features:**
- All models are dataclasses for easy serialization
- Comprehensive typing throughout
- Self-documenting field definitions

### 2. `git_manager.py` - Git Operations

Handles all Git-related functionality:

**Core Responsibilities:**
- Repository detection and initialization
- File status tracking (staged, modified, untracked, etc.)
- Diff generation with syntax highlighting
- Content retrieval at different git references
- Git commands (stage, unstage, revert)

**Key Features:**
- Graceful fallback when GitPython is not available
- Performance optimizations for large files
- HTML diff generation with syntax highlighting
- Support for diff-match-patch algorithm
- Cross-platform path handling

**Performance Safeguards:**
- Timeouts for diff generation
- Size limits for large files
- Batch syntax highlighting for better performance
- Simplified diff view for very large files

### 3. `file_system_watcher.py` - File System Monitoring

Monitors file system changes using the watchdog library:

**Core Responsibilities:**
- Cross-platform file system monitoring
- Event filtering and debouncing
- Git repository change detection
- Thread-safe async event handling

**Key Features:**
- Selective monitoring of relevant file changes
- Special handling for .git directory changes
- Debug tracing for troubleshooting
- Graceful fallback when watchdog is not available

**Event Filtering:**
- Skips temporary and debug files
- Focuses on meaningful file changes
- Monitors git-specific files for status updates

### 4. `manager.py` - Central Coordinator

The main `ProjectStateManager` class that orchestrates all operations:

**Core Responsibilities:**
- Project state initialization and lifecycle
- Folder expansion/collapse logic
- Tab management (open, close, activate)
- File system change processing
- Client session management
- State synchronization and updates

**Key Features:**
- Singleton pattern for global state management
- Debounced file change processing
- Flattened item structure for performance
- Detailed debug logging and state tracking
- Client session isolation

**State Management:**
- Each client session has independent project state
- Monitored folders with expansion states
- Real-time synchronization with clients
- Orphaned state cleanup

### 5. `handlers.py` - Request Handlers

AsyncHandler classes for different operations:

**Handler Classes:**
- `ProjectStateFolderExpandHandler` - Expand folders
- `ProjectStateFolderCollapseHandler` - Collapse folders  
- `ProjectStateFileOpenHandler` - Open files in tabs
- `ProjectStateTabCloseHandler` - Close tabs
- `ProjectStateSetActiveTabHandler` - Set active tab
- `ProjectStateDiffOpenHandler` - Open diff tabs
- `ProjectStateGitStageHandler` - Stage files
- `ProjectStateGitUnstageHandler` - Unstage files
- `ProjectStateGitRevertHandler` - Revert files

**Key Features:**
- Consistent error handling and validation
- Server/client session mapping
- Automatic state updates after operations
- Comprehensive logging

### 6. `utils.py` - Utility Functions

Shared utility functions:

- **`generate_tab_key()`**: Creates unique keys for different tab types
- Support for file tabs, diff tabs, untitled tabs
- Handles git reference parameters for diff tabs

## Preserved Functionality

All functionality from the original file has been preserved:

✅ **Complete Feature Parity**
- All classes, methods, and functions maintained
- Original behavior preserved exactly
- All logging statements preserved
- All documentation comments maintained

✅ **Performance Optimizations**
- Large file handling safeguards
- Diff generation timeouts
- Syntax highlighting optimizations
- Memory-efficient processing

✅ **Error Handling**
- Graceful fallbacks for missing dependencies
- Cross-platform compatibility
- Comprehensive exception handling
- Debug mode support

✅ **Git Integration**
- Full GitPython integration
- Advanced diff capabilities
- Multi-reference comparisons
- HTML diff generation

## Usage Examples

### Basic Usage

```python
# Import the main components
from project_state import (
    get_or_create_project_state_manager,
    ProjectState,
    GitManager
)

# Get the global project state manager
manager = get_or_create_project_state_manager(context, control_channel)

# Initialize a project
project_state = await manager.initialize_project_state(
    client_session_id="session123",
    project_folder_path="/path/to/project"
)
```

### Using Individual Components

```python
# Use GitManager independently
from project_state import GitManager

git_manager = GitManager("/path/to/project")
if git_manager.is_git_repo:
    status = git_manager.get_detailed_status()
    print(f"Branch: {git_manager.get_branch_name()}")
```

### Handler Integration

```python
# Import handlers for request processing
from project_state import ProjectStateFolderExpandHandler

# Use in your handler registry
handler = ProjectStateFolderExpandHandler()
result = await handler.execute(message)
```

## Migration Guide

The refactoring maintains complete backward compatibility:

1. **Existing imports continue to work** - The original module structure is preserved
2. **No API changes** - All function signatures remain the same
3. **Same behavior** - All functionality works exactly as before
4. **Performance improvements** - Better organization enables easier optimization

## Benefits of Modular Architecture

### 1. **Maintainability**
- Single responsibility principle
- Easier to locate and modify specific functionality
- Reduced cognitive load when working on specific features

### 2. **Testability**
- Individual modules can be tested in isolation
- Clear dependencies between components
- Easier mocking for unit tests

### 3. **Reusability**
- Components can be used independently
- GitManager can be used outside project state context
- Models can be shared across different systems

### 4. **Performance**
- Better import optimization
- Reduced memory footprint for unused components
- Clearer performance bottleneck identification

### 5. **Documentation**
- Self-documenting module structure
- Clear separation of concerns
- Easier onboarding for new developers

## Development Guidelines

### Adding New Features

1. **Identify the appropriate module** based on the feature's responsibility
2. **Follow existing patterns** for consistency
3. **Update the `__init__.py`** to export new public APIs
4. **Add comprehensive logging** for debugging
5. **Include performance safeguards** for resource-intensive operations

### Modifying Existing Features

1. **Maintain backward compatibility** unless breaking changes are explicitly approved
2. **Preserve all logging statements** for debugging continuity
3. **Update documentation** to reflect changes
4. **Test across all affected modules** to ensure integration works

### Best Practices

- Use type hints consistently
- Follow the established logging patterns
- Include docstrings for all public methods
- Handle errors gracefully with appropriate fallbacks
- Consider performance implications of changes

## Troubleshooting

### Common Issues

1. **Import Errors**: Ensure you're importing from the correct module path
2. **Missing Dependencies**: Check for optional dependencies (GitPython, watchdog, etc.)
3. **Performance Issues**: Review file size limits and timeout settings
4. **State Synchronization**: Check client session mapping and event handling

### Debug Mode

Enable debug mode for detailed state tracking:

```python
manager.set_debug_mode(True, "/path/to/debug.json")
```

This creates a JSON file with complete project state information for analysis.

## Future Enhancements

The modular architecture enables several future improvements:

1. **Plugin System**: Easy addition of new file system watchers or git providers
2. **Caching Layer**: Independent caching modules for performance
3. **API Versioning**: Separate handler versions for backward compatibility
4. **Microservice Architecture**: Components can be extracted to separate services
5. **Enhanced Testing**: Module-specific test suites with better coverage

## Conclusion

The modular architecture maintains all functionality while providing a solid foundation for future development. The separation of concerns makes the codebase more maintainable, testable, and extensible, while preserving the robust feature set that was built in the original implementation.