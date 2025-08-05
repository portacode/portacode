"""Project state handlers for maintaining project folder structure and git metadata."""

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union, Tuple
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

# Import diff-match-patch with fallback
try:
    from diff_match_patch import diff_match_patch
    DIFF_MATCH_PATCH_AVAILABLE = True
except ImportError:
    DIFF_MATCH_PATCH_AVAILABLE = False
    diff_match_patch = None

# Import Pygments with fallback
try:
    from pygments import highlight
    from pygments.lexers import get_lexer_for_filename, get_lexer_by_name
    from pygments.formatters import HtmlFormatter
    from pygments.util import ClassNotFound
    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False
    highlight = None
    get_lexer_for_filename = None
    get_lexer_by_name = None
    HtmlFormatter = None
    ClassNotFound = Exception

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
    
    def _compute_diff_details(self, original_content: str, modified_content: str) -> Optional[Dict[str, Any]]:
        """Compute per-character diff details using diff-match-patch."""
        if not DIFF_MATCH_PATCH_AVAILABLE:
            logger.debug("diff-match-patch not available, skipping diff details computation")
            return None
        
        # Add performance safeguards to prevent blocking
        max_content_size = 50000  # 50KB max per file for diff details
        if len(original_content) > max_content_size or len(modified_content) > max_content_size:
            logger.debug("File too large for diff details computation")
            return None
        
        try:
            dmp = diff_match_patch()
            
            # Set timeout for diff computation
            dmp.Diff_Timeout = 1.0  # 1 second timeout
            
            # Compute the diff
            diffs = dmp.diff_main(original_content, modified_content)
            
            # Clean up the diff for efficiency
            dmp.diff_cleanupSemantic(diffs)
            
            # Convert the diff to a serializable format
            diff_data = []
            for operation, text in diffs:
                diff_data.append({
                    "operation": operation,  # -1 = delete, 0 = equal, 1 = insert
                    "text": text
                })
            
            # Also compute some useful statistics
            char_additions = sum(len(text) for op, text in diffs if op == 1)
            char_deletions = sum(len(text) for op, text in diffs if op == -1)
            char_unchanged = sum(len(text) for op, text in diffs if op == 0)
            
            return {
                "diffs": diff_data,
                "stats": {
                    "char_additions": char_additions,
                    "char_deletions": char_deletions,
                    "char_unchanged": char_unchanged,
                    "total_changes": char_additions + char_deletions
                },
                "algorithm": "diff-match-patch"
            }
            
        except Exception as e:
            logger.error("Error computing diff details: %s", e)
            return None
    
    def _get_pygments_lexer(self, file_path: str) -> Optional[object]:
        """Get Pygments lexer for a file path using built-in detection."""
        if not PYGMENTS_AVAILABLE:
            return None
        
        try:
            # Use Pygments' built-in filename detection
            lexer = get_lexer_for_filename(file_path)
            return lexer
        except ClassNotFound:
            # If no lexer found, return None (will fall back to plain text)
            logger.debug("No Pygments lexer found for file: %s", file_path)
            return None
        except Exception as e:
            logger.debug("Error getting Pygments lexer: %s", e)
            return None
    
    def _generate_html_diff(self, original_content: str, modified_content: str, file_path: str) -> Optional[Dict[str, str]]:
        """Generate unified HTML diff with intra-line highlighting. Returns both minimal and full context versions."""
        if not PYGMENTS_AVAILABLE:
            logger.debug("Pygments not available for HTML diff generation")
            return None
        
        # Add performance safeguards to prevent blocking
        max_content_size = 500000  # 500KB max per file (more reasonable)
        max_lines = 5000  # Max 5000 lines per file (more reasonable for real projects)
        
        original_line_count = original_content.count('\n')
        modified_line_count = modified_content.count('\n')
        max_line_count = max(original_line_count, modified_line_count)
        
        # Check if file is too large for full processing
        is_large_file = (len(original_content) > max_content_size or 
                        len(modified_content) > max_content_size or 
                        max_line_count > max_lines)
        
        if is_large_file:
            logger.warning(f"Large file detected for diff generation: {file_path} ({max_line_count} lines)")
            # Generate simplified diff without syntax highlighting for large files
            return self._generate_simple_diff_html(original_content, modified_content, file_path)
        
        try:
            import difflib
            import time
            
            start_time = time.time()
            timeout_seconds = 5  # 5 second timeout
            
            # Get line-based diff using Python's difflib (similar to git diff)
            original_lines = original_content.splitlines(keepends=True)
            modified_lines = modified_content.splitlines(keepends=True)
            
            # Check timeout
            if time.time() - start_time > timeout_seconds:
                logger.warning(f"Diff generation timeout for {file_path}")
                return None
            
            # Generate both minimal and full diff with performance safeguards
            minimal_diff_lines = list(difflib.unified_diff(
                original_lines, 
                modified_lines, 
                fromfile='a/' + os.path.basename(file_path),
                tofile='b/' + os.path.basename(file_path),
                lineterm='',
                n=3  # 3 lines of context (default)
            ))
            
            # Check timeout
            if time.time() - start_time > timeout_seconds:
                logger.warning(f"Diff generation timeout for {file_path}")
                return None
            
            # Generate full context diff only if file is small enough
            if len(original_lines) + len(modified_lines) < 2000:  # Increased threshold for better UX
                full_diff_lines = list(difflib.unified_diff(
                    original_lines, 
                    modified_lines, 
                    fromfile='a/' + os.path.basename(file_path),
                    tofile='b/' + os.path.basename(file_path),
                    lineterm='',
                    n=len(original_lines) + len(modified_lines)  # Show all lines
                ))
            else:
                full_diff_lines = minimal_diff_lines  # Use minimal for large files
            
            # Parse diffs (simplified but restored)
            minimal_parsed = self._parse_unified_diff_simple(minimal_diff_lines)
            full_parsed = self._parse_unified_diff_simple(full_diff_lines)
            
            # Check timeout
            if time.time() - start_time > timeout_seconds:
                logger.warning(f"Diff generation timeout for {file_path}")
                return None
            
            # Generate HTML for both versions
            minimal_html = self._generate_diff_html(minimal_parsed, file_path, 'minimal')
            full_html = self._generate_diff_html(full_parsed, file_path, 'full')
            
            return {
                'minimal': minimal_html,
                'full': full_html
            }
            
        except Exception as e:
            logger.error("Error generating HTML diff: %s", e)
            return None
    
    def _generate_simple_diff_html(self, original_content: str, modified_content: str, file_path: str) -> Dict[str, str]:
        """Generate simplified diff HTML for large files without syntax highlighting."""
        try:
            import difflib
            
            # Get line-based diff using Python's difflib
            original_lines = original_content.splitlines(keepends=True)
            modified_lines = modified_content.splitlines(keepends=True)
            
            # Generate minimal diff only for large files
            diff_lines = list(difflib.unified_diff(
                original_lines, 
                modified_lines, 
                fromfile='a/' + os.path.basename(file_path),
                tofile='b/' + os.path.basename(file_path),
                lineterm='',
                n=3  # Keep minimal context
            ))
            
            # Parse with simple parser (no syntax highlighting)
            parsed = self._parse_unified_diff_simple(diff_lines)
            
            # Limit to reasonable size for large files
            max_simple_diff_lines = 500
            if len(parsed) > max_simple_diff_lines:
                parsed = parsed[:max_simple_diff_lines]
                logger.info(f"Truncated large diff to {max_simple_diff_lines} lines for {file_path}")
            
            # Generate HTML without syntax highlighting but with good UI
            html = self._generate_simple_diff_html_content(parsed, file_path)
            
            return {
                'minimal': html,
                'full': html  # Same for both to keep UI consistent
            }
            
        except Exception as e:
            logger.error(f"Error generating simple diff HTML: {e}")
            return {
                'minimal': self._generate_fallback_diff_html(file_path),
                'full': self._generate_fallback_diff_html(file_path)
            }
    
    def _generate_simple_diff_html_content(self, parsed_diff: List[Dict], file_path: str) -> str:
        """Generate simple HTML diff content without syntax highlighting but with good UI."""
        html_parts = []
        html_parts.append('<div class="unified-diff-container" data-view-mode="minimal">')
        
        # Add stats header (no toggle for large files to keep it simple)
        line_additions = sum(1 for line in parsed_diff if line['type'] == 'add')
        line_deletions = sum(1 for line in parsed_diff if line['type'] == 'delete')
        
        html_parts.append(f'''
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="additions">+{line_additions}</span>
                    <span class="deletions">-{line_deletions}</span>
                    <span class="file-path">{os.path.basename(file_path)} (Large file - simplified view)</span>
                </div>
            </div>
        ''')
        
        # Generate content without syntax highlighting
        html_parts.append('<div class="diff-content">')
        html_parts.append('<table class="diff-table">')
        
        for line_info in parsed_diff:
            if line_info['type'] == 'header':
                continue  # Skip headers
                
            line_type = line_info['type']
            old_line_num = line_info.get('old_line_num', '')
            new_line_num = line_info.get('new_line_num', '')
            content = line_info['content']
            
            # Simple HTML escaping without syntax highlighting
            escaped_content = self._escape_html(content)
            
            row_class = f'diff-line diff-{line_type}'
            html_parts.append(f'''
                <tr class="{row_class}">
                    <td class="line-num old-line-num">{old_line_num}</td>
                    <td class="line-num new-line-num">{new_line_num}</td>
                    <td class="line-content">{escaped_content}</td>
                </tr>
            ''')
        
        html_parts.append('</table>')
        html_parts.append('</div>')
        html_parts.append('</div>')
        
        return ''.join(html_parts)
    
    def _generate_fallback_diff_html(self, file_path: str) -> str:
        """Generate minimal fallback HTML when all else fails."""
        return f'''
        <div class="unified-diff-container" data-view-mode="minimal">
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="file-path">{os.path.basename(file_path)} (Diff unavailable)</span>
                </div>
            </div>
            <div class="diff-content">
                <div style="padding: 2rem; text-align: center; color: var(--text-secondary);">
                    <i class="fas fa-exclamation-triangle" style="font-size: 2rem; margin-bottom: 1rem;"></i>
                    <p>Diff view unavailable for this file</p>
                    <p style="font-size: 0.9rem;">File may be too large or binary</p>
                </div>
            </div>
        </div>
        '''
    
    def _parse_unified_diff_simple(self, diff_lines):
        """Simple unified diff parser without intra-line highlighting for better performance."""
        parsed = []
        old_line_num = 0
        new_line_num = 0
        
        for line in diff_lines:
            if line.startswith('@@'):
                # Parse hunk header to get line numbers
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1
                
                parsed.append({
                    'type': 'header',
                    'content': line,
                    'old_line_num': '',
                    'new_line_num': ''
                })
            elif line.startswith('---') or line.startswith('+++'):
                # Skip diff file headers (--- a/file, +++ b/file)
                parsed.append({
                    'type': 'header',
                    'content': line,
                    'old_line_num': '',
                    'new_line_num': ''
                })
            elif line.startswith('-'):
                old_line_num += 1
                parsed.append({
                    'type': 'delete',
                    'old_line_num': old_line_num,
                    'new_line_num': '',
                    'content': line
                })
            elif line.startswith('+'):
                new_line_num += 1
                parsed.append({
                    'type': 'add',
                    'old_line_num': '',
                    'new_line_num': new_line_num,
                    'content': line
                })
            elif line.startswith(' '):
                old_line_num += 1
                new_line_num += 1
                parsed.append({
                    'type': 'context',
                    'old_line_num': old_line_num,
                    'new_line_num': new_line_num,
                    'content': line
                })
        
        return parsed
    
    def _generate_diff_html(self, parsed_diff: List[Dict], file_path: str, view_mode: str) -> str:
        """Generate HTML for a parsed diff."""
        # Limit diff size to prevent performance issues
        max_diff_lines = 1000  # Increased limit for better UX
        if len(parsed_diff) > max_diff_lines:
            logger.warning(f"Diff too large, truncating: {file_path} ({len(parsed_diff)} lines)")
            parsed_diff = parsed_diff[:max_diff_lines]
        
        # Get Pygments lexer for syntax highlighting
        lexer = self._get_pygments_lexer(file_path)
        
        # Pre-highlight all unique lines for better context-aware syntax highlighting
        unique_lines = set()
        for line_info in parsed_diff:
            if line_info['type'] != 'header' and 'content' in line_info:
                content = line_info['content']
                if content and content[0] in '+- ':
                    clean_line = content[1:].rstrip('\n')
                    if clean_line.strip():
                        unique_lines.add(clean_line)
        
        # Pre-highlight all unique lines as a batch for better context
        highlighted_cache = {}
        if lexer and unique_lines:
            try:
                # Combine all lines to give Pygments better context
                combined_content = '\n'.join(unique_lines)
                combined_highlighted = highlight(combined_content, lexer, HtmlFormatter(nowrap=True, noclasses=False, style='monokai'))
                
                # Split back into individual lines
                highlighted_lines = combined_highlighted.split('\n')
                unique_lines_list = list(unique_lines)
                
                for i, line in enumerate(unique_lines_list):
                    if i < len(highlighted_lines):
                        highlighted_cache[line] = highlighted_lines[i]
            except Exception as e:
                logger.debug(f"Error in batch syntax highlighting: {e}")
                highlighted_cache = {}
        
        # Build HTML
        html_parts = []
        html_parts.append(f'<div class="unified-diff-container" data-view-mode="{view_mode}">')
        
        # Add stats header with toggle
        line_additions = sum(1 for line in parsed_diff if line['type'] == 'add')
        line_deletions = sum(1 for line in parsed_diff if line['type'] == 'delete')
        
        html_parts.append(f'''
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="additions">+{line_additions}</span>
                    <span class="deletions">-{line_deletions}</span>
                    <span class="file-path">{os.path.basename(file_path)}</span>
                </div>
                <div class="diff-stats-right">
                    <button class="diff-toggle-btn" data-current-mode="{view_mode}">
                        <i class="fas fa-eye"></i>
                        <span class="toggle-text"></span>
                    </button>
                </div>
            </div>
        ''')
        
        # Generate unified diff view
        html_parts.append('<div class="diff-content">')
        html_parts.append('<table class="diff-table">')
        
        for line_info in parsed_diff:
            if line_info['type'] == 'header':
                continue  # Skip all diff headers including --- and +++ lines
                
            line_type = line_info['type']
            old_line_num = line_info.get('old_line_num', '')
            new_line_num = line_info.get('new_line_num', '')
            content = line_info['content']
            
            # Apply syntax highlighting using pre-highlighted cache for better accuracy
            if content and content[0] in '+- ':
                prefix = content[0] if content[0] in '+-' else ' '
                clean_content = content[1:].rstrip('\n')
                
                # Use pre-highlighted cache if available
                if clean_content.strip() and clean_content in highlighted_cache:
                    final_content = prefix + highlighted_cache[clean_content]
                elif clean_content.strip():
                    # Fallback to individual line highlighting
                    try:
                        highlighted = highlight(clean_content, lexer, HtmlFormatter(nowrap=True, noclasses=False, style='monokai'))
                        final_content = prefix + highlighted
                    except Exception as e:
                        logger.debug("Error applying syntax highlighting: %s", e)
                        final_content = self._escape_html(content)
                else:
                    final_content = self._escape_html(content)
            else:
                final_content = self._escape_html(content)
            
            # CSS classes for different line types
            row_class = f'diff-line diff-{line_type}'
            
            html_parts.append(f'''
                <tr class="{row_class}">
                    <td class="line-num old-line-num">{old_line_num}</td>
                    <td class="line-num new-line-num">{new_line_num}</td>
                    <td class="line-content">{final_content}</td>
                </tr>
            ''')
        
        html_parts.append('</table>')
        html_parts.append('</div>')
        html_parts.append('</div>')
        
        return ''.join(html_parts)
    
    def _parse_unified_diff_with_intraline(self, diff_lines, original_lines, modified_lines):
        """Parse unified diff and add intra-line character highlighting."""
        parsed = []
        old_line_num = 0
        new_line_num = 0
        
        pending_deletes = []
        pending_adds = []
        
        def flush_pending():
            """Process pending delete/add pairs for intra-line highlighting."""
            if pending_deletes and pending_adds:
                # Apply intra-line highlighting to delete/add pairs
                for i, (del_line, add_line) in enumerate(zip(pending_deletes, pending_adds)):
                    del_content = del_line['content'][1:]  # Remove '-' prefix
                    add_content = add_line['content'][1:]  # Remove '+' prefix
                    
                    del_highlighted, add_highlighted = self._generate_intraline_diff(del_content, add_content)
                    
                    # Update the parsed lines with intra-line highlighting
                    del_line['intraline_html'] = '-' + del_highlighted
                    add_line['intraline_html'] = '+' + add_highlighted
                    
                    parsed.append(del_line)
                    parsed.append(add_line)
                
                # Handle remaining unmatched deletes/adds
                for del_line in pending_deletes[len(pending_adds):]:
                    parsed.append(del_line)
                for add_line in pending_adds[len(pending_deletes):]:
                    parsed.append(add_line)
            else:
                # No pairs to highlight, just add them as-is
                parsed.extend(pending_deletes)
                parsed.extend(pending_adds)
            
            pending_deletes.clear()
            pending_adds.clear()
        
        for line in diff_lines:
            if line.startswith('@@'):
                # Flush any pending changes before hunk header
                flush_pending()
                
                # Parse hunk header to get line numbers
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1
                
                parsed.append({
                    'type': 'header',
                    'content': line,
                    'old_line_num': '',
                    'new_line_num': ''
                })
            elif line.startswith('---') or line.startswith('+++'):
                # Skip diff file headers (--- a/file, +++ b/file)
                parsed.append({
                    'type': 'header',
                    'content': line,
                    'old_line_num': '',
                    'new_line_num': ''
                })
            elif line.startswith('-'):
                pending_deletes.append({
                    'type': 'delete',
                    'old_line_num': old_line_num + 1,
                    'new_line_num': '',
                    'content': line
                })
                old_line_num += 1
            elif line.startswith('+'):
                pending_adds.append({
                    'type': 'add',
                    'old_line_num': '',
                    'new_line_num': new_line_num + 1,
                    'content': line
                })
                new_line_num += 1
            elif line.startswith(' '):
                # Flush pending changes before context line
                flush_pending()
                
                old_line_num += 1
                new_line_num += 1
                parsed.append({
                    'type': 'context',
                    'old_line_num': old_line_num,
                    'new_line_num': new_line_num,
                    'content': line
                })
            elif line.startswith('---') or line.startswith('+++'):
                parsed.append({
                    'type': 'header',
                    'content': line,
                    'old_line_num': '',
                    'new_line_num': ''
                })
        
        # Flush any remaining pending changes
        flush_pending()
        
        return parsed
    
    def _generate_intraline_diff(self, old_text: str, new_text: str) -> Tuple[str, str]:
        """Generate intra-line character-level diff highlighting."""
        # Temporarily disable intraline highlighting to fix performance issues
        return self._escape_html(old_text), self._escape_html(new_text)
        
        if not DIFF_MATCH_PATCH_AVAILABLE:
            return self._escape_html(old_text), self._escape_html(new_text)
        
        try:
            dmp = diff_match_patch()
            diffs = dmp.diff_main(old_text, new_text)
            dmp.diff_cleanupSemantic(diffs)
            
            old_parts = []
            new_parts = []
            
            for op, text in diffs:
                escaped_text = self._escape_html(text)
                
                if op == 0:  # EQUAL
                    old_parts.append(escaped_text)
                    new_parts.append(escaped_text)
                elif op == -1:  # DELETE
                    old_parts.append(f'<span class="intraline-delete">{escaped_text}</span>')
                elif op == 1:  # INSERT
                    new_parts.append(f'<span class="intraline-add">{escaped_text}</span>')
            
            return ''.join(old_parts), ''.join(new_parts)
            
        except Exception as e:
            logger.debug("Error generating intra-line diff: %s", e)
            return self._escape_html(old_text), self._escape_html(new_text)
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return (text.replace('&', '&amp;')
                   .replace('<', '&lt;')
                   .replace('>', '&gt;')
                   .replace('"', '&quot;')
                   .replace("'", '&#x27;'))
    
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
                    diff_details = None  # No diff for deleted files
                elif diff_item.new_file:
                    change_type = 'added'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # For new files, compare empty content vs current staged content
                    if content_hash:
                        staged_content = self.get_file_content_staged(file_abs_path) or ""
                        diff_details = self._compute_diff_details("", staged_content)
                    else:
                        diff_details = None
                else:
                    # For modified files (including renames that git detected)
                    change_type = 'modified'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # Compare HEAD content vs staged content
                    head_content = self.get_file_content_at_commit(file_abs_path) or ""
                    staged_content = self.get_file_content_staged(file_abs_path) or ""
                    diff_details = self._compute_diff_details(head_content, staged_content)
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type=change_type,
                    content_hash=content_hash,
                    is_staged=True,
                    diff_details=diff_details
                )
                logger.debug("Created staged change for: %s (%s)", file_name, change_type)
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
                    diff_details = None  # No diff for deleted files
                elif diff_item.new_file:
                    change_type = 'added'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # For new files, compare empty content vs current working content
                    if content_hash and os.path.exists(file_abs_path):
                        try:
                            with open(file_abs_path, 'r', encoding='utf-8') as f:
                                working_content = f.read()
                            diff_details = self._compute_diff_details("", working_content)
                        except (OSError, UnicodeDecodeError):
                            diff_details = None
                    else:
                        diff_details = None
                else:
                    change_type = 'modified'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # Compare staged/index content vs working content
                    staged_content = self.get_file_content_staged(file_abs_path) or ""
                    if os.path.exists(file_abs_path):
                        try:
                            with open(file_abs_path, 'r', encoding='utf-8') as f:
                                working_content = f.read()
                            diff_details = self._compute_diff_details(staged_content, working_content)
                        except (OSError, UnicodeDecodeError):
                            diff_details = None
                    else:
                        diff_details = None
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type=change_type,
                    content_hash=content_hash,
                    is_staged=False,
                    diff_details=diff_details
                )
                logger.debug("Created unstaged change for: %s (%s)", file_name, change_type)
                detailed_status.unstaged_changes.append(change)
            
            # Get untracked files
            untracked_files = self.repo.untracked_files
            for file_repo_path in untracked_files:
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                
                # For untracked files, compare empty content vs current file content
                diff_details = None
                if content_hash and os.path.exists(file_abs_path):
                    try:
                        with open(file_abs_path, 'r', encoding='utf-8') as f:
                            working_content = f.read()
                        diff_details = self._compute_diff_details("", working_content)
                    except (OSError, UnicodeDecodeError):
                        diff_details = None
                
                change = GitFileChange(
                    file_repo_path=file_repo_path,
                    file_name=file_name,
                    file_abs_path=file_abs_path,
                    change_type='untracked',
                    content_hash=content_hash,
                    is_staged=False,
                    diff_details=diff_details
                )
                logger.debug("Created untracked change for: %s", file_name)
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
                            logger.debug("Git status change detected: %s - %s", event.event_type, os.path.basename(event.src_path))
                        else:
                            return  # Skip other .git files
                    except (ValueError, IndexError):
                        return  # Skip if can't parse .git path
                else:
                    # Only log significant file changes, not every single event
                    if event.event_type in ['created', 'deleted'] or event.src_path.endswith(('.py', '.js', '.html', '.css', '.json', '.md')):
                        logger.debug("File system event: %s - %s", event.event_type, os.path.basename(event.src_path))
                    else:
                        logger.debug("File event: %s", os.path.basename(event.src_path))
                
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
        return asdict(tab) if hasattr(tab, '__dataclass_fields__') else {}
    
    async def initialize_project_state(self, client_session_id: str, project_folder_path: str) -> ProjectState:
        """Initialize project state for a client session."""
        # Check if this client session already has a project state
        if client_session_id in self.projects:
            existing_project = self.projects[client_session_id]
            # If it's the same folder, return existing state
            if existing_project.project_folder_path == project_folder_path:
                logger.info("Returning existing project state for client session: %s", client_session_id)
                return existing_project
            else:
                # Different folder - cleanup old state and create new one
                logger.info("Client session %s switching projects from %s to %s", 
                          client_session_id, existing_project.project_folder_path, project_folder_path)
                self.cleanup_project(client_session_id)
        
        # Note: Multiple client sessions can have independent project states for the same folder
        # Each client session gets its own project state instance
        
        logger.info("Initializing project state for client session: %s, folder: %s", client_session_id, project_folder_path)
        
        # Initialize Git manager
        git_manager = GitManager(project_folder_path)
        self.git_managers[client_session_id] = git_manager
        
        # Create project state
        project_state = ProjectState(
            client_session_id=client_session_id,
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
        
        self.projects[client_session_id] = project_state
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
        git_manager = self.git_managers.get(project_state.client_session_id)
        
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
            logger.info(f"About to create tab for file: {file_path}")
            new_tab = await tab_factory.create_file_tab(file_path)
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
                file_path, original_content, modified_content, diff_title, 
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
        logger.debug("Processing file change: %s - %s", event.event_type, event.src_path)
        
        self._pending_changes.add(event.src_path)
        
        # Cancel existing timer
        if self._change_debounce_timer and not self._change_debounce_timer.done():
            self._change_debounce_timer.cancel()
        
        # Set new timer with proper exception handling
        async def debounced_process():
            try:
                await asyncio.sleep(0.5)  # Debounce delay
                await self._process_pending_changes()
            except asyncio.CancelledError:
                logger.debug("Debounce timer cancelled")
            except Exception as e:
                logger.error("Error in debounced file processing: %s", e)
        
        self._change_debounce_timer = asyncio.create_task(debounced_process())
    
    async def _process_pending_changes(self):
        """Process pending file changes."""
        if not self._pending_changes:
            logger.debug("No pending changes to process")
            return
        
        logger.debug("Processing %d pending file changes", len(self._pending_changes))
        
        logger.debug("Processing changes in %d files", len(self._pending_changes))
        
        # Process changes for each affected project
        affected_projects = set()
        for change_path in self._pending_changes:
            for client_session_id, project_state in self.projects.items():
                if change_path.startswith(project_state.project_folder_path):
                    affected_projects.add(client_session_id)
        
        if affected_projects:
            logger.debug("Refreshing %d affected projects", len(affected_projects))
        else:
            logger.debug("No affected projects to refresh")
        
        # Refresh affected projects
        for client_session_id in affected_projects:
            await self._refresh_project_state(client_session_id)
        
        self._pending_changes.clear()
        logger.debug("Finished processing file changes")
    
    async def _refresh_project_state(self, client_session_id: str):
        """Refresh project state after file changes."""
        if client_session_id not in self.projects:
            return
        
        project_state = self.projects[client_session_id]
        git_manager = self.git_managers[client_session_id]
        
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
        await self.control_channel.send(payload)
    
    def cleanup_project(self, client_session_id: str):
        """Clean up project state and resources."""
        if client_session_id in self.projects:
            project_state = self.projects[client_session_id]
            
            # Stop watching all monitored folders for this project
            for monitored_folder in project_state.monitored_folders:
                self.file_watcher.stop_watching(monitored_folder.folder_path)
            
            # Stop watching .git directory if it was being monitored
            if project_state.is_git_repo:
                git_dir_path = os.path.join(project_state.project_folder_path, '.git')
                self.file_watcher.stop_watching(git_dir_path)
            
            # Clean up managers
            self.git_managers.pop(client_session_id, None)
            self.projects.pop(client_session_id, None)
            
            logger.info("Cleaned up project state: %s", client_session_id)
            self._write_debug_state()
    
    def cleanup_projects_by_client_session(self, client_session_id: str):
        """Clean up project state for a specific client session when explicitly notified of disconnection."""
        logger.info("Explicitly cleaning up project state for disconnected client session: %s", client_session_id)
        
        # With the new design, each client session has only one project
        if client_session_id in self.projects:
            self.cleanup_project(client_session_id)
            logger.info("Cleaned up project state for client session: %s", client_session_id)
        else:
            logger.info("No project state found for client session: %s", client_session_id)
    
    def cleanup_all_projects(self):
        """Clean up all project states. Used for shutdown or reset."""
        logger.info("Cleaning up all project states")
        
        client_session_ids = list(self.projects.keys())
        for client_session_id in client_session_ids:
            self.cleanup_project(client_session_id)
        
        logger.info("Cleaned up %d project states", len(client_session_ids))
    
    def cleanup_orphaned_project_states(self, current_client_sessions: List[str]):
        """Clean up project states that don't match any current client session."""
        current_sessions_set = set(current_client_sessions)
        orphaned_keys = []
        
        for session_id in list(self.projects.keys()):
            if session_id not in current_sessions_set:
                orphaned_keys.append(session_id)
        
        if orphaned_keys:
            logger.info("Found %d orphaned project states, cleaning up: %s", len(orphaned_keys), orphaned_keys)
            for session_id in orphaned_keys:
                self.cleanup_project(session_id)
            logger.info("Cleaned up %d orphaned project states", len(orphaned_keys))
        else:
            logger.debug("No orphaned project states found")


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


# Global singleton instance
_global_project_state_manager: Optional['ProjectStateManager'] = None
_manager_lock = threading.Lock()

# Helper function for other handlers to get/create project state manager
def _get_or_create_project_state_manager(context: Dict[str, Any], control_channel) -> 'ProjectStateManager':
    """Get or create project state manager with debug setup (SINGLETON PATTERN)."""
    global _global_project_state_manager
    
    logger.info("_get_or_create_project_state_manager called")
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


def _reset_global_project_state_manager():
    """Reset the global project state manager (for testing/cleanup)."""
    global _global_project_state_manager
    with _manager_lock:
        if _global_project_state_manager:
            logger.info("Resetting global project state manager")
            _global_project_state_manager = None
        else:
            logger.debug("Global project state manager already None")


def _debug_global_manager_state():
    """Debug function to log the current state of the global manager."""
    global _global_project_state_manager
    with _manager_lock:
        if _global_project_state_manager:
            logger.info("Global ProjectStateManager exists (PID: %s)", os.getpid())
            logger.info("Active project states: %s", list(_global_project_state_manager.projects.keys()))
            logger.info("Total project states: %d", len(_global_project_state_manager.projects))
        else:
            logger.info("No global ProjectStateManager exists (PID: %s)", os.getpid())


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
        
        # With the new design, client session ID maps directly to project state
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            response = {
                "event": "project_state_folder_expand_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
            logger.error("Returning error response: %s", response)
            return response
        
        logger.info("Found project state for client session: %s", source_client_session)
        
        logger.info("Calling manager.expand_folder...")
        success = await manager.expand_folder(source_client_session, folder_path)
        logger.info("expand_folder returned: %s", success)
        
        if success:
            # Send updated state
            logger.info("Sending project state update...")
            project_state = manager.projects[source_client_session]
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
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_folder_collapse_response",
                "project_id": server_project_id,
                "folder_path": folder_path,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.collapse_folder(source_client_session, folder_path)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
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
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_file_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "success": False,
                "set_active": set_active,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.open_file(source_client_session, file_path, set_active)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
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
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_tab_close_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.close_tab(source_client_session, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
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
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_set_active_tab_response",
                "project_id": server_project_id,
                "tab_id": tab_id,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.set_active_tab(source_client_session, tab_id)
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
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
        if source_client_session not in manager.projects:
            logger.error("No project state found for client session: %s. Available project states: %s", 
                        source_client_session, list(manager.projects.keys()))
            return {
                "event": "project_state_diff_open_response",
                "project_id": server_project_id,
                "file_path": file_path,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "success": False,
                "error": f"No project state found for client session: {source_client_session}"
            }
        
        success = await manager.open_diff_tab(
            source_client_session, file_path, from_ref, to_ref, from_hash, to_hash
        )
        
        if success:
            # Send updated state
            project_state = manager.projects[source_client_session]
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


# Handler for explicit client session cleanup
async def handle_client_session_cleanup(handler, payload: Dict[str, Any], source_client_session: str) -> Dict[str, Any]:
    """Handle explicit cleanup of a client session when server notifies of permanent disconnection."""
    client_session_id = payload.get('client_session_id')
    
    if not client_session_id:
        logger.error("client_session_id is required for client session cleanup")
        return {
            "event": "client_session_cleanup_response",
            "success": False,
            "error": "client_session_id is required"
        }
    
    logger.info("Handling explicit cleanup for client session: %s", client_session_id)
    
    # Get the project state manager
    manager = _get_or_create_project_state_manager(handler.context, handler.control_channel)
    
    # Clean up the client session's project state
    manager.cleanup_projects_by_client_session(client_session_id)
    
    logger.info("Client session cleanup completed: %s", client_session_id)
    
    return {
        "event": "client_session_cleanup_response",
        "client_session_id": client_session_id,
        "success": True
    }


