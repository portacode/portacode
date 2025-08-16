"""Data models for project state management.

This module contains all the dataclasses and models used throughout the project
state management system, including TabInfo, FileItem, GitFileChange, 
GitDetailedStatus, ProjectState, and MonitoredFolder.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set, Union


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
    
    # Content hash fields for caching optimization
    content_hash: Optional[str] = None  # SHA-256 hash of content
    original_content_hash: Optional[str] = None  # SHA-256 hash of original_content for diffs
    modified_content_hash: Optional[str] = None  # SHA-256 hash of modified_content for diffs
    html_diff_hash: Optional[str] = None  # SHA-256 hash of html_diff_versions JSON
    
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
    is_staged: Optional[Union[bool, str]] = None  # True, False, or "mixed"
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
    diff_details: Optional[Dict[str, Any]] = None  # Per-character diff information using diff-match-patch


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
    client_session_id: str  # The client session ID - one project per client session
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