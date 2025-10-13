"""File system monitoring for project state changes.

This module provides the FileSystemWatcher class which monitors file system
changes using the watchdog library and triggers project state updates when
files or directories are modified.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Cross-platform file system monitoring
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
    logger.info("Watchdog library available for file system monitoring")
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = None
    logger.warning("Watchdog library not available - file system monitoring disabled")


class FileSystemWatcher:
    """Watches file system changes for project folders."""

    def __init__(self, project_manager):
        self.project_manager = project_manager  # Reference to ProjectStateManager
        self.observer: Optional[Observer] = None
        self.event_handler: Optional[FileSystemEventHandler] = None
        self.watched_paths: Set[str] = set()
        self.watch_handles: dict = {}  # Map path -> watch handle for proper cleanup
        # Store reference to the event loop for thread-safe async task creation
        try:
            self.event_loop = asyncio.get_running_loop()
            logger.debug("🔍 [TRACE] ✅ Captured event loop reference for file system watcher: %s", self.event_loop)
        except RuntimeError:
            self.event_loop = None
            logger.debug("🔍 [TRACE] ❌ No running event loop found - file system events may not work correctly")
        
        logger.debug("🔍 [TRACE] WATCHDOG_AVAILABLE: %s", WATCHDOG_AVAILABLE)
        if WATCHDOG_AVAILABLE:
            logger.debug("🔍 [TRACE] Initializing file system watcher...")
            self._initialize_watcher()
        else:
            logger.debug("🔍 [TRACE] ❌ Watchdog not available - file monitoring disabled")
    
    def _initialize_watcher(self):
        """Initialize file system watcher."""
        if not WATCHDOG_AVAILABLE:
            logger.warning("Watchdog not available, file monitoring disabled")
            return
        
        class ProjectEventHandler(FileSystemEventHandler):
            def __init__(self, manager, watcher):
                self.manager = manager
                self.watcher = watcher
                super().__init__()
            
            def on_any_event(self, event):
                logger.debug("🔍 [TRACE] FileSystemWatcher detected event: %s on path: %s", event.event_type, event.src_path)
                
                # Skip debug files to avoid feedback loops
                if event.src_path.endswith('project_state_debug.json'):
                    logger.debug("🔍 [TRACE] Skipping debug file: %s", event.src_path)
                    return
                
                # Only process events that represent actual content changes
                # Skip opened/closed events that don't indicate file modifications
                if event.event_type in ('opened', 'closed'):
                    logger.debug("🔍 [TRACE] Skipping opened/closed event: %s", event.event_type)
                    return
                
                # Handle .git folder events separately for git status monitoring
                path_parts = Path(event.src_path).parts
                if '.git' in path_parts:
                    logger.debug("🔍 [TRACE] Processing .git folder event: %s", event.src_path)
                    # Get the relative path within .git directory
                    try:
                        git_index = path_parts.index('.git')
                        git_relative_path = '/'.join(path_parts[git_index + 1:])
                        git_file = Path(event.src_path).name
                        
                        logger.debug("🔍 [TRACE] Git file details - relative_path: %s, file: %s", git_relative_path, git_file)
                        
                        # Monitor git files that indicate repository state changes
                        should_monitor_git_file = (
                            git_file == 'index' or  # Staging area changes
                            git_file == 'index.lock' or  # Staging operations in progress
                            git_file == 'HEAD' or   # Branch switches
                            git_relative_path.startswith('refs/heads/') or  # Branch updates
                            git_relative_path.startswith('refs/remotes/') or  # Remote tracking branches
                            git_relative_path.startswith('logs/refs/heads/') or  # Branch history
                            git_relative_path.startswith('logs/HEAD') or  # HEAD history
                            git_relative_path.startswith('objects/') and event.event_type in ('created', 'modified')  # New objects (commits, blobs)
                        )
                        
                        if should_monitor_git_file:
                            logger.debug("🔍 [TRACE] ✅ Git file matches monitoring criteria: %s", event.src_path)
                        else:
                            logger.debug("🔍 [TRACE] ❌ Git file does NOT match monitoring criteria - SKIPPING: %s", event.src_path)
                            return  # Skip other .git files
                    except (ValueError, IndexError):
                        logger.debug("🔍 [TRACE] ❌ Could not parse .git path - SKIPPING: %s", event.src_path)
                        return  # Skip if can't parse .git path
                else:
                    logger.debug("🔍 [TRACE] Processing non-git file event: %s", event.src_path)
                    # Only log significant file changes, not every single event
                    if event.event_type in ['created', 'deleted'] or event.src_path.endswith(('.py', '.js', '.html', '.css', '.json', '.md')):
                        logger.debug("File system event: %s - %s", event.event_type, os.path.basename(event.src_path))
                    else:
                        logger.debug("File event: %s", os.path.basename(event.src_path))
                
                # Schedule async task in the main event loop from this watchdog thread
                logger.debug("🔍 [TRACE] About to schedule async handler - event_loop exists: %s, closed: %s", 
                           self.watcher.event_loop is not None, 
                           self.watcher.event_loop.is_closed() if self.watcher.event_loop else "N/A")
                
                if self.watcher.event_loop and not self.watcher.event_loop.is_closed():
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.manager._handle_file_change(event), 
                            self.watcher.event_loop
                        )
                        logger.debug("🔍 [TRACE] ✅ Successfully scheduled file change handler for: %s", event.src_path)
                    except Exception as e:
                        logger.debug("🔍 [TRACE] ❌ Failed to schedule file change handler: %s", e)
                else:
                    logger.debug("🔍 [TRACE] ❌ No event loop available to handle file change: %s", event.src_path)
        
        self.event_handler = ProjectEventHandler(self.project_manager, self)
        self.observer = Observer()
    
    def start_watching(self, path: str):
        """Start watching a specific path."""
        if not WATCHDOG_AVAILABLE or not self.observer:
            logger.warning("Watchdog not available, cannot start watching: %s", path)
            return

        if path not in self.watched_paths:
            try:
                # Use recursive=False to watch only direct contents of each folder
                watch_handle = self.observer.schedule(self.event_handler, path, recursive=False)
                self.watched_paths.add(path)
                self.watch_handles[path] = watch_handle  # Store handle for cleanup
                logger.info("Started watching path (non-recursive): %s", path)

                if not self.observer.is_alive():
                    self.observer.start()
                    logger.info("Started file system observer")
            except Exception as e:
                logger.error("Error starting file watcher for %s: %s", path, e)
        else:
            logger.debug("Path already being watched: %s", path)
    
    def start_watching_git_directory(self, git_path: str):
        """Start watching a .git directory for git status changes."""
        if not WATCHDOG_AVAILABLE or not self.observer:
            logger.warning("Watchdog not available, cannot start watching git directory: %s", git_path)
            return

        if git_path not in self.watched_paths:
            try:
                # Watch .git directory recursively to catch changes in refs/, logs/, etc.
                watch_handle = self.observer.schedule(self.event_handler, git_path, recursive=True)
                self.watched_paths.add(git_path)
                self.watch_handles[git_path] = watch_handle  # Store handle for cleanup
                logger.info("Started watching git directory (recursive): %s", git_path)

                if not self.observer.is_alive():
                    self.observer.start()
                    logger.info("Started file system observer")
            except Exception as e:
                logger.error("Error starting git directory watcher for %s: %s", git_path, e)
        else:
            logger.debug("Git directory already being watched: %s", git_path)
    
    def stop_watching(self, path: str):
        """Stop watching a specific path."""
        if not WATCHDOG_AVAILABLE or not self.observer:
            return

        if path in self.watched_paths:
            # Actually unschedule the watch using stored handle
            watch_handle = self.watch_handles.get(path)
            if watch_handle:
                try:
                    self.observer.unschedule(watch_handle)
                    logger.info("Successfully unscheduled watch for: %s", path)
                except Exception as e:
                    logger.error("Error unscheduling watch for %s: %s", path, e)
                finally:
                    self.watch_handles.pop(path, None)

            self.watched_paths.discard(path)
            logger.debug("Stopped watching path: %s", path)
    
    def stop_all(self):
        """Stop all file watching."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            self.watched_paths.clear()
            self.watch_handles.clear()