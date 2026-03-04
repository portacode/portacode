"""File system monitoring for project state changes.

This module provides the FileSystemWatcher class which monitors file system
changes using the watchdog library and triggers project state updates when
files or directories are modified.
"""

import asyncio
import faulthandler
import logging
import os
import sys
import time
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
                # Skip events that indicate read-only access or metadata churn
                significant_event_types = {'modified', 'created', 'deleted', 'moved', 'closed_write'}
                if event.event_type not in significant_event_types:
                    logger.debug("🔍 [TRACE] Skipping non-content event: %s", event.event_type)
                    return
                
                # Reading directory contents during refresh generates metadata-only "modified" events.
                if event.is_directory and event.event_type == 'modified':
                    logger.debug("🔍 [TRACE] Skipping directory metadata change: %s", event.src_path)
                    return
                
                # Handle .git folder events separately for git status monitoring
                path_parts = Path(event.src_path).parts
                if '.git' in path_parts:
                    logger.debug("🔍 [TRACE] Skipping .git folder event entirely: %s", event.src_path)
                    return
                
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

        normalized_name = Path(path).name
        if normalized_name == '.git':
            logger.debug("Skipping watch for .git path: %s", path)
            return
        if not os.path.isdir(path):
            logger.debug("Skipping watch for non-directory path: %s", path)
            return
        if not os.access(path, os.R_OK | os.X_OK):
            logger.debug("Skipping watch due to insufficient access: %s", path)
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
    
    def stop_watching(self, path: str):
        """Stop watching a specific path."""
        if not WATCHDOG_AVAILABLE or not self.observer:
            return

        if path in self.watched_paths:
            # Actually unschedule the watch using stored handle
            watch_handle = self.watch_handles.get(path)
            if watch_handle:
                start = time.monotonic()
                logger.info(
                    "About to unschedule watch: path=%s handle=%s observer_alive=%s watched_paths=%d watch_handles=%d",
                    path,
                    id(watch_handle),
                    bool(self.observer and self.observer.is_alive()),
                    len(self.watched_paths),
                    len(self.watch_handles),
                )
                try:
                    # If unschedule blocks silently, force a thread traceback dump for diagnosis.
                    faulthandler.dump_traceback_later(8.0, file=sys.stderr, repeat=False)
                    self.observer.unschedule(watch_handle)
                    logger.info(
                        "Successfully unscheduled watch for: %s (elapsed=%.3fs)",
                        path,
                        max(time.monotonic() - start, 0.0),
                    )
                except Exception as e:
                    logger.exception(
                        "Error unscheduling watch for %s after %.3fs: %s",
                        path,
                        max(time.monotonic() - start, 0.0),
                        e,
                    )
                finally:
                    try:
                        faulthandler.cancel_dump_traceback_later()
                    except Exception:
                        pass
                    self.watch_handles.pop(path, None)
            else:
                logger.warning(
                    "Path %s marked as watched but has no watch handle; watched_paths=%d watch_handles=%d",
                    path,
                    len(self.watched_paths),
                    len(self.watch_handles),
                )

            self.watched_paths.discard(path)
            logger.debug("Stopped watching path: %s", path)
        else:
            logger.debug(
                "stop_watching called for non-watched path: %s (watched_paths=%d watch_handles=%d)",
                path,
                len(self.watched_paths),
                len(self.watch_handles),
            )
    
    def stop_all(self):
        """Stop all file watching."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            self.watched_paths.clear()
            self.watch_handles.clear()

    def get_diagnostics(self) -> dict:
        """Return lightweight stats for health monitoring."""
        return {
            "watched_paths": len(self.watched_paths),
            "git_watched_paths": len([path for path in self.watched_paths if path.endswith(".git")]),
            "observer_alive": bool(self.observer and self.observer.is_alive()),
        }
