"""Project state handlers for maintaining project folder structure and git metadata."""

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union
from dataclasses import dataclass, asdict
import platform

from .base import AsyncHandler, SyncHandler

logger = logging.getLogger(__name__)

# Import GitPython with fallback
try:
    import git
    from git import Repo, InvalidGitRepositoryError
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False
    git = None
    Repo = None
    InvalidGitRepositoryError = Exception

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


@dataclass
class TabInfo:
    """Represents an editor tab with content and metadata."""
    tab_id: str  # Unique identifier for the tab
    tab_type: str  # 'file', 'diff', 'untitled', 'image', 'audio', 'video'
    title: str  # Display title for the tab
    file_path: Optional[str] = None  # Path for file-based tabs
    content: Optional[str] = None  # Text content or base64 for media
    original_content: Optional[str] = None  # For diff view
    modified_content: Optional[str] = None  # For diff view
    is_dirty: bool = False  # Has unsaved changes
    mime_type: Optional[str] = None  # For media files
    encoding: Optional[str] = None  # Content encoding (base64, utf-8, etc.)
    metadata: Optional[Dict[str, Any]] = None  # Additional metadata

@dataclass
class MonitoredFolder:
    """Represents a folder that is being monitored for changes."""
    folder_path: str
    is_expanded: bool = False

@dataclass
class FileItem:
    """Represents a file or directory item with metadata."""
    name: str
    path: str
    is_directory: bool
    parent_path: str
    size: Optional[int] = None
    modified_time: Optional[float] = None
    is_git_tracked: Optional[bool] = None
    git_status: Optional[str] = None
    is_hidden: bool = False
    is_ignored: bool = False
    children: Optional[List['FileItem']] = None
    is_expanded: bool = False
    is_loaded: bool = False


@dataclass
class GitFileChange:
    """Represents a single file change in git."""
    file_repo_path: str  # Relative path from repository root
    file_name: str  # Just the filename (basename)
    file_abs_path: str  # Absolute path to the file
    change_type: str  # 'added', 'modified', 'deleted', 'untracked' - follows git's native types
    content_hash: Optional[str] = None  # SHA256 hash of current file content
    is_staged: bool = False  # Whether this change is staged


@dataclass
class GitDetailedStatus:
    """Represents detailed git status with file hashes."""
    head_commit_hash: Optional[str] = None  # Hash of HEAD commit
    staged_changes: List[GitFileChange] = None  # Changes in the staging area
    unstaged_changes: List[GitFileChange] = None  # Changes in working directory
    untracked_files: List[GitFileChange] = None  # Untracked files
    
    def __post_init__(self):
        if self.staged_changes is None:
            self.staged_changes = []
        if self.unstaged_changes is None:
            self.unstaged_changes = []
        if self.untracked_files is None:
            self.untracked_files = []


@dataclass
class ProjectState:
    """Represents the complete state of a project."""
    client_session_key: str  # The composite key: client_session_id + "_" + hash(project_folder_path)
    project_folder_path: str
    items: List[FileItem]
    monitored_folders: List[MonitoredFolder] = None
    is_git_repo: bool = False
    git_branch: Optional[str] = None
    git_status_summary: Optional[Dict[str, int]] = None  # Kept for backward compatibility
    git_detailed_status: Optional[GitDetailedStatus] = None  # New detailed git state
    open_tabs: Dict[str, 'TabInfo'] = None  # Changed from List to Dict with unique keys
    active_tab: Optional['TabInfo'] = None
    
    def __post_init__(self):
        if self.open_tabs is None:
            self.open_tabs = {}
        if self.monitored_folders is None:
            self.monitored_folders = []
    
    @property
    def client_session_id(self) -> str:
        """Extract the clean client session ID from the composite key."""
        return self.client_session_key.split('_')[0]


