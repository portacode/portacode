"""Git management functionality for project state.

This module provides the GitManager class which handles all Git-related operations
including status checking, diff generation, file content retrieval, and Git commands
like staging, unstaging, and reverting files.
"""

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .models import GitDetailedStatus, GitFileChange

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


class GitManager:
    """Manages Git operations for project state."""
    
    def __init__(self, project_path: str, change_callback: Optional[Callable] = None):
        self.project_path = project_path
        self.repo: Optional[Repo] = None
        self.is_git_repo = False
        self._change_callback = change_callback
        
        # Periodic monitoring attributes
        self._monitoring_task: Optional[asyncio.Task] = None
        self._cached_status_summary: Optional[Dict[str, int]] = None
        self._cached_detailed_status: Optional[GitDetailedStatus] = None
        self._cached_branch: Optional[str] = None
        self._monitoring_enabled = False
        
        self._initialize_repo()
        
        # Start monitoring if this is a git repo
        if self.is_git_repo and change_callback:
            self.start_periodic_monitoring()
    
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
    
    def reinitialize(self):
        """Reinitialize git repo detection (useful when .git directory is created after initialization)."""
        logger.info("Reinitializing git repo detection for: %s", self.project_path)
        
        # Stop existing monitoring
        self.stop_periodic_monitoring()
        
        self.repo = None
        self.is_git_repo = False
        self._initialize_repo()
        
        # Restart monitoring if this is now a git repo and we have a callback
        if self.is_git_repo and self._change_callback:
            self.start_periodic_monitoring()
    
    def get_branch_name(self) -> Optional[str]:
        """Get current Git branch name."""
        if not self.is_git_repo or not self.repo:
            return None
        
        try:
            return self.repo.active_branch.name
        except Exception as e:
            logger.debug("Could not get Git branch: %s", e)
            return None
    
    def _get_staging_status(self, file_path: str, rel_path: str) -> Union[bool, str]:
        """Get staging status for a file or directory. Returns True, False, or 'mixed'."""
        try:
            if os.path.isdir(file_path):
                # For directories, check all files within the directory
                try:
                    # Get all staged files
                    staged_files = set(self.repo.git.diff('--cached', '--name-only').splitlines())
                    # Get all files with unstaged changes
                    unstaged_files = set(self.repo.git.diff('--name-only').splitlines())
                    
                    # Find files within this directory
                    dir_staged_files = [f for f in staged_files if f.startswith(rel_path + '/') or f == rel_path]
                    dir_unstaged_files = [f for f in unstaged_files if f.startswith(rel_path + '/') or f == rel_path]
                    
                    has_staged = len(dir_staged_files) > 0
                    has_unstaged = len(dir_unstaged_files) > 0
                    
                    # Check for mixed staging within individual files in this directory
                    has_mixed_files = False
                    for staged_file in dir_staged_files:
                        if staged_file in dir_unstaged_files:
                            has_mixed_files = True
                            break
                    
                    if has_mixed_files or (has_staged and has_unstaged):
                        return "mixed"
                    elif has_staged:
                        return True
                    else:
                        return False
                        
                except Exception:
                    return False
            else:
                # For individual files
                try:
                    # Check if file has staged changes
                    staged_diff = self.repo.git.diff('--cached', '--name-only', rel_path)
                    has_staged = bool(staged_diff.strip())
                    
                    if has_staged:
                        # Check if also has unstaged changes (mixed scenario)
                        unstaged_diff = self.repo.git.diff('--name-only', rel_path)
                        has_unstaged = bool(unstaged_diff.strip())
                        return "mixed" if has_unstaged else True
                    return False
                except Exception:
                    return False
        except Exception:
            return False
    
    def get_file_status(self, file_path: str) -> Dict[str, Any]:
        """Get Git status for a specific file or directory."""
        if not self.is_git_repo or not self.repo:
            return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
        
        try:
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Get staging status for files and directories
            is_staged = self._get_staging_status(file_path, rel_path)
            
            # Check if ignored - GitPython handles path normalization internally
            is_ignored = self.repo.ignored(rel_path)
            if is_ignored:
                return {"is_tracked": False, "status": "ignored", "is_ignored": True, "is_staged": False}
            
            # For directories, aggregate status from contained files
            if os.path.isdir(file_path):
                # Normalize the relative path for cross-platform compatibility
                rel_path_normalized = rel_path.replace('\\', '/')
                
                # Check for untracked files in this directory
                has_untracked = False
                for untracked_file in self.repo.untracked_files:
                    untracked_normalized = untracked_file.replace('\\', '/')
                    if untracked_normalized.startswith(rel_path_normalized + '/') or untracked_normalized == rel_path_normalized:
                        has_untracked = True
                        break
                
                # Check for modified files in this directory using git status
                has_modified = False
                has_deleted = False
                try:
                    # Get status for files in this directory
                    status_output = self.repo.git.status(rel_path, porcelain=True)
                    if status_output.strip():
                        for line in status_output.strip().split('\n'):
                            if len(line) >= 2:
                                # When filtering git status by path, GitPython strips the leading space
                                # So format is either "XY filename" or " XY filename"  
                                if line.startswith(' '):
                                    # Full status format: " XY filename"
                                    index_status = line[0]
                                    worktree_status = line[1] 
                                    file_path_from_status = line[3:] if len(line) > 3 else ""
                                else:
                                    # Path-filtered format: "XY filename" (leading space stripped)
                                    # Two possible formats:
                                    # 1. Regular files: "M  filename" (index + worktree + space + filename)
                                    # 2. Submodules: "M filename" (index + space + filename)
                                    index_status = line[0] if len(line) > 0 else ' '
                                    worktree_status = line[1] if len(line) > 1 else ' '
                                    
                                    # Detect format by checking if position 2 is a space
                                    if len(line) > 2 and line[2] == ' ':
                                        # Regular file format: "M  filename"
                                        file_path_from_status = line[3:] if len(line) > 3 else ""
                                    else:
                                        # Submodule format: "M filename" 
                                        file_path_from_status = line[2:] if len(line) > 2 else ""
                                
                                # Check if this file is within our directory
                                file_normalized = file_path_from_status.replace('\\', '/')
                                if (file_normalized.startswith(rel_path_normalized + '/') or 
                                    file_normalized == rel_path_normalized):
                                    if index_status in ['M', 'A', 'R', 'C'] or worktree_status in ['M', 'A', 'R', 'C']:
                                        has_modified = True
                                    elif index_status == 'D' or worktree_status == 'D':
                                        has_deleted = True
                except Exception as e:
                    logger.debug("Error checking directory git status for %s: %s", rel_path, e)
                
                # Priority order: untracked > modified/deleted > clean
                if has_untracked:
                    return {"is_tracked": False, "status": "untracked", "is_ignored": False, "is_staged": is_staged}
                elif has_deleted:
                    return {"is_tracked": True, "status": "deleted", "is_ignored": False, "is_staged": is_staged}
                elif has_modified:
                    return {"is_tracked": True, "status": "modified", "is_ignored": False, "is_staged": is_staged}
                
                # Check if directory has tracked files to determine if it should show as clean
                try:
                    tracked_files = self.repo.git.ls_files(rel_path)
                    is_tracked = bool(tracked_files.strip())
                    status = "clean" if is_tracked else None
                    return {"is_tracked": is_tracked, "status": status, "is_ignored": False, "is_staged": is_staged}
                except Exception:
                    return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
            
            # For files
            else:
                # Check if untracked - direct comparison works cross-platform
                if rel_path in self.repo.untracked_files:
                    return {"is_tracked": False, "status": "untracked", "is_ignored": False, "is_staged": is_staged}
                
                # If file is staged, we need to determine its original status
                if is_staged:
                    # Check if this was originally an untracked file that got staged
                    # We need to check if the file existed in HEAD, not just in the index
                    try:
                        # Try to see if file existed in HEAD (was tracked before staging)
                        self.repo.git.show(f"HEAD:{rel_path}")
                        # If we get here, file existed in HEAD, so it was modified and staged
                        is_tracked = True
                        original_status = "modified"
                    except Exception:
                        # File didn't exist in HEAD, so it was untracked when staged (new file)
                        is_tracked = False
                        original_status = "added"
                    
                    return {"is_tracked": is_tracked, "status": original_status, "is_ignored": False, "is_staged": is_staged}
                
                # Check if tracked and dirty - GitPython handles path normalization
                if self.repo.is_dirty(path=rel_path):
                    return {"is_tracked": True, "status": "modified", "is_ignored": False, "is_staged": is_staged}
                
                # Check if tracked and clean - GitPython handles paths
                try:
                    self.repo.git.ls_files(rel_path, error_unmatch=True)
                    return {"is_tracked": True, "status": "clean", "is_ignored": False, "is_staged": is_staged}
                except Exception:
                    return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
                    
        except Exception as e:
            logger.debug("Error getting Git status for %s: %s", file_path, e)
            return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

    def get_file_status_batch(self, file_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get Git status for multiple files/directories at once (optimized batch operation).

        Args:
            file_paths: List of absolute file paths

        Returns:
            Dict mapping file_path to status dict: {"is_tracked": bool, "status": str, "is_ignored": bool, "is_staged": bool|"mixed"}
        """
        if not self.is_git_repo or not self.repo:
            # Return empty status for all paths
            return {path: {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}
                    for path in file_paths}

        result = {}

        try:
            # Convert all paths to relative paths
            rel_paths_map = {}  # abs_path -> rel_path
            for file_path in file_paths:
                try:
                    rel_path = os.path.relpath(file_path, self.repo.working_dir)
                    rel_paths_map[file_path] = rel_path
                except Exception as e:
                    logger.debug("Error converting path %s to relative: %s", file_path, e)
                    result[file_path] = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

            rel_paths = list(rel_paths_map.values())

            # BATCH OPERATION 1: Get all ignored paths at once
            ignored_paths = set()
            try:
                ignored_list = self.repo.ignored(*rel_paths)
                ignored_paths = set(ignored_list) if ignored_list else set()
            except Exception as e:
                logger.debug("Error checking ignored status for batch: %s", e)

            # BATCH OPERATION 2: Get global git data once
            untracked_files = set(self.repo.untracked_files)

            try:
                staged_files_output = self.repo.git.diff('--cached', '--name-only')
                staged_files = set(staged_files_output.splitlines()) if staged_files_output.strip() else set()
            except Exception:
                staged_files = set()

            try:
                unstaged_files_output = self.repo.git.diff('--name-only')
                unstaged_files = set(unstaged_files_output.splitlines()) if unstaged_files_output.strip() else set()
            except Exception:
                unstaged_files = set()

            # BATCH OPERATION 3: Get status for all paths at once
            status_map = {}  # rel_path -> status_line
            try:
                status_output = self.repo.git.status(*rel_paths, porcelain=True)
                if status_output.strip():
                    for line in status_output.strip().split('\n'):
                        # Git porcelain format: XY path (X=index, Y=worktree, then space, then path)
                        # Some files may have renamed format: XY path -> new_path
                        if len(line) >= 3:
                            # Skip first 3 characters (2 status + 1 space) to get the file path
                            # But git uses exactly 2 chars for status then space, so position 3 onwards is path
                            parts = line.split(None, 1)  # Split on first whitespace to separate status from path
                            if len(parts) >= 2:
                                file_path_from_status = parts[1]
                                # Handle renames (format: "old_path -> new_path")
                                if ' -> ' in file_path_from_status:
                                    file_path_from_status = file_path_from_status.split(' -> ')[1]
                                status_map[file_path_from_status] = line
            except Exception as e:
                logger.debug("Error getting batch status: %s", e)

            # BATCH OPERATION 4: Get all tracked files
            try:
                tracked_files_output = self.repo.git.ls_files()
                tracked_files = set(tracked_files_output.splitlines()) if tracked_files_output.strip() else set()
            except Exception:
                tracked_files = set()

            # Process each file with the batch data
            for file_path, rel_path in rel_paths_map.items():
                try:
                    # Check if ignored
                    if rel_path in ignored_paths:
                        result[file_path] = {"is_tracked": False, "status": "ignored", "is_ignored": True, "is_staged": False}
                        continue

                    # Determine staging status
                    is_staged = self._get_staging_status_from_batch(
                        file_path, rel_path, staged_files, unstaged_files
                    )

                    # Handle directories
                    if os.path.isdir(file_path):
                        result[file_path] = self._get_directory_status_from_batch(
                            file_path, rel_path, untracked_files, status_map, tracked_files, is_staged
                        )
                    # Handle files
                    else:
                        result[file_path] = self._get_file_status_from_batch(
                            file_path, rel_path, untracked_files, staged_files, tracked_files, is_staged
                        )

                except Exception as e:
                    logger.debug("Error processing status for %s: %s", file_path, e)
                    result[file_path] = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

            # Fill in any missing paths with default status
            for file_path in file_paths:
                if file_path not in result:
                    result[file_path] = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

        except Exception as e:
            logger.error("Error in get_file_status_batch: %s", e)
            # Return default status for all paths on error
            for file_path in file_paths:
                if file_path not in result:
                    result[file_path] = {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

        return result

    def _get_staging_status_from_batch(self, file_path: str, rel_path: str,
                                       staged_files: set, unstaged_files: set) -> Union[bool, str]:
        """Get staging status using pre-fetched batch data."""
        try:
            if os.path.isdir(file_path):
                # For directories, check files within
                dir_staged_files = [f for f in staged_files if f.startswith(rel_path + '/') or f == rel_path]
                dir_unstaged_files = [f for f in unstaged_files if f.startswith(rel_path + '/') or f == rel_path]

                has_staged = len(dir_staged_files) > 0
                has_unstaged = len(dir_unstaged_files) > 0

                # Check for mixed staging
                has_mixed_files = any(f in dir_unstaged_files for f in dir_staged_files)

                if has_mixed_files or (has_staged and has_unstaged):
                    return "mixed"
                elif has_staged:
                    return True
                else:
                    return False
            else:
                # For files
                has_staged = rel_path in staged_files
                has_unstaged = rel_path in unstaged_files

                if has_staged and has_unstaged:
                    return "mixed"
                elif has_staged:
                    return True
                else:
                    return False
        except Exception:
            return False

    def _get_directory_status_from_batch(self, file_path: str, rel_path: str,
                                         untracked_files: set, status_map: dict,
                                         tracked_files: set, is_staged: Union[bool, str]) -> Dict[str, Any]:
        """Get directory status using pre-fetched batch data."""
        try:
            rel_path_normalized = rel_path.replace('\\', '/')

            # Check for untracked files in this directory
            has_untracked = any(
                f.replace('\\', '/').startswith(rel_path_normalized + '/') or f.replace('\\', '/') == rel_path_normalized
                for f in untracked_files
            )

            # Check for modified/deleted files using status map
            has_modified = False
            has_deleted = False

            for status_file_path, status_line in status_map.items():
                if len(status_line) >= 2:
                    file_normalized = status_file_path.replace('\\', '/')
                    if file_normalized.startswith(rel_path_normalized + '/') or file_normalized == rel_path_normalized:
                        index_status = status_line[0] if len(status_line) > 0 else ' '
                        worktree_status = status_line[1] if len(status_line) > 1 else ' '

                        if index_status in ['M', 'A', 'R', 'C'] or worktree_status in ['M', 'A', 'R', 'C']:
                            has_modified = True
                        elif index_status == 'D' or worktree_status == 'D':
                            has_deleted = True

            if has_untracked:
                return {"is_tracked": False, "status": "untracked", "is_ignored": False, "is_staged": is_staged}
            elif has_deleted:
                return {"is_tracked": True, "status": "deleted", "is_ignored": False, "is_staged": is_staged}
            elif has_modified:
                return {"is_tracked": True, "status": "modified", "is_ignored": False, "is_staged": is_staged}

            # Check if directory has tracked files
            has_tracked = any(
                f.replace('\\', '/').startswith(rel_path_normalized + '/') or f.replace('\\', '/') == rel_path_normalized
                for f in tracked_files
            )

            status = "clean" if has_tracked else None
            return {"is_tracked": has_tracked, "status": status, "is_ignored": False, "is_staged": is_staged}

        except Exception as e:
            logger.debug("Error getting directory status for %s: %s", file_path, e)
            return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

    def _get_file_status_from_batch(self, file_path: str, rel_path: str,
                                    untracked_files: set, staged_files: set,
                                    tracked_files: set, is_staged: Union[bool, str]) -> Dict[str, Any]:
        """Get file status using pre-fetched batch data."""
        try:
            # Check if untracked
            if rel_path in untracked_files:
                return {"is_tracked": False, "status": "untracked", "is_ignored": False, "is_staged": is_staged}

            # If file is staged, determine original status
            if is_staged:
                # Check if file existed in HEAD
                try:
                    self.repo.git.show(f"HEAD:{rel_path}")
                    # File existed in HEAD
                    return {"is_tracked": True, "status": "modified", "is_ignored": False, "is_staged": is_staged}
                except Exception:
                    # File didn't exist in HEAD (new file)
                    return {"is_tracked": False, "status": "added", "is_ignored": False, "is_staged": is_staged}

            # Check if tracked and dirty
            try:
                if self.repo.is_dirty(path=rel_path):
                    return {"is_tracked": True, "status": "modified", "is_ignored": False, "is_staged": is_staged}
            except Exception:
                pass

            # Check if tracked and clean
            if rel_path in tracked_files:
                return {"is_tracked": True, "status": "clean", "is_ignored": False, "is_staged": is_staged}

            return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

        except Exception as e:
            logger.debug("Error getting file status for %s: %s", file_path, e)
            return {"is_tracked": False, "status": None, "is_ignored": False, "is_staged": False}

    def get_status_summary(self) -> Dict[str, int]:
        """Get summary of Git status."""
        if not self.is_git_repo or not self.repo:
            return {}
        
        try:
            status = self.repo.git.status(porcelain=True).strip()
            if not status:
                return {"clean": 0}
            
            summary = {"modified": 0, "deleted": 0, "untracked": 0}
            
            for line in status.split('\n'):
                if len(line) >= 2:
                    index_status = line[0]
                    worktree_status = line[1]
                    
                    # Count A (added) as untracked since they represent originally untracked files
                    if index_status == 'A' or worktree_status == 'A':
                        summary["untracked"] += 1
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
            # Handle case where repository has no commits (no HEAD)
            try:
                staged_files = self.repo.index.diff("HEAD")
            except Exception as e:
                logger.debug("🔍 [TRACE] No HEAD found (likely no commits), using staged-only detection: %s", e)
                # When no HEAD exists, we need to get staged files differently
                staged_file_names = []
                try:
                    staged_output = self.repo.git.diff('--cached', '--name-only')
                    staged_file_names = staged_output.splitlines() if staged_output.strip() else []
                except Exception:
                    staged_file_names = []
                logger.debug("🔍 [TRACE] Found %d staged files in no-HEAD repo: %s", len(staged_file_names), staged_file_names)
                
                # Create staged file changes manually for no-HEAD repos
                for file_repo_path in staged_file_names:
                    file_abs_path = os.path.join(self.project_path, file_repo_path)
                    file_name = os.path.basename(file_repo_path)
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    
                    # For staged files in no-HEAD repo, they are all "added" (new files)
                    diff_details = None
                    
                    change = GitFileChange(
                        file_repo_path=file_repo_path,
                        file_name=file_name,
                        file_abs_path=file_abs_path,
                        change_type='added',
                        content_hash=content_hash,
                        is_staged=True,
                        diff_details=diff_details
                    )
                    logger.debug("🔍 [TRACE] Created staged change for no-HEAD repo: %s (added)", file_name)
                    detailed_status.staged_changes.append(change)
                
                # Skip the normal staged files loop since we handled it above
                staged_files = []
            # Get git status --porcelain for accurate change types (GitPython diff can be buggy)
            try:
                porcelain_status = self.repo.git.status(porcelain=True).strip()
                porcelain_map = {}
                if porcelain_status:
                    for line in porcelain_status.split('\n'):
                        if len(line) >= 3:
                            index_status = line[0]
                            file_path = line[3:]
                            porcelain_map[file_path] = index_status
            except Exception:
                porcelain_map = {}
            
            for diff_item in staged_files:
                file_repo_path = diff_item.a_path or diff_item.b_path
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                
                # Determine change type - use porcelain status for accuracy, fall back to GitPython
                porcelain_status = porcelain_map.get(file_repo_path, '')
                if porcelain_status == 'A':
                    change_type = 'untracked'
                elif porcelain_status == 'M':
                    change_type = 'modified'
                elif porcelain_status == 'D':
                    change_type = 'deleted'
                else:
                    # Fall back to GitPython detection
                    if diff_item.deleted_file:
                        change_type = 'deleted'
                    elif diff_item.new_file:
                        change_type = 'untracked'
                    else:
                        change_type = 'modified'
                
                # Set content hash and diff details based on change type
                if change_type == 'deleted':
                    content_hash = None
                    diff_details = None  # No diff for deleted files
                elif change_type == 'untracked':
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # For new files, compare empty content vs current staged content
                    diff_details = None
                else:  # modified
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # Compare HEAD content vs staged content
                    diff_details = None
                
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
            try:
                unstaged_files = self.repo.index.diff(None)
            except Exception as e:
                logger.debug("🔍 [TRACE] Error getting unstaged files: %s", e)
                unstaged_files = []
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
                    diff_details = None
                else:
                    change_type = 'modified'
                    content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                    # Compare staged/index content vs working content
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
            try:
                untracked_files = self.repo.untracked_files
                logger.debug("🔍 [TRACE] Processing %d untracked files: %s", len(untracked_files), untracked_files)
            except Exception as e:
                logger.debug("🔍 [TRACE] Error getting untracked files: %s", e)
                untracked_files = []
            for file_repo_path in untracked_files:
                file_abs_path = os.path.join(self.project_path, file_repo_path)
                file_name = os.path.basename(file_repo_path)
                content_hash = self._compute_file_hash(file_abs_path) if os.path.exists(file_abs_path) else None
                
                # For untracked files, compare empty content vs current file content
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
                logger.debug("🔍 [TRACE] Created untracked change for: %s", file_name)
                detailed_status.untracked_files.append(change)
            
            logger.debug("🔍 [TRACE] Returning detailed_status with %d staged, %d unstaged, %d untracked", 
                        len(detailed_status.staged_changes), 
                        len(detailed_status.unstaged_changes), 
                        len(detailed_status.untracked_files))
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
    
    def _is_submodule(self, file_path: str) -> bool:
        """Check if the given path is a submodule."""
        if not self.is_git_repo or not self.repo:
            return False
        
        try:
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Check if this path is listed in .gitmodules
            gitmodules_path = os.path.join(self.repo.working_dir, '.gitmodules')
            if os.path.exists(gitmodules_path):
                try:
                    with open(gitmodules_path, 'r') as f:
                        content = f.read()
                        # Simple check - look for path = rel_path in .gitmodules
                        for line in content.splitlines():
                            if line.strip().startswith('path ='):
                                submodule_path = line.split('=', 1)[1].strip()
                                if submodule_path == rel_path:
                                    return True
                except Exception as e:
                    logger.warning("Error reading .gitmodules: %s", e)
            
            # Alternative check: see if the path has a .git file (submodule indicator)
            git_path = os.path.join(file_path, '.git')
            if os.path.isfile(git_path):  # Submodules have .git as a file, not directory
                return True
                
            return False
            
        except Exception as e:
            logger.warning("Error checking if %s is submodule: %s", file_path, e)
            return False

    def stage_file(self, file_path: str) -> bool:
        """Stage a file for commit."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Check if this is a submodule
            if self._is_submodule(file_path):
                logger.info("Detected submodule, using git add command directly: %s", rel_path)
                # For submodules, use git add directly to stage only the submodule reference
                self.repo.git.add(rel_path)
            else:
                # For regular files, use the index method
                self.repo.index.add([rel_path])
            
            logger.info("Successfully staged file: %s", rel_path)
            return True
            
        except Exception as e:
            logger.error("Error staging file %s: %s", file_path, e)
            raise RuntimeError(f"Failed to stage file: {e}")
    
    def unstage_file(self, file_path: str) -> bool:
        """Unstage a file (remove from staging area)."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Check if this is a submodule
            if self._is_submodule(file_path):
                logger.info("Detected submodule, using git restore for unstaging: %s", rel_path)
                # For submodules, always use git restore --staged (works with submodules)
                self.repo.git.restore('--staged', rel_path)
            else:
                # Check if repository has any commits (HEAD exists)
                try:
                    self.repo.head.commit
                    has_head = True
                except Exception:
                    has_head = False
                
                if has_head:
                    # Reset the file from HEAD (unstage) - for repos with commits
                    self.repo.git.restore('--staged', rel_path)
                else:
                    # For repositories with no commits, use git rm --cached to unstage
                    self.repo.git.rm('--cached', rel_path)
            
            logger.info("Successfully unstaged file: %s", rel_path)
            return True
            
        except Exception as e:
            logger.error("Error unstaging file %s: %s", file_path, e)
            raise RuntimeError(f"Failed to unstage file: {e}")
    
    def revert_file(self, file_path: str) -> bool:
        """Revert a file to its HEAD version (discard local changes)."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Convert to relative path from repo root
            rel_path = os.path.relpath(file_path, self.repo.working_dir)
            
            # Check if repository has any commits (HEAD exists)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            if has_head:
                # Restore the file from HEAD - for repos with commits
                self.repo.git.restore(rel_path)
                logger.info("Successfully reverted file: %s", rel_path)
            else:
                # For repositories with no commits, we can't revert to HEAD
                # Instead, just remove the file to "revert" it to non-existence
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info("Successfully removed file (no HEAD to revert to): %s", rel_path)
                else:
                    logger.info("File already does not exist (no HEAD to revert to): %s", rel_path)
            
            return True
            
        except Exception as e:
            logger.error("Error reverting file %s: %s", file_path, e)
            raise RuntimeError(f"Failed to revert file: {e}")
    
    def stage_files(self, file_paths: List[str]) -> bool:
        """Stage multiple files for commit in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        if not file_paths:
            logger.info("No files provided for staging")
            return True
        
        try:
            # Convert all paths to relative paths from repo root
            rel_paths = []
            submodule_paths = []
            
            for file_path in file_paths:
                rel_path = os.path.relpath(file_path, self.repo.working_dir)
                if self._is_submodule(file_path):
                    submodule_paths.append(rel_path)
                else:
                    rel_paths.append(rel_path)
            
            # Stage submodules using git add directly
            if submodule_paths:
                logger.info("Staging submodules using git add directly: %s", submodule_paths)
                for submodule_path in submodule_paths:
                    self.repo.git.add(submodule_path)
            
            # Stage regular files using index.add for efficiency
            if rel_paths:
                logger.info("Staging regular files: %s", rel_paths)
                self.repo.index.add(rel_paths)
            
            logger.info("Successfully staged %d files (%d submodules, %d regular)", 
                       len(file_paths), len(submodule_paths), len(rel_paths))
            return True
            
        except Exception as e:
            logger.error("Error staging files %s: %s", file_paths, e)
            raise RuntimeError(f"Failed to stage files: {e}")

    def unstage_files(self, file_paths: List[str]) -> bool:
        """Unstage multiple files in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        if not file_paths:
            logger.info("No files provided for unstaging")
            return True
        
        try:
            # Convert all paths to relative paths from repo root
            rel_paths = []
            submodule_paths = []
            
            for file_path in file_paths:
                rel_path = os.path.relpath(file_path, self.repo.working_dir)
                if self._is_submodule(file_path):
                    submodule_paths.append(rel_path)
                else:
                    rel_paths.append(rel_path)
            
            # Check if repository has any commits (HEAD exists)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            # Unstage all files using appropriate method
            all_rel_paths = rel_paths + submodule_paths
            
            if has_head:
                # Use git restore --staged for all files (works for both regular files and submodules)
                if all_rel_paths:
                    self.repo.git.restore('--staged', *all_rel_paths)
            else:
                # For repositories with no commits, use git rm --cached
                if all_rel_paths:
                    self.repo.git.rm('--cached', *all_rel_paths)
            
            logger.info("Successfully unstaged %d files (%d submodules, %d regular)", 
                       len(file_paths), len(submodule_paths), len(rel_paths))
            return True
            
        except Exception as e:
            logger.error("Error unstaging files %s: %s", file_paths, e)
            raise RuntimeError(f"Failed to unstage files: {e}")

    def revert_files(self, file_paths: List[str]) -> bool:
        """Revert multiple files to their HEAD version in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        if not file_paths:
            logger.info("No files provided for reverting")
            return True
        
        try:
            # Check if repository has any commits (HEAD exists)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            if has_head:
                # Convert to relative paths and restore all files at once
                rel_paths = [os.path.relpath(file_path, self.repo.working_dir) for file_path in file_paths]
                # Filter out submodules - we don't revert submodules as they don't have working directory changes
                regular_files = []
                for i, file_path in enumerate(file_paths):
                    if not self._is_submodule(file_path):
                        regular_files.append(rel_paths[i])
                
                if regular_files:
                    self.repo.git.restore(*regular_files)
                    logger.info("Successfully reverted %d files", len(regular_files))
            else:
                # For repositories with no commits, remove files to "revert" them
                removed_count = 0
                for file_path in file_paths:
                    if not self._is_submodule(file_path) and os.path.exists(file_path):
                        os.remove(file_path)
                        removed_count += 1
                logger.info("Successfully removed %d files (no HEAD to revert to)", removed_count)
            
            return True
            
        except Exception as e:
            logger.error("Error reverting files %s: %s", file_paths, e)
            raise RuntimeError(f"Failed to revert files: {e}")

    def stage_all_changes(self) -> bool:
        """Stage all changes (modified, deleted, untracked) in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Use git add . to stage everything - this is the most efficient way
            self.repo.git.add('.')
            logger.info("Successfully staged all changes using 'git add .'")
            return True
            
        except Exception as e:
            logger.error("Error staging all changes: %s", e)
            raise RuntimeError(f"Failed to stage all changes: {e}")

    def unstage_all_changes(self) -> bool:
        """Unstage all staged changes in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Check if repository has any commits (HEAD exists)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            if has_head:
                # Use git restore --staged . to unstage everything
                self.repo.git.restore('--staged', '.')
            else:
                # For repositories with no commits, remove everything from index
                self.repo.git.rm('--cached', '-r', '.')
            
            logger.info("Successfully unstaged all changes")
            return True
            
        except Exception as e:
            logger.error("Error unstaging all changes: %s", e)
            raise RuntimeError(f"Failed to unstage all changes: {e}")

    def revert_all_changes(self) -> bool:
        """Revert all working directory changes in one atomic operation."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        try:
            # Check if repository has any commits (HEAD exists)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            if has_head:
                # Use git restore . to revert all working directory changes
                self.repo.git.restore('.')
                logger.info("Successfully reverted all working directory changes")
            else:
                logger.warning("Cannot revert changes in repository with no commits")
                return False
            
            return True
            
        except Exception as e:
            logger.error("Error reverting all changes: %s", e)
            raise RuntimeError(f"Failed to revert all changes: {e}")

    def commit_changes(self, message: str) -> bool:
        """Commit staged changes with the given message."""
        if not self.is_git_repo or not self.repo:
            raise RuntimeError("Not a git repository")
        
        if not message or not message.strip():
            raise ValueError("Commit message cannot be empty")
        
        try:
            # Handle repositories with no previous commits (first commit)
            try:
                self.repo.head.commit
                has_head = True
            except Exception:
                has_head = False
            
            if not has_head:
                # For the first commit, check if anything is staged
                try:
                    staged_output = self.repo.git.diff('--cached', '--name-only')
                    has_staged = bool(staged_output.strip())
                except Exception:
                    has_staged = False
            else:
                # Check if there are staged changes to commit
                staged_changes = self.repo.index.diff("HEAD")
                has_staged = len(staged_changes) > 0
            
            if not has_staged:
                raise RuntimeError("No staged changes to commit")
            
            # Perform the commit
            commit = self.repo.index.commit(message.strip())
            logger.info("Successfully committed changes with hash: %s", commit.hexsha)
            logger.info("Commit message: %s", message.strip())
            
            return True
            
        except Exception as e:
            logger.error("Error committing changes: %s", e)
            raise RuntimeError(f"Failed to commit changes: {e}")
    
    def start_periodic_monitoring(self):
        """Start periodic monitoring of git status changes."""
        if not self.is_git_repo or not self._change_callback:
            return
        
        if self._monitoring_task and not self._monitoring_task.done():
            logger.debug("Git monitoring already running for %s", self.project_path)
            return
        
        logger.info("Starting periodic git monitoring for %s", self.project_path)
        self._monitoring_enabled = True
        
        # Initialize cached status
        self._update_cached_status()
        
        # Start the monitoring task
        self._monitoring_task = asyncio.create_task(self._monitor_git_changes())
    
    def stop_periodic_monitoring(self):
        """Stop periodic monitoring of git status changes."""
        self._monitoring_enabled = False
        
        if self._monitoring_task and not self._monitoring_task.done():
            logger.info("Stopping periodic git monitoring for %s", self.project_path)
            self._monitoring_task.cancel()
            self._monitoring_task = None
    
    def _update_cached_status(self):
        """Update cached git status for comparison."""
        if not self.is_git_repo:
            return
        
        try:
            self._cached_status_summary = self.get_status_summary()
            self._cached_detailed_status = self.get_detailed_status()
            self._cached_branch = self.get_branch_name()
            logger.debug("Updated cached git status for %s", self.project_path)
        except Exception as e:
            logger.error("Error updating cached git status: %s", e)
    
    async def _monitor_git_changes(self):
        """Monitor git changes periodically and trigger callback when changes are detected."""
        try:
            while self._monitoring_enabled:
                await asyncio.sleep(5.0)  # Check every 5000ms

                if not self._monitoring_enabled or not self.is_git_repo:
                    break

                try:
                    # Get current status - run in executor to avoid blocking event loop
                    loop = asyncio.get_event_loop()
                    current_status_summary = await loop.run_in_executor(None, self.get_status_summary)
                    current_detailed_status = await loop.run_in_executor(None, self.get_detailed_status)
                    current_branch = await loop.run_in_executor(None, self.get_branch_name)
                    
                    # Compare with cached status
                    status_changed = (
                        current_status_summary != self._cached_status_summary or
                        current_branch != self._cached_branch or
                        self._detailed_status_changed(current_detailed_status, self._cached_detailed_status)
                    )
                    
                    if status_changed:
                        logger.info("Git status change detected for %s", self.project_path)
                        logger.debug("Status summary: %s -> %s", self._cached_status_summary, current_status_summary)
                        logger.debug("Branch: %s -> %s", self._cached_branch, current_branch)
                        
                        # Update cached status
                        self._cached_status_summary = current_status_summary
                        self._cached_detailed_status = current_detailed_status
                        self._cached_branch = current_branch
                        
                        # Trigger callback
                        if self._change_callback:
                            try:
                                if asyncio.iscoroutinefunction(self._change_callback):
                                    await self._change_callback()
                                else:
                                    self._change_callback()
                            except Exception as e:
                                logger.error("Error in git change callback: %s", e)
                    
                except Exception as e:
                    logger.error("Error during git status monitoring: %s", e)
                    # Continue monitoring despite errors
                    
        except asyncio.CancelledError:
            logger.debug("Git monitoring cancelled for %s", self.project_path)
        except Exception as e:
            logger.error("Fatal error in git monitoring: %s", e)
        finally:
            logger.debug("Git monitoring stopped for %s", self.project_path)
    
    def _detailed_status_changed(self, current: Optional[GitDetailedStatus], cached: Optional[GitDetailedStatus]) -> bool:
        """Compare detailed status objects for changes."""
        if current is None and cached is None:
            return False
        if current is None or cached is None:
            return True
        
        # Compare key attributes
        if (
            current.head_commit_hash != cached.head_commit_hash or
            len(current.staged_changes) != len(cached.staged_changes) or
            len(current.unstaged_changes) != len(cached.unstaged_changes) or
            len(current.untracked_files) != len(cached.untracked_files)
        ):
            return True
        
        # Compare staged changes content hashes
        current_staged_hashes = {c.file_repo_path: c.content_hash for c in current.staged_changes}
        cached_staged_hashes = {c.file_repo_path: c.content_hash for c in cached.staged_changes}
        if current_staged_hashes != cached_staged_hashes:
            return True
        
        # Compare unstaged changes content hashes
        current_unstaged_hashes = {c.file_repo_path: c.content_hash for c in current.unstaged_changes}
        cached_unstaged_hashes = {c.file_repo_path: c.content_hash for c in cached.unstaged_changes}
        if current_unstaged_hashes != cached_unstaged_hashes:
            return True
        
        # Compare untracked files content hashes
        current_untracked_hashes = {c.file_repo_path: c.content_hash for c in current.untracked_files}
        cached_untracked_hashes = {c.file_repo_path: c.content_hash for c in cached.untracked_files}
        if current_untracked_hashes != cached_untracked_hashes:
            return True
        
        return False
    
    def cleanup(self):
        """Cleanup resources when GitManager is being destroyed."""
        logger.info("Cleaning up GitManager for %s", self.project_path)
        self.stop_periodic_monitoring()