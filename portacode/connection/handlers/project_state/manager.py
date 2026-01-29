"""Main project state manager that orchestrates all project state operations.

This module contains the ProjectStateManager class which is the central coordinator
for all project state operations, including file system monitoring, git operations,
tab management, and state synchronization.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from asyncio import Lock
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Set

from .models import ProjectState, MonitoredFolder, FileItem, TabInfo
from .git_manager import GitManager
from .file_system_watcher import FileSystemWatcher
from ....logging_categories import get_categorized_logger, LogCategory

def _deterministic_file_tab_id(file_path: str) -> str:
    try:
        resolved = os.path.abspath(file_path)
        mtime = int(Path(resolved).stat().st_mtime)
    except OSError:
        mtime = "unknown"
        resolved = os.path.abspath(file_path)
    return f"{resolved}:{mtime}"

def _deterministic_diff_tab_id(
    file_path: str,
    from_ref: str,
    to_ref: str,
    from_hash: Optional[str],
    to_hash: Optional[str]
) -> str:
    return f"diff:{os.path.abspath(file_path)}:{from_ref}:{to_ref}:{from_hash or ''}:{to_hash or ''}"

logger = get_categorized_logger(__name__)

# Global singleton instance
_global_project_state_manager: Optional['ProjectStateManager'] = None
_manager_lock = threading.Lock()


class ProjectStateManager:
    """Manages project state for client sessions."""
    
    def __init__(self, control_channel, context: Dict[str, Any]):
        self.control_channel = control_channel
        self.context = context
        self.projects: Dict[str, ProjectState] = {}
        self.git_managers: Dict[str, GitManager] = {}
        self._shared_git_managers: Dict[str, Dict[str, Any]] = {}
        self._shared_git_lock: Lock = Lock()
        self.file_watcher = FileSystemWatcher(self)
        self.debug_mode = False
        self.debug_file_path: Optional[str] = None
        self._session_locks: Dict[str, Lock] = {}
        
        # Content caching optimization
        self.use_content_caching = context.get("use_content_caching", False)
        
        # Debouncing for file changes
        self._change_debounce_timer: Optional[asyncio.Task] = None
        self._pending_changes: Set[str] = set()
        self._pending_change_sources: Dict[str, Dict[str, Any]] = {}
    
    def set_debug_mode(self, enabled: bool, debug_file_path: Optional[str] = None):
        """Enable or disable debug mode with JSON output."""
        self.debug_mode = enabled
        self.debug_file_path = debug_file_path
        if enabled:
            logger.info("Project state debug mode enabled, output to: %s", debug_file_path)
    
    def _get_session_lock(self, client_session_id: str) -> Lock:
        """Get or create an asyncio lock for a client session."""
        lock = self._session_locks.get(client_session_id)
        if not lock:
            lock = Lock()
            self._session_locks[client_session_id] = lock
        return lock
    
    def _write_debug_state(self):
        """Write current state to debug JSON file (thread-safe)."""
        if not self.debug_mode or not self.debug_file_path:
            return
        
        # Use a lock to prevent multiple instances from writing simultaneously
        with _manager_lock:
            try:
                debug_data = {
                    "_instance_info": {
                        "pid": os.getpid(),
                        "timestamp": time.time(),
                        "project_count": len(self.projects)
                    }
                }
                
                for project_id, state in self.projects.items():
                    debug_data[project_id] = {
                        "project_folder_path": state.project_folder_path,
                        "is_git_repo": state.is_git_repo,
                        "git_branch": state.git_branch,
                        "git_status_summary": state.git_status_summary,
                        "git_detailed_status": asdict(state.git_detailed_status) if state.git_detailed_status and hasattr(state.git_detailed_status, '__dataclass_fields__') else None,
                        "open_tabs": [self._serialize_tab_info(tab) for tab in state.open_tabs.values()],
                        "active_tab": self._serialize_tab_info(state.active_tab) if state.active_tab else None,
                        "monitored_folders": [asdict(mf) if hasattr(mf, '__dataclass_fields__') else {} for mf in state.monitored_folders],
                        "items": [self._serialize_file_item(item) for item in state.items]
                    }
                
                # Write atomically by writing to temp file first, then renaming
                temp_file_path = self.debug_file_path + ".tmp"
                with open(temp_file_path, 'w', encoding='utf-8') as f:
                    json.dump(debug_data, f, indent=2, default=str)
                
                # Atomic rename
                os.rename(temp_file_path, self.debug_file_path)
                
                # Only log debug info occasionally to avoid spam
                if len(debug_data) > 1:  # >1 because we always have _instance_info
                    logger.debug("Debug state updated: %d projects (PID: %s)", len(debug_data) - 1, os.getpid())
                    
            except Exception as e:
                logger.error("Error writing debug state: %s", e)
    
    def _serialize_file_item(self, item: FileItem) -> Dict[str, Any]:
        """Serialize FileItem for JSON output."""
        result = asdict(item) if hasattr(item, '__dataclass_fields__') else {}
        if item.children:
            result["children"] = [self._serialize_file_item(child) for child in item.children]
        return result
    
    def _serialize_tab_info(self, tab: TabInfo) -> Dict[str, Any]:
        """Serialize TabInfo for JSON output."""
        if not hasattr(tab, '__dataclass_fields__'):
            return {}
        
        tab_dict = asdict(tab)
        
        # If content caching is enabled, exclude content fields to reduce payload size
        if self.use_content_caching:
            # Only include hashes, not the actual content
            tab_dict.pop('content', None)
            tab_dict.pop('original_content', None) 
            tab_dict.pop('modified_content', None)
            # Keep the hashes for client-side cache lookup
            # content_hash, original_content_hash, modified_content_hash remain
            
            # Also exclude large metadata for diff tabs
            if tab_dict.get('metadata'):
                metadata = tab_dict['metadata']
                # Remove massive HTML diff content that can be megabytes
                metadata.pop('html_diff_versions', None)
                metadata.pop('diff_details', None)
            
        return tab_dict

    async def _handle_shared_git_change(self, project_folder_path: str):
        """Notify all sessions for a project when the shared git manager detects changes."""
        async with self._shared_git_lock:
            entry = self._shared_git_managers.get(project_folder_path)
            target_sessions = list(entry["sessions"]) if entry else []

        for session_id in target_sessions:
            await self._refresh_project_state(session_id, git_only=True, reason="git_monitor")

    async def _acquire_shared_git_manager(self, project_folder_path: str, client_session_id: str) -> GitManager:
        """Get or create a shared git manager for a project path."""
        async with self._shared_git_lock:
            entry = self._shared_git_managers.get(project_folder_path)

            if not entry:
                async def git_change_callback():
                    await self._handle_shared_git_change(project_folder_path)

                git_manager = GitManager(
                    project_folder_path,
                    change_callback=git_change_callback,
                    owner_session_id=client_session_id,
                )
                entry = {"manager": git_manager, "sessions": set()}
                self._shared_git_managers[project_folder_path] = entry

            entry["sessions"].add(client_session_id)
            self.git_managers[client_session_id] = entry["manager"]
            return entry["manager"]

    async def _release_shared_git_manager(self, project_folder_path: Optional[str], client_session_id: str):
        """Release a session's reference to a shared git manager and clean it up if unused."""
        manager_to_cleanup: Optional[GitManager] = None

        async with self._shared_git_lock:
            manager = self.git_managers.pop(client_session_id, None)

            if not project_folder_path:
                manager_to_cleanup = manager
            else:
                entry = self._shared_git_managers.get(project_folder_path)
                if entry:
                    entry["sessions"].discard(client_session_id)
                    if not entry["sessions"]:
                        self._shared_git_managers.pop(project_folder_path, None)
                        manager_to_cleanup = manager or entry["manager"]
                else:
                    manager_to_cleanup = manager

        if manager_to_cleanup:
            manager_to_cleanup.cleanup()
    
    async def initialize_project_state(self, client_session_id: str, project_folder_path: str) -> ProjectState:
        """Initialize project state for a client session."""
        lock = self._get_session_lock(client_session_id)
        async with lock:
            existing_project = self.projects.get(client_session_id)
            if existing_project:
                if existing_project.project_folder_path == project_folder_path:
                    logger.info("Returning existing project state for client session: %s", client_session_id)
                    return existing_project
                logger.info("Client session %s switching projects from %s to %s",
                            client_session_id, existing_project.project_folder_path, project_folder_path)
                previous_path = self._cleanup_project_locked(client_session_id)
                await self._release_shared_git_manager(previous_path, client_session_id)
            
            logger.info("Initializing project state for client session: %s, folder: %s", client_session_id, project_folder_path)
            git_manager = await self._acquire_shared_git_manager(project_folder_path, client_session_id)
            
            loop = asyncio.get_event_loop()
            is_git_repo = git_manager.is_git_repo
            git_branch = await loop.run_in_executor(None, git_manager.get_branch_name)
            git_status_summary = await loop.run_in_executor(None, git_manager.get_status_summary)
            git_detailed_status = await loop.run_in_executor(None, git_manager.get_detailed_status)
            
            project_state = ProjectState(
                client_session_id=client_session_id,
                project_folder_path=project_folder_path,
                items=[],
                is_git_repo=is_git_repo,
                git_branch=git_branch,
                git_status_summary=git_status_summary,
                git_detailed_status=git_detailed_status,
            )
            
            await self._initialize_monitored_folders(project_state)
            await self._sync_all_state_with_monitored_folders(project_state)
            
            self.projects[client_session_id] = project_state
            self._write_debug_state()
            
            return project_state

    def get_diagnostics(self) -> Dict[str, Any]:
        """Return aggregate stats for health monitoring."""
        git_diag = {}
        for session_id, git_manager in self.git_managers.items():
            git_diag[session_id] = git_manager.get_diagnostics()

        watcher_diag = self.file_watcher.get_diagnostics() if self.file_watcher else {}

        return {
            "projects": len(self.projects),
            "git_managers": len(self.git_managers),
            "pending_file_changes": len(self._pending_changes),
            "watcher": watcher_diag,
            "git_sessions": git_diag,
        }
    
    async def _initialize_monitored_folders(self, project_state: ProjectState):
        """Initialize monitored folders with project root (expanded) and its immediate subdirectories (collapsed)."""
        # Add project root as expanded
        project_state.monitored_folders.append(
            MonitoredFolder(folder_path=project_state.project_folder_path, is_expanded=True)
        )
        
        # Scan project root for immediate subdirectories and add them as collapsed
        try:
            with os.scandir(project_state.project_folder_path) as entries:
                for entry in entries:
                    if entry.is_dir() and entry.name != '.git':  # Only exclude .git, allow other dot folders
                        project_state.monitored_folders.append(
                            MonitoredFolder(folder_path=entry.path, is_expanded=False)
                        )
        except (OSError, PermissionError) as e:
            logger.error("Error scanning project root for subdirectories: %s", e)
    
    async def _start_watching_monitored_folders(self, project_state: ProjectState):
        """Start watching all monitored folders."""
        for monitored_folder in project_state.monitored_folders:
            self.file_watcher.start_watching(monitored_folder.folder_path)
    
    async def _sync_watchdog_with_monitored_folders(self, project_state: ProjectState):
        """Ensure watchdog is monitoring each monitored folder individually (non-recursive)."""
        # Watch each monitored folder individually to align with the monitored_folders structure
        for monitored_folder in project_state.monitored_folders:
            self.file_watcher.start_watching(monitored_folder.folder_path)
        
        # Intentionally avoid watching .git; Git status changes are polled separately
        if project_state.is_git_repo:
            git_dir_path = os.path.join(project_state.project_folder_path, '.git')
            logger.debug("🔍 [TRACE] Project is git repo, but skipping .git watcher registration: %s", LogCategory.GIT, git_dir_path)
        else:
            logger.debug("🔍 [TRACE] Project is NOT a git repo, no .git watcher needed", LogCategory.GIT)
        
        # Watchdog synchronized
    
    async def _sync_all_state_with_monitored_folders(self, project_state: ProjectState):
        """Synchronize all dependent state (watchdog, items) with monitored_folders changes."""
        # Syncing state with monitored folders
        
        # Sync watchdog monitoring
        logger.debug("Syncing watchdog monitoring")
        await self._sync_watchdog_with_monitored_folders(project_state)
        
        # Rebuild items structure from all monitored folders
        logger.debug("Rebuilding items structure")
        await self._build_flattened_items_structure(project_state)
        # Items rebuilt
        
        # Update debug state less frequently
        self._write_debug_state()
        logger.debug("_sync_all_state_with_monitored_folders completed")
    
    async def _add_subdirectories_to_monitored(self, project_state: ProjectState, parent_folder_path: str):
        """Add all subdirectories of a folder to monitored_folders if not already present, and remove deleted ones."""
        # logger.info("_add_subdirectories_to_monitored called for: %s", parent_folder_path)
        try:
            existing_paths = {mf.folder_path for mf in project_state.monitored_folders}
            # logger.info("Existing monitored paths: %s", existing_paths)
            added_any = False
            removed_any = False
            
            # First, clean up any monitored folders that no longer exist
            to_remove = []
            for monitored_folder in project_state.monitored_folders:
                # Don't remove the root project folder, only subdirectories
                if monitored_folder.folder_path != project_state.project_folder_path:
                    if not os.path.exists(monitored_folder.folder_path):
                        logger.info("Removing deleted monitored folder: %s", monitored_folder.folder_path)
                        to_remove.append(monitored_folder)
                        removed_any = True
            
            for folder_to_remove in to_remove:
                project_state.monitored_folders.remove(folder_to_remove)
            
            # Then, add new subdirectories
            with os.scandir(parent_folder_path) as entries:
                for entry in entries:
                    if entry.is_dir() and entry.name != '.git':  # Only exclude .git, allow other dot folders
                        # logger.info("Found subdirectory: %s", entry.path)
                        if entry.path not in existing_paths:
                            logger.info("Adding new monitored folder: %s", entry.path)
                            new_monitored = MonitoredFolder(folder_path=entry.path, is_expanded=False)
                            project_state.monitored_folders.append(new_monitored)
                            added_any = True
                        else:
                            # logger.info("Subdirectory already monitored: %s", entry.path)
                            pass
            
            # logger.info("Added any new folders: %s, Removed any deleted folders: %s", added_any, removed_any)
            # Note: sync will be handled by the caller, no need to sync here
                
        except (OSError, PermissionError) as e:
            logger.error("Error scanning folder %s for subdirectories: %s", parent_folder_path, e)
    
    def _find_monitored_folder(self, project_state: ProjectState, folder_path: str) -> Optional[MonitoredFolder]:
        """Find a monitored folder by path."""
        for monitored_folder in project_state.monitored_folders:
            if monitored_folder.folder_path == folder_path:
                return monitored_folder
        return None
    
    async def _load_directory_items(self, project_state: ProjectState, directory_path: str, is_root: bool = False, parent_item: Optional[FileItem] = None):
        """Load directory items with Git metadata."""
        git_manager = self.git_managers.get(project_state.client_session_id)
        
        try:
            items = []
            
            # Use os.scandir for better performance
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    try:
                        # Skip .git metadata regardless of whether it's a dir or file (worktrees create files)
                        if entry.name == '.git':
                            continue
                            
                        stat_info = entry.stat()
                        is_hidden = entry.name.startswith('.')
                        
                        # Get Git status if available
                        git_info = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
                        if git_manager:
                            git_info = git_manager.get_file_status(entry.path)
                        
                        # Check if this directory is expanded and loaded
                        is_expanded = False
                        is_loaded = True  # Files are always loaded; for directories, will be set based on monitored_folders
                        
                        file_item = FileItem(
                            name=entry.name,
                            path=entry.path,
                            is_directory=entry.is_dir(),
                            parent_path=directory_path,
                            size=stat_info.st_size if entry.is_file() else None,
                            modified_time=stat_info.st_mtime,
                            is_git_tracked=git_info["is_tracked"],
                            git_status=git_info["status"],
                            is_staged=git_info["is_staged"],
                            is_hidden=is_hidden,
                            is_ignored=git_info["is_ignored"],
                            is_expanded=is_expanded,
                            is_loaded=is_loaded
                        )
                        
                        items.append(file_item)
                        
                    except (OSError, PermissionError) as e:
                        logger.debug("Error reading entry %s: %s", entry.path, e)
                        continue
            
            # Sort items: directories first, then files, both alphabetically
            items.sort(key=lambda x: (not x.is_directory, x.name.lower()))
            
            if is_root:
                project_state.items = items
            elif parent_item:
                parent_item.children = items
                # Don't set is_loaded here - it's set in _build_flattened_items_structure based on monitored_folders
                
        except (OSError, PermissionError) as e:
            logger.error("Error loading directory %s: %s", directory_path, e)
    
    async def _build_flattened_items_structure(self, project_state: ProjectState):
        """Build a flattened items structure including ALL items from ALL monitored folders."""
        import time
        func_start = time.time()

        all_items = []

        # Create sets for quick lookup
        expanded_paths = {mf.folder_path for mf in project_state.monitored_folders if mf.is_expanded}
        monitored_paths = {mf.folder_path for mf in project_state.monitored_folders}

        # OPTIMIZATION: Collect all file paths first, then batch git operations
        batch_git_start = time.time()
        all_file_paths = []
        folder_to_paths = {}  # monitored_folder_path -> list of child paths

        # First pass: scan all directories to collect file paths
        for monitored_folder in project_state.monitored_folders:
            try:
                child_paths = []
                with os.scandir(monitored_folder.folder_path) as entries:
                    for entry in entries:
                        if entry.name == '.git':
                            continue
                        child_paths.append(entry.path)
                        all_file_paths.append(entry.path)
                folder_to_paths[monitored_folder.folder_path] = child_paths
            except (OSError, PermissionError) as e:
                logger.error("Error scanning folder %s: %s", monitored_folder.folder_path, e)
                folder_to_paths[monitored_folder.folder_path] = []

        # BATCH GIT OPERATION: Get status for ALL files at once
        git_manager = self.git_managers.get(project_state.client_session_id)
        git_status_map = {}
        if git_manager and all_file_paths:
            loop = asyncio.get_event_loop()
            git_status_map = await loop.run_in_executor(
                None,
                git_manager.get_file_status_batch,
                all_file_paths
            )

        batch_git_duration = time.time() - batch_git_start
        logger.info("⏱️ Batch git operations for %d files took %.4f seconds", len(all_file_paths), batch_git_duration)

        # Second pass: load items using pre-fetched git status
        load_items_start = time.time()
        loop = asyncio.get_event_loop()
        for monitored_folder in project_state.monitored_folders:
            # Load direct children of this monitored folder (run in executor to avoid blocking)
            children = await loop.run_in_executor(
                None,
                self._load_directory_items_list_sync,
                monitored_folder.folder_path,
                monitored_folder.folder_path,
                git_status_map  # Pass pre-fetched git status
            )
            
            # Set correct expansion and loading states for each child
            for child in children:
                if child.is_directory:
                    # Set is_expanded based on expanded_paths
                    child.is_expanded = child.path in expanded_paths
                    # Set is_loaded based on monitored_paths (content loaded = in monitored folders)
                    child.is_loaded = child.path in monitored_paths
                else:
                    # Files are always loaded
                    child.is_loaded = True
                all_items.append(child)

        load_items_duration = time.time() - load_items_start
        logger.info("⏱️ Loading items took %.4f seconds", load_items_duration)

        # Remove duplicates (items might be loaded multiple times due to nested monitoring)
        dedup_start = time.time()
        items_dict = {}
        for item in all_items:
            items_dict[item.path] = item

        dedup_duration = time.time() - dedup_start
        logger.info("⏱️ Deduplication took %.4f seconds", dedup_duration)

        # Convert back to list and sort for consistent ordering
        sort_start = time.time()
        project_state.items = list(items_dict.values())
        project_state.items.sort(key=lambda x: (x.parent_path, not x.is_directory, x.name.lower()))
        sort_duration = time.time() - sort_start
        logger.info("⏱️ Sorting took %.4f seconds", sort_duration)

        func_duration = time.time() - func_start
        logger.info("⏱️ _build_flattened_items_structure TOTAL: %.4f seconds (batch_git=%.4f, load=%.4f)",
                   func_duration, batch_git_duration, load_items_duration)
    
    def _load_directory_items_list_sync(self, directory_path: str, parent_path: str,
                                       git_status_map: Dict[str, Dict[str, Any]] = None) -> List[FileItem]:
        """Load directory items and return as a list with parent_path (synchronous version for executor).

        Args:
            directory_path: Directory to scan
            parent_path: Parent path for items
            git_status_map: Optional pre-fetched git status map (path -> status_dict)
        """
        items = []

        try:
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    try:
                        # Skip .git metadata regardless of whether it's a dir or file (worktrees create files)
                        if entry.name == '.git':
                            continue

                        stat_info = entry.stat()
                        is_hidden = entry.name.startswith('.')

                        # Get Git status from pre-fetched map or use default
                        git_info = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
                        if git_status_map and entry.path in git_status_map:
                            git_info = git_status_map[entry.path]

                        file_item = FileItem(
                            name=entry.name,
                            path=entry.path,
                            is_directory=entry.is_dir(),
                            parent_path=parent_path,
                            size=stat_info.st_size if entry.is_file() else None,
                            modified_time=stat_info.st_mtime,
                            is_git_tracked=git_info["is_tracked"],
                            git_status=git_info["status"],
                            is_staged=git_info["is_staged"],
                            is_hidden=is_hidden,
                            is_ignored=git_info["is_ignored"],
                            is_expanded=False,
                            is_loaded=True  # Will be set correctly in _build_flattened_items_structure
                        )

                        items.append(file_item)

                    except (OSError, PermissionError) as e:
                        logger.debug("Error reading entry %s: %s", entry.path, e)
                        continue

            # Sort items: directories first, then files, both alphabetically
            items.sort(key=lambda x: (not x.is_directory, x.name.lower()))

        except (OSError, PermissionError) as e:
            logger.error("Error loading directory %s: %s", directory_path, e)

        return items
    
    async def expand_folder(self, client_session_id: str, folder_path: str) -> bool:
        """Expand a folder and load its contents."""
        logger.info("expand_folder called: client_session_id=%s, folder_path=%s", client_session_id, folder_path)
        
        if client_session_id not in self.projects:
            logger.error("Project state not found for client session: %s", client_session_id)
            return False
        
        project_state = self.projects[client_session_id]
        logger.info("Found project state. Current monitored_folders count: %d", len(project_state.monitored_folders))
        
        # Debug: log all monitored folders
        for i, mf in enumerate(project_state.monitored_folders):
            logger.info("Monitored folder %d: path=%s, is_expanded=%s", i, mf.folder_path, mf.is_expanded)
        
        # Update the monitored folder to expanded state
        monitored_folder = self._find_monitored_folder(project_state, folder_path)
        if not monitored_folder:
            logger.error("Monitored folder not found for path: %s", folder_path)
            return False
        
        logger.info("Found monitored folder: %s, current is_expanded: %s", monitored_folder.folder_path, monitored_folder.is_expanded)
        monitored_folder.is_expanded = True
        logger.info("Set monitored folder to expanded: %s", monitored_folder.is_expanded)
        
        # Add all subdirectories of the expanded folder to monitored folders
        logger.info("Adding subdirectories to monitored for: %s", folder_path)
        await self._add_subdirectories_to_monitored(project_state, folder_path)
        
        # Sync all dependent state (this will update items and watchdog)
        logger.info("Syncing all state with monitored folders")
        await self._sync_all_state_with_monitored_folders(project_state)
        
        logger.info("expand_folder completed successfully")
        return True
    
    async def collapse_folder(self, client_session_id: str, folder_path: str) -> bool:
        """Collapse a folder."""
        if client_session_id not in self.projects:
            return False
        
        project_state = self.projects[client_session_id]
        
        # Update the monitored folder to collapsed state
        monitored_folder = self._find_monitored_folder(project_state, folder_path)
        if not monitored_folder:
            return False
        
        monitored_folder.is_expanded = False
        
        # Note: We keep monitoring collapsed folders for file changes
        # but don't stop watching them as we want to detect new files/folders
        
        # Sync all dependent state (this will update items with correct expansion state)
        await self._sync_all_state_with_monitored_folders(project_state)
        
        return True
    
    def _find_item_by_path(self, items: List[FileItem], target_path: str) -> Optional[FileItem]:
        """Find a file item by its path recursively."""
        for item in items:
            if item.path == target_path:
                return item
            if item.children:
                found = self._find_item_by_path(item.children, target_path)
                if found:
                    return found
        return None
    
    async def open_file(self, client_session_id: str, file_path: str, set_active: bool = True) -> bool:
        """Open a file in a new tab with content loaded."""
        if client_session_id not in self.projects:
            return False
        
        project_state = self.projects[client_session_id]
        
        # Generate unique key for file tab
        from .utils import generate_tab_key
        tab_key = generate_tab_key('file', file_path)
        
        # Check if file is already open
        if tab_key in project_state.open_tabs:
            existing_tab = project_state.open_tabs[tab_key]
            if set_active:
                project_state.active_tab = existing_tab
            self._write_debug_state()
            return True
        
        # Create new file tab using tab factory
        from ..tab_factory import get_tab_factory
        tab_factory = get_tab_factory()

        try:
            logger.info(f"About to create tab for file: {file_path}")
            new_tab = await tab_factory.create_file_tab(file_path, tab_id=_deterministic_file_tab_id(file_path))
            logger.info(f"Tab created successfully, adding to project state")
            project_state.open_tabs[tab_key] = new_tab
            if set_active:
                project_state.active_tab = new_tab
            
            logger.info(f"Opened file tab: {file_path} (content loaded: {len(new_tab.content or '') > 0})")
            try:
                self._write_debug_state()
            except Exception as debug_e:
                logger.warning(f"Debug state write failed (non-critical): {debug_e}")
            return True
        except Exception as e:
            logger.error(f"Failed to create tab for file {file_path}: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False
    
    async def close_tab(self, client_session_id: str, tab_id: str) -> bool:
        """Close a tab by tab ID."""
        if client_session_id not in self.projects:
            return False
        
        project_state = self.projects[client_session_id]
        
        # Find and remove the tab by searching through the dictionary values
        tab_key_to_remove = None
        tab_to_remove = None
        for key, tab in project_state.open_tabs.items():
            if tab.tab_id == tab_id:
                tab_key_to_remove = key
                tab_to_remove = tab
                break
        
        if not tab_to_remove:
            return False
        
        del project_state.open_tabs[tab_key_to_remove]
        
        # Clear active tab if it was the closed tab
        if project_state.active_tab and project_state.active_tab.tab_id == tab_id:
            # Set active tab to the last remaining tab, or None if no tabs left
            remaining_tabs = list(project_state.open_tabs.values())
            project_state.active_tab = remaining_tabs[-1] if remaining_tabs else None
        
        return True
    
    async def set_active_tab(self, client_session_id: str, tab_id: Optional[str]) -> bool:
        """Set the currently active tab."""
        if client_session_id not in self.projects:
            return False
        
        project_state = self.projects[client_session_id]
        
        if tab_id:
            # Find the tab by ID in the dictionary values
            tab = None
            for t in project_state.open_tabs.values():
                if t.tab_id == tab_id:
                    tab = t
                    break
            if not tab:
                return False
            project_state.active_tab = tab
        else:
            project_state.active_tab = None
        
        return True
    
    async def open_diff_tab(self, client_session_id: str, file_path: str, 
                           from_ref: str, to_ref: str, from_hash: Optional[str] = None, 
                           to_hash: Optional[str] = None) -> bool:
        """Open a diff tab comparing file versions at different git timeline points."""
        if client_session_id not in self.projects:
            return False
        
        project_state = self.projects[client_session_id]
        git_manager = self.git_managers.get(client_session_id)
        
        if not git_manager or not git_manager.is_git_repo:
            logger.error("Cannot create diff tab: not a git repository")
            return False
        
        # Generate unique key for diff tab
        from .utils import generate_tab_key
        tab_key = generate_tab_key('diff', file_path, 
                                 from_ref=from_ref, to_ref=to_ref, 
                                 from_hash=from_hash, to_hash=to_hash)
        
        # Check if this diff tab is already open
        if tab_key in project_state.open_tabs:
            existing_tab = project_state.open_tabs[tab_key]
            project_state.active_tab = existing_tab
            logger.info(f"Diff tab already exists, activating: {tab_key}")
            self._write_debug_state()
            return True
        
        try:
            # Get content based on the reference type
            original_content = ""
            modified_content = ""
            
            # Handle 'from' reference
            if from_ref == "head":
                original_content = git_manager.get_file_content_at_commit(file_path) or ""
            elif from_ref == "staged":
                original_content = git_manager.get_file_content_staged(file_path) or ""
            elif from_ref == "working":
                # Read current file content
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            original_content = f.read()
                    except (OSError, UnicodeDecodeError) as e:
                        logger.error("Error reading working file %s: %s", file_path, e)
                        original_content = f"# Error reading file: {e}"
            elif from_ref == "commit" and from_hash:
                original_content = git_manager.get_file_content_at_commit(file_path, from_hash) or ""
            
            # Handle 'to' reference
            if to_ref == "head":
                modified_content = git_manager.get_file_content_at_commit(file_path) or ""
            elif to_ref == "staged":
                modified_content = git_manager.get_file_content_staged(file_path) or ""
            elif to_ref == "working":
                # Read current file content
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            modified_content = f.read()
                    except (OSError, UnicodeDecodeError) as e:
                        logger.error("Error reading working file %s: %s", file_path, e)
                        modified_content = f"# Error reading file: {e}"
            elif to_ref == "commit" and to_hash:
                modified_content = git_manager.get_file_content_at_commit(file_path, to_hash) or ""
            
            # Create diff tab using tab factory
            from ..tab_factory import get_tab_factory
            tab_factory = get_tab_factory()
            
            # Compute diff details for the client
            diff_details = git_manager._compute_diff_details(original_content, modified_content)
            
            # Generate HTML diff with syntax highlighting (both minimal and full context)
            # Re-enable with improved performance and on-demand generation
            html_diff_versions = None
            try:
                import time
                diff_start_time = time.time()
                
                # Skip HTML diff for very large files to prevent connection issues
                original_size = len(original_content)
                modified_size = len(modified_content)
                if original_size > 1000000 or modified_size > 1000000:  # 1MB limit
                    logger.warning(f"Skipping HTML diff generation for large file {file_path} ({original_size}+{modified_size} bytes)")
                    html_diff_versions = None
                else:
                    logger.info(f"Starting HTML diff generation for {file_path} ({original_size}+{modified_size} bytes)")
                    html_diff_versions = git_manager._generate_html_diff(original_content, modified_content, file_path)
                    diff_end_time = time.time()
                    logger.info(f"HTML diff generation completed for {file_path} in {diff_end_time - diff_start_time:.2f}s")
            except Exception as e:
                logger.error(f"Error generating HTML diff for {file_path}: {e}")
                import traceback
                logger.error(f"Diff generation traceback: {traceback.format_exc()}")
                # Continue without HTML diff - fallback to basic diff will be used
            
            # Create a descriptive title for the diff
            title_parts = []
            if from_ref == "commit" and from_hash:
                title_parts.append(from_hash[:8])
            else:
                title_parts.append(from_ref)
            title_parts.append("→")
            if to_ref == "commit" and to_hash:
                title_parts.append(to_hash[:8])
            else:
                title_parts.append(to_ref)
            
            diff_title = f"{os.path.basename(file_path)} ({' '.join(title_parts)})"
            
            diff_tab = await tab_factory.create_diff_tab_with_title(
                file_path,
                original_content,
                modified_content,
                diff_title,
                tab_id=_deterministic_diff_tab_id(file_path, from_ref, to_ref, from_hash, to_hash),
                diff_details=diff_details
            )
            
            # Add metadata about the diff references
            metadata_update = {
                'from_ref': from_ref,
                'to_ref': to_ref,
                'from_hash': from_hash,
                'to_hash': to_hash,
                'diff_timeline': True
            }
            
            # Only add HTML diff versions if they were successfully generated
            if html_diff_versions:
                metadata_update['html_diff_versions'] = html_diff_versions
            
            diff_tab.metadata.update(metadata_update)
            
            project_state.open_tabs[tab_key] = diff_tab
            project_state.active_tab = diff_tab
            
            logger.info(f"Created timeline diff tab for: {file_path} ({from_ref} → {to_ref})")
            self._write_debug_state()
            return True
            
        except Exception as e:
            logger.error(f"Failed to create timeline diff tab for {file_path}: {e}")
            return False
    
    async def _handle_file_change(self, event):
        """Handle file system change events with debouncing."""
        logger.debug("🔍 [TRACE] _handle_file_change called: %s - %s", LogCategory.FILE_SYSTEM, event.event_type, event.src_path)
        is_git_event = ".git" in Path(event.src_path).parts
        if is_git_event:
            logger.info("File watcher event from .git: %s %s", LogCategory.FILE_SYSTEM, event.event_type, event.src_path)
        else:
            logger.debug("File watcher event: %s %s", LogCategory.FILE_SYSTEM, event.event_type, event.src_path)
        
        self._pending_changes.add(event.src_path)
        self._pending_change_sources[event.src_path] = {
            "event_type": event.event_type,
            "is_git_event": is_git_event,
            "timestamp": time.time(),
        }
        logger.debug("🔍 [TRACE] Added to pending changes: %s (total pending: %d)", LogCategory.FILE_SYSTEM, event.src_path, len(self._pending_changes))
        
        # Cancel existing timer
        if self._change_debounce_timer and not self._change_debounce_timer.done():
            logger.debug("🔍 [TRACE] Cancelling existing debounce timer")
            self._change_debounce_timer.cancel()
        
        # Set new timer with proper exception handling
        async def debounced_process():
            try:
                logger.debug("🔍 [TRACE] Starting debounce delay (0.5s)...")
                await asyncio.sleep(0.5)  # Debounce delay
                logger.debug("🔍 [TRACE] Debounce delay complete, processing pending changes...")
                await self._process_pending_changes()
            except asyncio.CancelledError:
                logger.debug("🔍 [TRACE] Debounce timer cancelled")
            except Exception as e:
                logger.error("🔍 [TRACE] ❌ Error in debounced file processing: %s", e)
        
        logger.debug("🔍 [TRACE] Creating new debounce timer task...")
        self._change_debounce_timer = asyncio.create_task(debounced_process())
    
    async def _process_pending_changes(self):
        """Process pending file changes."""
        logger.debug("🔍 [TRACE] _process_pending_changes called")
        
        if not self._pending_changes:
            logger.debug("🔍 [TRACE] No pending changes to process")
            return
        
        logger.debug("🔍 [TRACE] Processing %d pending file changes: %s", len(self._pending_changes), list(self._pending_changes))
        git_events = [path for path in self._pending_changes if self._pending_change_sources.get(path, {}).get("is_git_event")]
        workspace_events = [path for path in self._pending_changes if path not in git_events]
        logger.info(
            "Pending change summary: total=%d git_events=%d workspace_events=%d sample_git=%s",
            LogCategory.FILE_SYSTEM,
            len(self._pending_changes),
            len(git_events),
            len(workspace_events),
            git_events[:3],
        )
        
        # Process changes for each affected project
        affected_projects = set()
        logger.debug("🔍 [TRACE] Checking %d active projects for affected paths", len(self.projects))
        
        for change_path in self._pending_changes:
            logger.debug("🔍 [TRACE] Checking change path: %s", change_path)
            for client_session_id, project_state in self.projects.items():
                logger.debug("🔍 [TRACE] Comparing with project path: %s (session: %s)", 
                           project_state.project_folder_path, client_session_id)
                if change_path.startswith(project_state.project_folder_path):
                    logger.debug("🔍 [TRACE] ✅ Change affects project session: %s", client_session_id)
                    affected_projects.add(client_session_id)
                else:
                    logger.debug("🔍 [TRACE] ❌ Change does NOT affect project session: %s", client_session_id)
        
        if affected_projects:
            logger.debug("🔍 [TRACE] Found %d affected projects: %s", len(affected_projects), list(affected_projects))
        else:
            logger.debug("🔍 [TRACE] ❌ No affected projects to refresh")
        
        # Refresh affected projects
        for client_session_id in affected_projects:
            project_state = self.projects.get(client_session_id)
            if not project_state:
                continue
            project_paths = [
                path for path in self._pending_changes
                if path.startswith(project_state.project_folder_path)
            ]
            git_paths = [
                path for path in project_paths
                if self._pending_change_sources.get(path, {}).get("is_git_event")
            ]
            is_git_only_batch = bool(project_paths) and len(project_paths) == len(git_paths)
            logger.info(
                "Refreshing project %s due to pending changes: total_paths=%d git_paths=%d git_only_batch=%s sample_paths=%s",
                LogCategory.FILE_SYSTEM,
                client_session_id,
                len(project_paths),
                len(git_paths),
                is_git_only_batch,
                project_paths[:3],
            )
            await self._refresh_project_state(
                client_session_id,
                git_only=is_git_only_batch,
                reason="filesystem_watch_git_only" if is_git_only_batch else "filesystem_watch",
            )
        
        self._pending_changes.clear()
        self._pending_change_sources.clear()
        logger.debug("🔍 [TRACE] ✅ Finished processing file changes")
    
    async def _refresh_project_state(self, client_session_id: str, git_only: bool = False, reason: str = "unknown"):
        """Refresh project state after file changes.

        Args:
            client_session_id: The client session ID
            git_only: If True, only git status changed (skip filesystem operations like
                     detecting new directories and syncing file state). Use this for
                     git operations (stage, unstage, revert) to avoid unnecessary work.
        """
        logger.debug("🔍 [TRACE] _refresh_project_state called for session: %s (git_only=%s, reason=%s)",
                   client_session_id, git_only, reason)
        
        if client_session_id not in self.projects:
            logger.debug("🔍 [TRACE] ❌ Session not found in projects: %s", client_session_id)
            return
        
        project_state = self.projects[client_session_id]
        git_manager = self.git_managers[client_session_id]
        logger.debug("🔍 [TRACE] Found project state and git manager for session: %s", client_session_id)
        
        # Check if git repo status changed (created or deleted)
        git_dir_path = os.path.join(project_state.project_folder_path, '.git')
        git_dir_exists = os.path.exists(git_dir_path)
        
        if not git_manager.is_git_repo and git_dir_exists:
            # Git repo was created
            logger.debug("🔍 [TRACE] Git repo detected, reinitializing git manager for session: %s", client_session_id)
            git_manager.reinitialize()
        elif git_manager.is_git_repo and not git_dir_exists:
            # Git repo was deleted
            logger.debug("🔍 [TRACE] Git repo removed, updating git manager for session: %s", client_session_id)
            git_manager.repo = None
            git_manager.is_git_repo = False
        
        # Update Git status
        if git_manager:
            logger.debug("🔍 [TRACE] Updating git status for session: %s", client_session_id)
            old_branch = project_state.git_branch
            old_status_summary = project_state.git_status_summary
            old_is_git_repo = project_state.is_git_repo
            
            # Update all git state atomically - single source of truth
            project_state.is_git_repo = git_manager.is_git_repo
            project_state.git_branch = git_manager.get_branch_name()
            project_state.git_status_summary = git_manager.get_status_summary()
            project_state.git_detailed_status = git_manager.get_detailed_status()
            
            logger.debug("🔍 [TRACE] Git status updated - is_git_repo: %s->%s, branch: %s->%s, summary: %s->%s", 
                       old_is_git_repo, project_state.is_git_repo,
                       old_branch, project_state.git_branch, 
                       old_status_summary, project_state.git_status_summary)
        else:
            logger.debug("🔍 [TRACE] ❌ No git manager found for session: %s", client_session_id)

        # For git-only operations, skip scanning for new directories
        # but still sync items to update git attributes for UI
        if not git_only:
            # Detect and add new directories in expanded folders before syncing
            logger.debug("🔍 [TRACE] Detecting and adding new directories...")
            await self._detect_and_add_new_directories(project_state)
        else:
            logger.debug("🔍 [TRACE] Skipping directory detection (git_only=True)")

        # Always sync state to update git attributes on items (needed for UI updates)
        logger.debug("🔍 [TRACE] Syncing all state with monitored folders...")
        await self._sync_all_state_with_monitored_folders(project_state)

        # Send update to clients
        logger.debug("🔍 [TRACE] About to send project state update...")
        await self._send_project_state_update(project_state)
    
    async def _detect_and_add_new_directories(self, project_state: ProjectState):
        """Detect new directories in EXPANDED monitored folders and add them to monitoring."""
        # For each currently expanded monitored folder, check if new subdirectories appeared
        expanded_folder_paths = [mf.folder_path for mf in project_state.monitored_folders if mf.is_expanded]
        logger.debug("🔍 [TRACE] Checking %d expanded folders for new subdirectories: %s", 
                   len(expanded_folder_paths), expanded_folder_paths)
        
        for folder_path in expanded_folder_paths:
            if os.path.exists(folder_path) and os.path.isdir(folder_path):
                logger.debug("🔍 [TRACE] Checking expanded folder for new subdirectories: %s", folder_path)
                await self._add_subdirectories_to_monitored(project_state, folder_path)
    
    async def _reload_visible_structures(self, project_state: ProjectState):
        """Reload all visible structures with flattened items."""
        await self._build_flattened_items_structure(project_state)
    
    async def _send_project_state_update(self, project_state: ProjectState, server_project_id: str = None):
        """Send project state update to the specific client session only."""
        logger.debug("🔍 [TRACE] _send_project_state_update called for session: %s", project_state.client_session_id)
        
        # Create state signature for change detection
        current_state_signature = {
            "git_branch": project_state.git_branch,
            "git_status_summary": project_state.git_status_summary,
            "git_detailed_status": str(project_state.git_detailed_status) if project_state.git_detailed_status else None,
            "open_tabs": tuple((tab.tab_id, tab.tab_type, tab.title) for tab in project_state.open_tabs.values()),
            "active_tab": project_state.active_tab.tab_id if project_state.active_tab else None,
            "items_count": len(project_state.items),
            "monitored_folders": tuple((mf.folder_path, mf.is_expanded) for mf in sorted(project_state.monitored_folders, key=lambda x: x.folder_path))
        }
        
        logger.debug("🔍 [TRACE] Current state signature: %s", current_state_signature)
        
        # Check if state has actually changed
        last_signature = getattr(project_state, '_last_sent_signature', None)
        logger.debug("🔍 [TRACE] Last sent signature: %s", last_signature)
        
        if last_signature == current_state_signature:
            logger.debug("🔍 [TRACE] ❌ Project state unchanged, skipping update for client: %s", project_state.client_session_id)
            return
        
        # State has changed, send update
        project_state._last_sent_signature = current_state_signature
        logger.debug("🔍 [TRACE] ✅ State has changed, preparing to send update to client: %s", project_state.client_session_id)
        
        payload = {
            "event": "project_state_update",
            "project_id": server_project_id or project_state.client_session_id,  # Use server ID if provided
            "project_folder_path": project_state.project_folder_path,
            "is_git_repo": project_state.is_git_repo,
            "git_branch": project_state.git_branch,
            "git_status_summary": project_state.git_status_summary,
            "git_detailed_status": asdict(project_state.git_detailed_status) if project_state.git_detailed_status and hasattr(project_state.git_detailed_status, '__dataclass_fields__') else (logger.warning(f"git_detailed_status is not a dataclass: {type(project_state.git_detailed_status)} - {project_state.git_detailed_status}") or None),
            "open_tabs": [self._serialize_tab_info(tab) for tab in project_state.open_tabs.values()],
            "active_tab": self._serialize_tab_info(project_state.active_tab) if project_state.active_tab else None,
            "items": [self._serialize_file_item(item) for item in project_state.items],
            "timestamp": time.time(),
            "client_sessions": [project_state.client_session_id]  # Target only this client session
        }
        
        # Log payload size analysis before sending
        try:
            import json
            payload_json = json.dumps(payload)
            payload_size_kb = len(payload_json.encode('utf-8')) / 1024
            
            if payload_size_kb > 100:  # Log for large project state updates
                logger.warning("📦 Large project_state_update: %.1f KB for client %s", 
                              payload_size_kb, project_state.client_session_id)
                
                # Analyze which parts are large
                large_components = []
                for key, value in payload.items():
                    if key in ['open_tabs', 'active_tab', 'items', 'git_detailed_status']:
                        component_size = len(json.dumps(value).encode('utf-8')) / 1024
                        if component_size > 10:  # Components > 10KB
                            large_components.append(f"{key}: {component_size:.1f}KB")
                
                if large_components:
                    logger.warning("📦 Large components in project_state_update: %s", ", ".join(large_components))
                
                # Special analysis for active_tab which often contains HTML diff
                if payload.get('active_tab') and isinstance(payload['active_tab'], dict):
                    active_tab = payload['active_tab']
                    tab_type = active_tab.get('tab_type', 'unknown')
                    if tab_type == 'diff' and active_tab.get('metadata'):
                        metadata = active_tab['metadata']
                        if 'html_diff_versions' in metadata:
                            html_diff_size = len(json.dumps(metadata['html_diff_versions']).encode('utf-8')) / 1024
                            logger.warning("📦 HTML diff in active_tab: %.1f KB (tab_type: %s)", html_diff_size, tab_type)
                            
            elif payload_size_kb > 50:
                logger.info("📦 Medium project_state_update: %.1f KB for client %s", 
                           payload_size_kb, project_state.client_session_id)
        
        except Exception as e:
            logger.warning("Failed to analyze payload size: %s", e)
        
        # Send via control channel with client session targeting
        logger.debug("🔍 [TRACE] About to send payload via control channel...")
        try:
            await self.control_channel.send(payload)
            logger.debug("🔍 [TRACE] ✅ Successfully sent project_state_update to client: %s", project_state.client_session_id)
        except Exception as e:
            logger.error("🔍 [TRACE] ❌ Failed to send project_state_update: %s", e)
    
    async def cleanup_project(self, client_session_id: str):
        """Clean up project state and resources."""
        lock = self._get_session_lock(client_session_id)
        async with lock:
            project_folder_path = self._cleanup_project_locked(client_session_id)
        await self._release_shared_git_manager(project_folder_path, client_session_id)

    def _cleanup_project_locked(self, client_session_id: str) -> Optional[str]:
        """Internal helper to release resources associated with a project state. Lock must be held."""
        project_state = self.projects.get(client_session_id)
        project_folder_path = project_state.project_folder_path if project_state else None
        
        if project_state:
            # Cancel debounce timer to prevent pending refreshes running after cleanup
            if self._change_debounce_timer and not self._change_debounce_timer.done():
                try:
                    self._change_debounce_timer.cancel()
                except Exception:
                    pass
            # Remove pending file change events related to this project
            if self._pending_changes:
                removed = [path for path in list(self._pending_changes) if path.startswith(project_state.project_folder_path)]
                for path in removed:
                    self._pending_changes.discard(path)
                    self._pending_change_sources.pop(path, None)
                if removed:
                    logger.debug("Removed %d pending change paths for session %s during cleanup", len(removed), client_session_id)
            
            # Stop watching all monitored folders for this project
            for monitored_folder in project_state.monitored_folders:
                self.file_watcher.stop_watching(monitored_folder.folder_path)
            
            # Stop watching .git directory if it was being monitored
            if project_state.is_git_repo:
                git_dir_path = os.path.join(project_state.project_folder_path, '.git')
                self.file_watcher.stop_watching(git_dir_path)
            
            self.projects.pop(client_session_id, None)
            logger.info("Cleaned up project state: %s", client_session_id)
        else:
            logger.info("No project state found for client session: %s during cleanup", client_session_id)
        
        # Clean up associated git manager even if project state was not registered
        # (actual cleanup occurs when shared git manager refcount drops to zero)
        self._write_debug_state()
        return project_folder_path
    
    async def cleanup_projects_by_client_session(self, client_session_id: str):
        """Clean up project state for a specific client session when explicitly notified of disconnection."""
        logger.info("Explicitly cleaning up project state for disconnected client session: %s", client_session_id)
        
        # With the new design, each client session has only one project
        if client_session_id in self.projects:
            await self.cleanup_project(client_session_id)
            logger.info("Cleaned up project state for client session: %s", client_session_id)
        else:
            logger.info("No project state found for client session: %s", client_session_id)
    
    async def cleanup_all_projects(self):
        """Clean up all project states. Used for shutdown or reset."""
        logger.info("Cleaning up all project states")
        
        client_session_ids = list(self.projects.keys())
        for client_session_id in client_session_ids:
            await self.cleanup_project(client_session_id)
        
        logger.info("Cleaned up %d project states", len(client_session_ids))
    
    async def refresh_project_state_for_file_change(self, file_path: str):
        """Public method to trigger project state refresh for a specific file change."""
        logger.info(f"Manual refresh triggered for file change: {file_path}")
        
        # Find project states that include this file path
        for client_session_id, project_state in self.projects.items():
            project_folder = Path(project_state.project_folder_path)
            
            # Check if the file is within this project
            try:
                Path(file_path).relative_to(project_folder)
                # File is within this project, trigger refresh
                logger.info(f"Refreshing project state for session {client_session_id} after file change: {file_path}")
                await self._refresh_project_state(client_session_id, reason="manual_file_change")
                break
            except ValueError:
                # File is not within this project
                continue

    async def cleanup_orphaned_project_states(self, current_client_sessions: List[str]):
        """Clean up project states that don't match any current client session."""
        current_sessions_set = set(current_client_sessions)
        orphaned_keys = []
        
        for session_id in list(self.projects.keys()):
            if session_id not in current_sessions_set:
                orphaned_keys.append(session_id)
        
        if orphaned_keys:
            logger.info("Found %d orphaned project states, cleaning up: %s", len(orphaned_keys), orphaned_keys)
            for session_id in orphaned_keys:
                await self.cleanup_project(session_id)
            logger.info("Cleaned up %d orphaned project states", len(orphaned_keys))
        else:
            logger.debug("No orphaned project states found")


# Helper function for other handlers to get/create project state manager
def get_or_create_project_state_manager(context: Dict[str, Any], control_channel) -> 'ProjectStateManager':
    """Get or create project state manager with debug setup (SINGLETON PATTERN)."""
    global _global_project_state_manager
    
    logger.info("get_or_create_project_state_manager called")
    logger.info("Context debug flag: %s", context.get("debug", False))
    
    with _manager_lock:
        if _global_project_state_manager is None:
            logger.info("Creating new GLOBAL ProjectStateManager (singleton)")
            manager = ProjectStateManager(control_channel, context)
            
            # Set up debug mode if enabled
            if context.get("debug", False):
                debug_file_path = os.path.join(os.getcwd(), "project_state_debug.json")
                logger.info("Setting up debug mode with file: %s", debug_file_path)
                manager.set_debug_mode(True, debug_file_path)
            else:
                logger.info("Debug mode not enabled in context")
            
            _global_project_state_manager = manager
            logger.info("Created and stored new GLOBAL manager (PID: %s)", os.getpid())
            return manager
        else:
            logger.info("Returning existing GLOBAL project state manager (PID: %s)", os.getpid())
            # Update the control channel reference in case it changed
            _global_project_state_manager.control_channel = control_channel
            
            # Log active project states for debugging
            if _global_project_state_manager.projects:
                logger.debug("Active project states: %s", list(_global_project_state_manager.projects.keys()))
            else:
                logger.debug("No active project states in global manager")
            
            return _global_project_state_manager


def get_global_project_state_manager() -> Optional['ProjectStateManager']:
    """Return the current global project state manager if it exists."""
    with _manager_lock:
        return _global_project_state_manager


def reset_global_project_state_manager():
    """Reset the global project state manager (for testing/cleanup)."""
    global _global_project_state_manager
    with _manager_lock:
        if _global_project_state_manager:
            logger.info("Resetting global project state manager")
            _global_project_state_manager = None
        else:
            logger.debug("Global project state manager already None")


def debug_global_manager_state():
    """Debug function to log the current state of the global manager."""
    global _global_project_state_manager
    with _manager_lock:
        if _global_project_state_manager:
            logger.info("Global ProjectStateManager exists (PID: %s)", os.getpid())
            logger.info("Active project states: %s", list(_global_project_state_manager.projects.keys()))
            logger.info("Total project states: %d", len(_global_project_state_manager.projects))
        else:
            logger.info("No global ProjectStateManager exists (PID: %s)", os.getpid())