class GitManager:
    """Manages Git operations for project state."""
    
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.repo: Optional[Repo] = None
        self.is_git_repo = False
        self._initialize_repo()
    
    def _initialize_repo(self):
        """Initialize Git repository if available."""
        if not GIT_AVAILABLE:
            logger.warning("GitPython not available, Git features disabled")
            return
        
        try:
            self.repo = Repo(self.project_path)
            self.is_git_repo = True
            logger.info("Initialized Git repo for project: %s", self.project_path)
        except (InvalidGitRepositoryError, Exception) as e:
            logger.debug("Not a Git repository or Git error: %s", e)
    
    def get_branch_name(self) -> Optional[str]:
        """Get current Git branch name."""
        if not self.is_git_repo or not self.repo:
            return None
        
        try:
            return self.repo.active_branch.name
        except Exception as e:
            logger.debug("Could not get Git branch: %s", e)
            return None
    
    def get_file_status(self, file_path: str) -> Dict[str, Any]:
        """Get Git status for a specific file or directory."""
        if not self.is_git_repo or not self.repo:
            return {"is_tracked": False, "status": None, "is_ignored": False}
        
        try:
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Check if ignored - GitPython handles path normalization internally
            is_ignored = self.repo.ignored(rel_path)
            if is_ignored:
                return {"is_tracked": False, "status": "ignored", "is_ignored": True}
            
            # For directories, only report status if they contain tracked or untracked files
            if os.path.isdir(file_path):
                # Check if directory contains any untracked files using path.startswith()
                # This handles cross-platform path separators correctly
                has_untracked = any(
                    os.path.commonpath([f, rel_path]) == rel_path and f != rel_path
                    for f in self.repo.untracked_files
                )
                if has_untracked:
                    return {"is_tracked": False, "status": "untracked", "is_ignored": False}
                
                # Check if directory is dirty - GitPython handles path normalization
                if self.repo.is_dirty(path=rel_path):
                    return {"is_tracked": True, "status": "modified", "is_ignored": False}
                
                # Check if directory has tracked files - let GitPython handle paths
                try:
                    tracked_files = self.repo.git.ls_files(rel_path)
                    is_tracked = bool(tracked_files.strip())
                    status = "clean" if is_tracked else None
                    return {"is_tracked": is_tracked, "status": status, "is_ignored": False}
                except Exception:
                    return {"is_tracked": False, "status": None, "is_ignored": False}
            
            # For files
            else:
                # Check if untracked - direct comparison works cross-platform
                if rel_path in self.repo.untracked_files:
                    return {"is_tracked": False, "status": "untracked", "is_ignored": False}
                
                # Check if tracked and dirty - GitPython handles path normalization
                if self.repo.is_dirty(path=rel_path):
                    return {"is_tracked": True, "status": "modified", "is_ignored": False}
                
                # Check if tracked and clean - GitPython handles paths
                try:
                    self.repo.git.ls_files(rel_path, error_unmatch=True)
                    return {"is_tracked": True, "status": "clean", "is_ignored": False}
                except Exception:
                    return {"is_tracked": False, "status": None, "is_ignored": False}
                    
        except Exception as e:
            logger.debug("Error getting Git status for %s: %s", file_path, e)
            return {"is_tracked": False, "status": None, "is_ignored": False}
    
    def get_status_summary(self) -> Dict[str, int]:
        """Get summary of Git status."""
        if not self.is_git_repo or not self.repo:
            return {}
        
        try:
            status = self.repo.git.status(porcelain=True).strip()
            if not status:
                return {"clean": 0}
            
            summary = {"modified": 0, "added": 0, "deleted": 0, "untracked": 0}
            
            for line in status.split('\n'):
                if len(line) >= 2:
                    index_status = line[0]
                    worktree_status = line[1]
                    
                    if index_status == 'A' or worktree_status == 'A':
                        summary["added"] += 1
                    elif index_status == 'M' or worktree_status == 'M':
                        summary["modified"] += 1
                    elif index_status == 'D' or worktree_status == 'D':
                        summary["deleted"] += 1
                    elif index_status == '?' and worktree_status == '?':
                        summary["untracked"] += 1
            
            return summary
            
        except Exception as e:
            logger.debug("Error getting Git status summary: %s", e)
            return {}
    
    def _compute_file_hash(self, file_path: str) -> Optional[str]:
        """Compute SHA256 hash of file content."""
        try:
            with open(file_path, 'rb') as f:
                file_hash = hashlib.sha256()
                chunk = f.read(8192)
                while chunk:
                    file_hash.update(chunk)
                    chunk = f.read(8192)
                return file_hash.hexdigest()
        except (OSError, IOError) as e:
            logger.debug("Error computing hash for %s: %s", file_path, e)
            return None
    
    def get_head_commit_hash(self) -> Optional[str]:
        """Get the hash of the HEAD commit."""
        if not self.is_git_repo or not self.repo:
            return None
        
        try:
            return self.repo.head.commit.hexsha
        except Exception as e:
            logger.debug("Error getting HEAD commit hash: %s", e)
            return None
    
    def get_detailed_status(self) -> GitDetailedStatus:
        """Get detailed Git status with file hashes using GitPython APIs."""
        if not self.is_git_repo or not self.repo:
            return GitDetailedStatus()
        
        try:
            detailed_status = GitDetailedStatus()
            detailed_status.head_commit_hash = self.get_head_commit_hash()
            
            # Get all changed files using GitPython's index diff
            # Get staged changes (index vs HEAD)
            staged_files = self.repo.index.diff("HEAD")
            for diff_item in staged_files:
                file_repo_path = diff_item.a_path or diff_item.b_path
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                
                # Determine change type - stick to git's native types
                if diff_item.deleted_file:
                    change_type = 'deleted'
                    content_hash = None
                elif diff_item.new_file:
                    change_type = 'added'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                else:
                    # For modified files (including renames that git detected)
                    change_type = 'modified'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type=change_type,
                    content_hash=content_hash,
                    is_staged=True
                )
                logger.info("GIT STATUS DEBUG - Created staged change: %s", asdict(change))
                detailed_status.staged_changes.append(change)
            
            # Get unstaged changes (working tree vs index)
            unstaged_files = self.repo.index.diff(None)
            for diff_item in unstaged_files:
                file_repo_path = diff_item.a_path or diff_item.b_path
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                
                # Determine change type - stick to git's native types
                if diff_item.deleted_file:
                    change_type = 'deleted'
                    content_hash = None
                elif diff_item.new_file:
                    change_type = 'added'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                else:
                    change_type = 'modified'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type=change_type,
                    content_hash=content_hash,
                    is_staged=False
                )
                logger.info("GIT STATUS DEBUG - Created unstaged change: %s", asdict(change))
                detailed_status.unstaged_changes.append(change)
            
            # Get untracked files
            untracked_files = self.repo.untracked_files
            for file_repo_path in untracked_files:
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type='untracked',
                    content_hash=content_hash,
                    is_staged=False
                )
                logger.info("GIT STATUS DEBUG - Created untracked change: %s", asdict(change))
                detailed_status.untracked_files.append(change)
            
            return detailed_status
            
        except Exception as e:
            logger.error("Error getting detailed Git status: %s", e)
            return GitDetailedStatus()
    
    def _get_change_type(self, status_char: str) -> str:
        """Convert git status character to change type."""
        status_map = {
            'A': 'added',
            'M': 'modified', 
            'D': 'deleted',
            'R': 'renamed',
            'C': 'copied',
            'U': 'unmerged',
            '?': 'untracked'
        }
        return status_map.get(status_char, 'unknown')
    
    def get_file_content_at_commit(self, file_path: str, commit_hash: Optional[str] = None) -> Optional[str]:
        """Get file content at a specific commit. If commit_hash is None, gets HEAD content."""
        if not self.is_git_repo or not self.repo:
            return None
        
        try:
            if commit_hash is None:
                commit_hash = 'HEAD'
            
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Get file content at the specified commit
            try:
                content = self.repo.git.show(f"{commit_hash}:{rel_path}")
                return content
            except Exception as e:
                logger.debug("File %s not found at commit %s: %s", rel_path, commit_hash, e)
                return None
                
        except Exception as e:
            logger.error("Error getting file content at commit %s for %s: %s", commit_hash, file_path, e)
            return None
    
    def get_file_content_staged(self, file_path: str) -> Optional[str]:
        """Get staged content of a file."""
        if not self.is_git_repo or not self.repo:
            return None
        
        try:
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Get staged content
            try:
                content = self.repo.git.show(f":{rel_path}")
                return content
            except Exception as e:
                logger.debug("File %s not found in staging area: %s", rel_path, e)
                return None
                
        except Exception as e:
            logger.error("Error getting staged content for %s: %s", file_path, e)
            return None


class FileSystemWatcher:
    """Watches file system changes for project folders."""
    
    def __init__(self, project_manager: 'ProjectStateManager'):
        self.project_manager = project_manager
        self.observer: Optional[Observer] = None
        self.event_handler: Optional[FileSystemEventHandler] = None
        self.watched_paths: Set[str] = set()
        # Store reference to the event loop for thread-safe async task creation
        try:
            self.event_loop = asyncio.get_running_loop()
            logger.info("Captured event loop reference for file system watcher")
        except RuntimeError:
            self.event_loop = None
            logger.warning("No running event loop found - file system events may not work correctly")
        
        if WATCHDOG_AVAILABLE:
            self._initialize_watcher()
    
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
                # Skip debug files to avoid feedback loops
                if event.src_path.endswith('project_state_debug.json'):
                    return
                
                # Only process events that represent actual content changes
                # Skip opened/closed events that don't indicate file modifications
                if event.event_type in ('opened', 'closed'):
                    return
                
                # Handle .git folder events separately for git status monitoring
                path_parts = Path(event.src_path).parts
                if '.git' in path_parts:
                    # Get the relative path within .git directory
                    try:
                        git_index = path_parts.index('.git')
                        git_relative_path = '/'.join(path_parts[git_index + 1:])
                        git_file = Path(event.src_path).name
                        
                        # Monitor git files that indicate repository state changes
                        should_monitor_git_file = (
                            git_file == 'index' or  # Staging area changes
                            git_file == 'HEAD' or   # Branch switches
                            git_relative_path.startswith('refs/heads/') or  # Branch updates
                            git_relative_path.startswith('refs/remotes/') or  # Remote tracking branches
                            git_relative_path.startswith('logs/refs/heads/') or  # Branch history
                            git_relative_path.startswith('logs/HEAD')  # HEAD history
                        )
                        
                        if should_monitor_git_file:
                            logger.info("Git status change detected: %s - %s", event.event_type, event.src_path)
                        else:
                            return  # Skip other .git files
                    except (ValueError, IndexError):
                        return  # Skip if can't parse .git path
                else:
                    logger.info("File system event: %s - %s", event.event_type, event.src_path)
                
                # Schedule async task in the main event loop from this watchdog thread
                if self.watcher.event_loop and not self.watcher.event_loop.is_closed():
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.manager._handle_file_change(event), 
                            self.watcher.event_loop
                        )
                        logger.debug("Successfully scheduled file change handler for: %s", event.src_path)
                    except Exception as e:
                        logger.error("Failed to schedule file change handler: %s", e)
                else:
                    logger.warning("No event loop available to handle file change: %s", event.src_path)
        
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
                self.observer.schedule(self.event_handler, path, recursive=False)
                self.watched_paths.add(path)
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
                self.observer.schedule(self.event_handler, git_path, recursive=True)
                self.watched_paths.add(git_path)
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
            # Note: watchdog doesn't have direct path removal, would need to recreate observer
            self.watched_paths.discard(path)
            logger.debug("Stopped watching path: %s", path)
    
    def stop_all(self):
        """Stop all file watching."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            self.watched_paths.clear()


class ProjectStateManager:
    """Manages project state for client sessions."""
    
    def __init__(self, control_channel, context: Dict[str, Any]):
        self.control_channel = control_channel
        self.context = context
        self.projects: Dict[str, ProjectState] = {}
        self.git_managers: Dict[str, GitManager] = {}
        self.file_watcher = FileSystemWatcher(self)
        self.debug_mode = False
        self.debug_file_path: Optional[str] = None
        
        # Debouncing for file changes
        self._change_debounce_timer: Optional[asyncio.Task] = None
        self._pending_changes: Set[str] = set()
    
    def set_debug_mode(self, enabled: bool, debug_file_path: Optional[str] = None):
        """Enable or disable debug mode with JSON output."""
        self.debug_mode = enabled
        self.debug_file_path = debug_file_path
        if enabled:
            logger.info("Project state debug mode enabled, output to: %s", debug_file_path)
    
    def _write_debug_state(self):
        """Write current state to debug JSON file."""
        logger.debug("_write_debug_state called: debug_mode=%s, debug_file_path=%s", self.debug_mode, self.debug_file_path)
        if not self.debug_mode or not self.debug_file_path:
            logger.debug("Debug mode not enabled or no debug file path, skipping debug write")
            return
        
        try:
            debug_data = {}
            for project_id, state in self.projects.items():
                debug_data[project_id] = {
                    "project_folder_path": state.project_folder_path,
                    "is_git_repo": state.is_git_repo,
                    "git_branch": state.git_branch,
                    "git_status_summary": state.git_status_summary,
                    "git_detailed_status": asdict(state.git_detailed_status) if state.git_detailed_status else None,
                    "open_tabs": [self._serialize_tab_info(tab) for tab in state.open_tabs.values()],
                    "active_tab": self._serialize_tab_info(state.active_tab) if state.active_tab else None,
                    "monitored_folders": [asdict(mf) for mf in state.monitored_folders],
                    "items": [self._serialize_file_item(item) for item in state.items]
                }
            
            with open(self.debug_file_path, 'w', encoding='utf-8') as f:
                json.dump(debug_data, f, indent=2, default=str)
            
            logger.debug("Debug state written successfully to: %s", self.debug_file_path)
            logger.debug("Debug data summary: %d projects", len(debug_data))
            for project_id, data in debug_data.items():
                logger.debug("Project %s: %d monitored_folders, %d items", 
                           project_id, len(data.get('monitored_folders', [])), len(data.get('items', [])))
                
        except Exception as e:
            logger.error("Error writing debug state: %s", e)
    
    def _serialize_file_item(self, item: FileItem) -> Dict[str, Any]:
        """Serialize FileItem for JSON output."""
        result = asdict(item)
        if item.children:
            result["children"] = [self._serialize_file_item(child) for child in item.children]
        return result
    
    def _serialize_tab_info(self, tab: TabInfo) -> Dict[str, Any]:
        """Serialize TabInfo for JSON output."""
        return asdict(tab)
    
    async def initialize_project_state(self, client_session: str, project_folder_path: str) -> ProjectState:
        """Initialize project state for a client session."""
        client_session_key = f"{client_session}_{hash(project_folder_path)}"
        
        if client_session_key in self.projects:
            return self.projects[client_session_key]
        
        logger.info("Initializing project state for client session: %s, folder: %s", client_session, project_folder_path)
        
        # Initialize Git manager
        git_manager = GitManager(project_folder_path)
        self.git_managers[client_session_key] = git_manager
        
        # Create project state
        project_state = ProjectState(
            client_session_key=client_session_key,
            project_folder_path=project_folder_path,
            items=[],
            is_git_repo=git_manager.is_git_repo,
            git_branch=git_manager.get_branch_name(),
            git_status_summary=git_manager.get_status_summary(),
            git_detailed_status=git_manager.get_detailed_status()
        )
        
        # Initialize monitored folders with project root and its immediate subdirectories
        await self._initialize_monitored_folders(project_state)
        
        # Sync all dependent state (items, watchdog)
        await self._sync_all_state_with_monitored_folders(project_state)
        
        self.projects[client_session_key] = project_state
        self._write_debug_state()
        
        return project_state
    
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
        
        # For git repositories, also watch the .git directory for git status changes
        if project_state.is_git_repo:
            git_dir_path = os.path.join(project_state.project_folder_path, '.git')
            if os.path.exists(git_dir_path):
                self.file_watcher.start_watching_git_directory(git_dir_path)
                logger.debug("Started monitoring .git directory for git status changes: %s", git_dir_path)
        
        logger.debug("Watchdog synchronized: watching %d monitored folders individually", len(project_state.monitored_folders))
    
    async def _sync_all_state_with_monitored_folders(self, project_state: ProjectState):
        """Synchronize all dependent state (watchdog, items) with monitored_folders changes."""
        logger.debug("_sync_all_state_with_monitored_folders called")
        logger.debug("Current monitored_folders count: %d", len(project_state.monitored_folders))
        
        # Sync watchdog monitoring
        logger.debug("Syncing watchdog monitoring")
        await self._sync_watchdog_with_monitored_folders(project_state)
        
        # Rebuild items structure from all monitored folders
        logger.debug("Rebuilding items structure")
        await self._build_flattened_items_structure(project_state)
        logger.debug("Items count after rebuild: %d", len(project_state.items))
        
        # Update debug state
        logger.debug("Writing debug state")
        self._write_debug_state()
        logger.debug("_sync_all_state_with_monitored_folders completed")
    
    async def _add_subdirectories_to_monitored(self, project_state: ProjectState, parent_folder_path: str):
        """Add all subdirectories of a folder to monitored_folders if not already present."""
        logger.info("_add_subdirectories_to_monitored called for: %s", parent_folder_path)
        try:
            existing_paths = {mf.folder_path for mf in project_state.monitored_folders}
            logger.info("Existing monitored paths: %s", existing_paths)
            added_any = False
            
            with os.scandir(parent_folder_path) as entries:
                for entry in entries:
                    if entry.is_dir() and entry.name != '.git':  # Only exclude .git, allow other dot folders
                        logger.info("Found subdirectory: %s", entry.path)
                        if entry.path not in existing_paths:
                            logger.info("Adding new monitored folder: %s", entry.path)
                            new_monitored = MonitoredFolder(folder_path=entry.path, is_expanded=False)
                            project_state.monitored_folders.append(new_monitored)
                            added_any = True
                        else:
                            logger.info("Subdirectory already monitored: %s", entry.path)
            
            logger.info("Added any new folders: %s", added_any)
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
        git_manager = self.git_managers.get(project_state.client_session_key)
        
        try:
            items = []
            
            # Use os.scandir for better performance
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    try:
                        # Skip .git folders and their contents
                        if entry.name == '.git' and entry.is_dir():
                            continue
                            
                        stat_info = entry.stat()
                        is_hidden = entry.name.startswith('.')
                        
                        # Get Git status if available
                        git_info = {"is_tracked": False, "status": None, "is_ignored": False}
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
        all_items = []
        
        # Create sets for quick lookup
        expanded_paths = {mf.folder_path for mf in project_state.monitored_folders if mf.is_expanded}
        monitored_paths = {mf.folder_path for mf in project_state.monitored_folders}
        
        # Load items from ALL monitored folders
        for monitored_folder in project_state.monitored_folders:
            # Load direct children of this monitored folder
            children = await self._load_directory_items_list(monitored_folder.folder_path, monitored_folder.folder_path)
            
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
        
        # Remove duplicates (items might be loaded multiple times due to nested monitoring)
        # Use a dict to deduplicate by path while preserving the last loaded state
        items_dict = {}
        for item in all_items:
            items_dict[item.path] = item
        
        # Convert back to list and sort for consistent ordering
        project_state.items = list(items_dict.values())
        project_state.items.sort(key=lambda x: (x.parent_path, not x.is_directory, x.name.lower()))
    
    async def _load_directory_items_list(self, directory_path: str, parent_path: str) -> List[FileItem]:
        """Load directory items and return as a list with parent_path."""
        git_manager = None
        for manager in self.git_managers.values():
            if directory_path.startswith(manager.project_path):
                git_manager = manager
                break
        
        items = []
        
        try:
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    try:
                        # Skip .git folders and their contents
                        if entry.name == '.git' and entry.is_dir():
                            continue
                            
                        stat_info = entry.stat()
                        is_hidden = entry.name.startswith('.')
                        
                        # Get Git status if available
                        git_info = {"is_tracked": False, "status": None, "is_ignored": False}
                        if git_manager:
                            git_info = git_manager.get_file_status(entry.path)
                        
                        file_item = FileItem(
                            name=entry.name,
                            path=entry.path,
                            is_directory=entry.is_dir(),
                            parent_path=parent_path,
                            size=stat_info.st_size if entry.is_file() else None,
                            modified_time=stat_info.st_mtime,
                            is_git_tracked=git_info["is_tracked"],
                            git_status=git_info["status"],
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
    
    async def expand_folder(self, client_session_key: str, folder_path: str) -> bool:
        """Expand a folder and load its contents."""
        logger.info("expand_folder called: client_session_key=%s, folder_path=%s", client_session_key, folder_path)
        
        if client_session_key not in self.projects:
            logger.error("Project state not found for key: %s", client_session_key)
            return False
        
        project_state = self.projects[client_session_key]
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
    
    async def collapse_folder(self, client_session_key: str, folder_path: str) -> bool:
        """Collapse a folder."""
        if client_session_key not in self.projects:
            return False
        
        project_state = self.projects[client_session_key]
        
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
    
    async def open_file(self, client_session_key: str, file_path: str, set_active: bool = True) -> bool:
        """Open a file in a new tab with content loaded."""
        if client_session_key not in self.projects:
            return False
        
        project_state = self.projects[client_session_key]
        
        # Generate unique key for file tab
        tab_key = generate_tab_key('file', file_path)
        
        # Check if file is already open
        if tab_key in project_state.open_tabs:
            existing_tab = project_state.open_tabs[tab_key]
            if set_active:
                project_state.active_tab = existing_tab
            self._write_debug_state()
            return True
        
        # Create new file tab using tab factory
        from .tab_factory import get_tab_factory
        tab_factory = get_tab_factory()
        
        try:
            new_tab = await tab_factory.create_file_tab(file_path)
            project_state.open_tabs[tab_key] = new_tab
            if set_active:
                project_state.active_tab = new_tab
            
            logger.info(f"Opened file tab: {file_path} (content loaded: {len(new_tab.content or '') > 0})")
            self._write_debug_state()
            return True
        except Exception as e:
            logger.error(f"Failed to create tab for file {file_path}: {e}")
            return False
    
    async def close_tab(self, client_session_key: str, tab_id: str) -> bool:
        """Close a tab by tab ID."""
        if client_session_key not in self.projects:
            return False
        
        project_state = self.projects[client_session_key]
        
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
        
        self._write_debug_state()
        return True
    
    async def set_active_tab(self, client_session_key: str, tab_id: Optional[str]) -> bool:
        """Set the currently active tab."""
        if client_session_key not in self.projects:
            return False
        
        project_state = self.projects[client_session_key]
        
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
        
        self._write_debug_state()
        return True
    
    async def open_diff_tab(self, client_session_key: str, file_path: str, 
                           from_ref: str, to_ref: str, from_hash: Optional[str] = None, 
                           to_hash: Optional[str] = None) -> bool:
        """Open a diff tab comparing file versions at different git timeline points."""
        if client_session_key not in self.projects:
            return False
        
        project_state = self.projects[client_session_key]
        git_manager = self.git_managers.get(client_session_key)
        
        if not git_manager or not git_manager.is_git_repo:
            logger.error("Cannot create diff tab: not a git repository")
            return False
        
        # Generate unique key for diff tab
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
            from .tab_factory import get_tab_factory
            tab_factory = get_tab_factory()
            
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
                file_path, original_content, modified_content, diff_title
            )
            
            # Add metadata about the diff references
            diff_tab.metadata.update({
                'from_ref': from_ref,
                'to_ref': to_ref,
                'from_hash': from_hash,
                'to_hash': to_hash,
                'diff_timeline': True
            })
            
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
        logger.debug("Processing file change: %s - %s", event.event_type, event.src_path)
        
        self._pending_changes.add(event.src_path)
        
        # Cancel existing timer
        if self._change_debounce_timer:
            self._change_debounce_timer.cancel()
        
        # Set new timer
        logger.debug("Starting debounce timer for file changes")
        self._change_debounce_timer = asyncio.create_task(self._process_pending_changes())
    
    async def _process_pending_changes(self):
        """Process pending file changes after debounce delay."""
        logger.info("Processing %d pending file changes after debounce", len(self._pending_changes))
        await asyncio.sleep(0.5)  # Debounce delay
        
        if not self._pending_changes:
            logger.debug("No pending changes to process")
            return
        
        logger.debug("Pending changes: %s", list(self._pending_changes))
        
        # Process changes for each affected project
        affected_projects = set()
        for change_path in self._pending_changes:
            logger.debug("Checking change path: %s", change_path)
            for client_session_key, project_state in self.projects.items():
                if change_path.startswith(project_state.project_folder_path):
                    logger.debug("Change affects project: %s", client_session_key)
                    affected_projects.add(client_session_key)
        
        logger.info("Refreshing %d affected projects", len(affected_projects))
        
        # Refresh affected projects
        for client_session_key in affected_projects:
            logger.debug("Refreshing project state: %s", client_session_key)
            await self._refresh_project_state(client_session_key)
        
        self._pending_changes.clear()
        logger.debug("Finished processing file changes")
    
    async def _refresh_project_state(self, client_session_key: str):
        """Refresh project state after file changes."""
        if client_session_key not in self.projects:
            return
        
        project_state = self.projects[client_session_key]
        git_manager = self.git_managers[client_session_key]
        
        # Update Git status
        if git_manager:
            project_state.git_branch = git_manager.get_branch_name()
            project_state.git_status_summary = git_manager.get_status_summary()
            project_state.git_detailed_status = git_manager.get_detailed_status()
        
        # Sync all dependent state (items, watchdog) - no automatic directory detection
        await self._sync_all_state_with_monitored_folders(project_state)
        
        # Send update to clients
        await self._send_project_state_update(project_state)
    
    async def _detect_and_add_new_directories(self, project_state: ProjectState):
        """Detect new directories in monitored folders and add them to monitoring."""
        # For each currently monitored folder, check if new subdirectories appeared
        monitored_folder_paths = [mf.folder_path for mf in project_state.monitored_folders]
        
        for folder_path in monitored_folder_paths:
            if os.path.exists(folder_path) and os.path.isdir(folder_path):
                await self._add_subdirectories_to_monitored(project_state, folder_path)
    
    async def _reload_visible_structures(self, project_state: ProjectState):
        """Reload all visible structures with flattened items."""
        await self._build_flattened_items_structure(project_state)
    
    async def _send_project_state_update(self, project_state: ProjectState, server_project_id: str = None):
        """Send project state update to the specific client session only."""
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
        
        # Check if state has actually changed
        last_signature = getattr(project_state, '_last_sent_signature', None)
        if last_signature == current_state_signature:
            logger.debug("Project state unchanged, skipping update for client: %s", project_state.client_session_id)
            return
        
        # State has changed, send update
        project_state._last_sent_signature = current_state_signature
        logger.info("Sending project state update to client: %s", project_state.client_session_id)
        
        payload = {
            "event": "project_state_update",
            "project_id": server_project_id or project_state.client_session_key,  # Use server ID if provided
            "project_folder_path": project_state.project_folder_path,
            "is_git_repo": project_state.is_git_repo,
            "git_branch": project_state.git_branch,
            "git_status_summary": project_state.git_status_summary,
            "git_detailed_status": asdict(project_state.git_detailed_status) if project_state.git_detailed_status else None,
            "open_tabs": [self._serialize_tab_info(tab) for tab in project_state.open_tabs.values()],
            "active_tab": self._serialize_tab_info(project_state.active_tab) if project_state.active_tab else None,
            "items": [self._serialize_file_item(item) for item in project_state.items],
            "timestamp": time.time(),
            "client_sessions": [project_state.client_session_id]  # Target only this client session
        }
        
        # Send via control channel with client session targeting
        await self.control_channel.send(payload)
    
    def cleanup_project(self, client_session_key: str):
        """Clean up project state and resources."""
        if client_session_key in self.projects:
            project_state = self.projects[client_session_key]
            
            # Stop watching all monitored folders for this project
            for monitored_folder in project_state.monitored_folders:
                self.file_watcher.stop_watching(monitored_folder.folder_path)
            
            # Stop watching .git directory if it was being monitored
            if project_state.is_git_repo:
                git_dir_path = os.path.join(project_state.project_folder_path, '.git')
                self.file_watcher.stop_watching(git_dir_path)
            
            # Clean up managers
            self.git_managers.pop(client_session_key, None)
            self.projects.pop(client_session_key, None)
            
            logger.info("Cleaned up project state: %s", client_session_key)
            self._write_debug_state()
    
    def cleanup_projects_by_client_session(self, client_session_id: str):
        """Clean up all project states for a specific client session."""
        logger.info("Cleaning up all project states for client session: %s", client_session_id)
        
        # Find all project states that belong to this client session
        keys_to_remove = []
        for client_session_key in self.projects.keys():
            if client_session_key.startswith(client_session_id):
                keys_to_remove.append(client_session_key)
        
        # Clean up each project state
        for client_session_key in keys_to_remove:
            self.cleanup_project(client_session_key)
        
        logger.info("Cleaned up %d project states for client session: %s", len(keys_to_remove), client_session_id)
    
    def cleanup_all_projects(self):
        """Clean up all project states. Used for shutdown or reset."""
        logger.info("Cleaning up all project states")
        
        keys_to_remove = list(self.projects.keys())
        for client_session_key in keys_to_remove:
            self.cleanup_project(client_session_key)
        
        logger.info("Cleaned up %d project states", len(keys_to_remove))


def generate_tab_key(tab_type: str, file_path: str, **kwargs) -> str:
    """Generate a unique key for a tab.
    
    Args:
        tab_type: Type of tab ('file', 'diff', 'untitled', etc.)
        file_path: Path to the file
        **kwargs: Additional parameters for diff tabs (from_ref, to_ref, from_hash, to_hash)
    
    Returns:
        Unique string key for the tab
    """
    import uuid
    
    if tab_type == 'file':
        return file_path
    elif tab_type == 'diff':
        from_ref = kwargs.get('from_ref', '')
        to_ref = kwargs.get('to_ref', '')
        from_hash = kwargs.get('from_hash', '')
        to_hash = kwargs.get('to_hash', '')
        return f"diff:{file_path}:{from_ref}:{to_ref}:{from_hash}:{to_hash}"
    elif tab_type == 'untitled':
        # For untitled tabs, use the tab_id as the key since they don't have a file path
        return kwargs.get('tab_id', str(uuid.uuid4()))
    else:
        # For other tab types, use file_path if available, otherwise tab_id
        return file_path if file_path else kwargs.get('tab_id', str(uuid.uuid4()))


# Helper function for other handlers to get/create project state manager
def _get_or_create_project_state_manager(context: Dict[str, Any], control_channel) -> 'ProjectStateManager':
    """Get or create project state manager with debug setup."""
    logger.info("_get_or_create_project_state_manager called")
    logger.info("Context debug flag: %s", context.get("debug", False))
    
    if "project_state_manager" not in context:
        logger.info("Creating new ProjectStateManager")
        manager = ProjectStateManager(control_channel, context)
        
        # Set up debug mode if enabled
        if context.get("debug", False):
            debug_file_path = os.path.join(os.getcwd(), "project_state_debug.json")
            logger.info("Setting up debug mode with file: %s", debug_file_path)
            manager.set_debug_mode(True, debug_file_path)
        else:
            logger.info("Debug mode not enabled in context")
        
        context["project_state_manager"] = manager
        logger.info("Created and stored new manager")
        return manager
    else:
        logger.info("Returning existing project state manager")
        return context["project_state_manager"]


# Handler classes
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
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        logger.info("Got manager: %s", manager)
        
        # Find project state using client session - we need to find which project state this client session has
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            logger.error("No project state found for client session: %s", source_client_session)
            response = {
                "event": "project_state_folder_expand_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False
            }
            logger.info("Returning response: %s", response)
            return response
        
        logger.info("Found project state key: %s", project_state_key)
        
        logger.info("Calling manager.expand_folder...")
        success = await manager.expand_folder(project_state_key, folder_path)
        logger.info("expand_folder returned: %s", success)
        
        if success:
            # Send updated state
            logger.info("Sending project state update...")
            project_state = manager.projects[project_state_key]
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
        
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            return {
                "event": "project_state_folder_collapse_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False
            }
        
        success = await manager.collapse_folder(project_state_key, folder_path)
        
        if success:
            # Send updated state
            project_state = manager.projects[project_state_key]
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
        
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            return {
                "event": "project_state_file_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "success": False,
                "set_active": set_active
            }
        
        success = await manager.open_file(project_state_key, file_path, set_active)
        
        if success:
            # Send updated state
            project_state = manager.projects[project_state_key]
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
        
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            return {
                "event": "project_state_tab_close_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False
            }
        
        success = await manager.close_tab(project_state_key, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[project_state_key]
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
        
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            return {
                "event": "project_state_set_active_tab_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False
            }
        
        success = await manager.set_active_tab(project_state_key, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[project_state_key]
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
        
        manager = _get_or_create_project_state_manager(self.context, self.control_channel)
        
        # Find project state using client session
        project_state_key = None
        for key in manager.projects.keys():
            if key.startswith(source_client_session):
                project_state_key = key
                break
        
        if not project_state_key:
            return {
                "event": "project_state_diff_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "success": False,
                "error": "Project state not found"
            }
        
        success = await manager.open_diff_tab(
            project_state_key, file_path, from_ref, to_ref, from_hash, to_hash
        )
        
        if success:
            # Send updated state
            project_state = manager.projects[project_state_key]
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


