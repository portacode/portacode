"""File operation handlers for demonstrating the send_command functionality."""

import os
import logging
import fnmatch
import re
import json
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence
from pathlib import Path

from .base import AsyncHandler, SyncHandler
from .chunked_content import create_chunked_response

logger = logging.getLogger(__name__)

# Global content cache: hash -> content
_content_cache = {}


class FileReadHandler(SyncHandler):
    """Handler for reading file contents."""
    
    @property
    def command_name(self) -> str:
        return "file_read"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Read file contents with optional pagination."""
        file_path = message.get("path")
        if not file_path:
            raise ValueError("path parameter is required")

        encoding = message.get("encoding", "utf-8")
        start_line = self._coerce_positive_int(message.get("start_line"), default=1)
        max_lines = self._coerce_positive_int(message.get("max_lines"), allow_none=True)
        end_line = self._coerce_positive_int(message.get("end_line"), allow_none=True)

        if start_line < 1:
            start_line = 1

        if end_line is not None and end_line >= start_line:
            range_len = end_line - start_line + 1
            if max_lines is None:
                max_lines = range_len
            else:
                max_lines = min(max_lines, range_len)

        if max_lines is not None:
            max_lines = min(max_lines, 2000)

        try:
            file_size = os.path.getsize(file_path)
        except FileNotFoundError:
            raise ValueError(f"File not found: {file_path}")
        except PermissionError:
            raise RuntimeError(f"Permission denied: {file_path}")

        total_lines = 0
        collected_lines: List[str] = []
        truncated_after = False

        try:
            with open(file_path, "r", encoding=encoding, errors="replace") as file_obj:
                for idx, line in enumerate(file_obj, start=1):
                    total_lines += 1
                    if idx < start_line:
                        continue

                    if max_lines is not None and len(collected_lines) >= max_lines:
                        truncated_after = True
                        continue

                    collected_lines.append(line)
        except FileNotFoundError:
            raise ValueError(f"File not found: {file_path}")
        except PermissionError:
            raise RuntimeError(f"Permission denied: {file_path}")
        except OSError as exc:
            raise RuntimeError(f"Error reading file: {exc}")

        returned_start_line = start_line if collected_lines else None
        returned_end_line = (
            start_line + len(collected_lines) - 1 if collected_lines else None
        )
        has_more_before = bool(collected_lines) and start_line > 1
        has_more_after = truncated_after or (
            returned_end_line is not None and total_lines > returned_end_line
        )

        return {
            "event": "file_read_response",
            "path": file_path,
            "content": "".join(collected_lines),
            "size": file_size,
            "total_lines": total_lines,
            "returned_lines": len(collected_lines),
            "start_line": returned_start_line,
            "requested_start_line": start_line,
            "end_line": returned_end_line,
            "has_more_before": has_more_before,
            "has_more_after": has_more_after,
            "encoding": encoding,
        }

    @staticmethod
    def _coerce_positive_int(
        value: Any,
        *,
        default: Optional[int] = None,
        allow_none: bool = False,
    ) -> Optional[int]:
        if value is None:
            if allow_none:
                return None
            return default or 0
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return None if allow_none else (default or 0)
        if coerced <= 0:
            return None if allow_none else (default or 0)
        return coerced


class FileWriteHandler(SyncHandler):
    """Handler for writing file contents."""
    
    @property
    def command_name(self) -> str:
        return "file_write"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Write file contents."""
        file_path = message.get("path")
        content = message.get("content", "")
        
        if not file_path:
            raise ValueError("path parameter is required")
        
        try:
            # Create parent directories if they don't exist
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {
                "event": "file_write_response",
                "path": file_path,
                "bytes_written": len(content.encode('utf-8')),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {file_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to write file: {e}")


class DirectoryListHandler(SyncHandler):
    """Handler for listing directory contents."""
    
    @property
    def command_name(self) -> str:
        return "directory_list"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents."""
        path = message.get("path", ".")
        show_hidden = message.get("show_hidden", False)
        limit_raw = message.get("limit")
        offset_raw = message.get("offset", 0)

        def _parse_positive_int(value, *, allow_none=False, minimum=0, maximum=None):
            if value is None:
                return None if allow_none else minimum
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None if allow_none else minimum
            if parsed < minimum:
                parsed = minimum
            if maximum is not None and parsed > maximum:
                parsed = maximum
            return parsed

        offset = _parse_positive_int(offset_raw, minimum=0)
        limit = _parse_positive_int(limit_raw, allow_none=True, minimum=1, maximum=1000)
        
        try:
            items = []
            for item in os.listdir(path):
                # Skip hidden files unless requested
                if not show_hidden and item.startswith('.'):
                    continue
                    
                item_path = os.path.join(path, item)
                try:
                    stat_info = os.stat(item_path)
                    items.append({
                        "name": item,
                        "is_dir": os.path.isdir(item_path),
                        "is_file": os.path.isfile(item_path),
                        "size": stat_info.st_size,
                        "modified": stat_info.st_mtime,
                        "permissions": oct(stat_info.st_mode)[-3:],
                    })
                except (OSError, PermissionError):
                    # Skip items we can't stat
                    continue
            
            total_count = len(items)

            if offset:
                if offset >= total_count:
                    sliced_items = []
                else:
                    sliced_items = items[offset:]
            else:
                sliced_items = items

            if limit is not None and limit >= 0:
                sliced_items = sliced_items[:limit]

            returned_count = len(sliced_items)
            has_more = total_count > offset + returned_count if total_count else False
            
            return {
                "event": "directory_list_response",
                "path": path,
                "items": sliced_items,
                "count": returned_count,
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
            }
        except FileNotFoundError:
            raise ValueError(f"Directory not found: {path}")
        except PermissionError:
            raise RuntimeError(f"Permission denied: {path}")
        except NotADirectoryError:
            raise ValueError(f"Path is not a directory: {path}")


class FileInfoHandler(SyncHandler):
    """Handler for getting file/directory information."""
    
    @property
    def command_name(self) -> str:
        return "file_info"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Get file or directory information."""
        path = message.get("path")
        if not path:
            raise ValueError("path parameter is required")
        
        try:
            stat_info = os.stat(path)
            
            return {
                "event": "file_info_response",
                "path": path,
                "exists": True,
                "is_file": os.path.isfile(path),
                "is_dir": os.path.isdir(path),
                "is_symlink": os.path.islink(path),
                "size": stat_info.st_size,
                "modified": stat_info.st_mtime,
                "accessed": stat_info.st_atime,
                "created": stat_info.st_ctime,
                "permissions": oct(stat_info.st_mode)[-3:],
                "owner_uid": stat_info.st_uid,
                "group_gid": stat_info.st_gid,
            }
        except FileNotFoundError:
            return {
                "event": "file_info_response", 
                "path": path,
                "exists": False,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {path}")


class FileDeleteHandler(SyncHandler):
    """Handler for deleting files and directories."""
    
    @property
    def command_name(self) -> str:
        return "file_delete"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a file or directory."""
        path = message.get("path")
        recursive = message.get("recursive", False)
        
        if not path:
            raise ValueError("path parameter is required")
        
        try:
            if os.path.isfile(path):
                os.remove(path)
                deleted_type = "file"
            elif os.path.isdir(path):
                if recursive:
                    import shutil
                    shutil.rmtree(path)
                else:
                    os.rmdir(path)
                deleted_type = "directory"
            else:
                raise ValueError(f"Path does not exist: {path}")
            
            return {
                "event": "file_delete_response",
                "path": path,
                "deleted_type": deleted_type,
                "success": True,
            }
        except FileNotFoundError:
            raise ValueError(f"Path not found: {path}")
        except PermissionError:
            raise RuntimeError(f"Permission denied: {path}")
        except OSError as e:
            if "Directory not empty" in str(e):
                raise ValueError(f"Directory not empty (use recursive=True): {path}")
            raise RuntimeError(f"Failed to delete: {e}")


class FileCreateHandler(SyncHandler):
    """Handler for creating new files."""
    
    @property
    def command_name(self) -> str:
        return "file_create"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new file."""
        parent_path = message.get("parent_path")
        file_name = message.get("file_name")
        content = message.get("content", "")
        
        if not parent_path:
            raise ValueError("parent_path parameter is required")
        if not file_name:
            raise ValueError("file_name parameter is required")
        
        # Validate file name (no path separators or special chars)
        if "/" in file_name or "\\" in file_name or file_name in [".", ".."]:
            raise ValueError("Invalid file name")
        
        try:
            # Ensure parent directory exists
            parent_dir = Path(parent_path)
            if not parent_dir.exists():
                raise ValueError(f"Parent directory does not exist: {parent_path}")
            if not parent_dir.is_dir():
                raise ValueError(f"Parent path is not a directory: {parent_path}")
            
            # Create the full file path
            file_path = parent_dir / file_name
            
            # Check if file already exists
            if file_path.exists():
                raise ValueError(f"File already exists: {file_name}")
            
            # Create the file
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {
                "event": "file_create_response",
                "parent_path": parent_path,
                "file_name": file_name,
                "file_path": str(file_path),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {parent_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to create file: {e}")


class FolderCreateHandler(SyncHandler):
    """Handler for creating new folders."""
    
    @property
    def command_name(self) -> str:
        return "folder_create"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new folder."""
        parent_path = message.get("parent_path")
        folder_name = message.get("folder_name")
        
        if not parent_path:
            raise ValueError("parent_path parameter is required")
        if not folder_name:
            raise ValueError("folder_name parameter is required")
        
        # Validate folder name (no path separators or special chars)
        if "/" in folder_name or "\\" in folder_name or folder_name in [".", ".."]:
            raise ValueError("Invalid folder name")
        
        try:
            # Ensure parent directory exists
            parent_dir = Path(parent_path)
            if not parent_dir.exists():
                raise ValueError(f"Parent directory does not exist: {parent_path}")
            if not parent_dir.is_dir():
                raise ValueError(f"Parent path is not a directory: {parent_path}")
            
            # Create the full folder path
            folder_path = parent_dir / folder_name
            
            # Check if folder already exists
            if folder_path.exists():
                raise ValueError(f"Folder already exists: {folder_name}")
            
            # Create the folder
            folder_path.mkdir(parents=False, exist_ok=False)
            
            return {
                "event": "folder_create_response",
                "parent_path": parent_path,
                "folder_name": folder_name,
                "folder_path": str(folder_path),
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {parent_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to create folder: {e}")


class FileRenameHandler(SyncHandler):
    """Handler for renaming files and folders."""
    
    @property
    def command_name(self) -> str:
        return "file_rename"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Rename a file or folder."""
        old_path = message.get("old_path")
        new_name = message.get("new_name")
        
        if not old_path:
            raise ValueError("old_path parameter is required")
        if not new_name:
            raise ValueError("new_name parameter is required")
        
        # Validate new name (no path separators or special chars)
        if "/" in new_name or "\\" in new_name or new_name in [".", ".."]:
            raise ValueError("Invalid new name")
        
        try:
            old_path_obj = Path(old_path)
            if not old_path_obj.exists():
                raise ValueError(f"Path does not exist: {old_path}")
            
            # Create new path in same directory
            new_path = old_path_obj.parent / new_name
            
            # Check if target already exists
            if new_path.exists():
                raise ValueError(f"Target already exists: {new_name}")
            
            # Determine if it's a file or directory
            is_directory = old_path_obj.is_dir()
            
            # Rename the file/folder
            old_path_obj.rename(new_path)
            
            return {
                "event": "file_rename_response",
                "old_path": old_path,
                "new_path": str(new_path),
                "new_name": new_name,
                "is_directory": is_directory,
                "success": True,
            }
        except PermissionError:
            raise RuntimeError(f"Permission denied: {old_path}")
        except OSError as e:
            raise RuntimeError(f"Failed to rename: {e}")


class FileSearchHandler(SyncHandler):
    """Handler for searching text within files under a root directory."""

    DEFAULT_EXCLUDE_DIRS: Sequence[str] = (
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "tmp",
        "static",
        "assets",
        "coverage",
    )

    DEFAULT_EXCLUDE_FILE_GLOBS: Sequence[str] = (
        "*.min.js",
        "*.min.css",
    )

    BINARY_EXTENSIONS: Sequence[str] = (
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
        ".ttf", ".woff", ".woff2", ".eot",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac",
        ".exe", ".dll", ".so", ".dylib",
        ".class", ".jar",
    )

    DEFAULT_INCLUDE_EXTENSIONS: Sequence[str] = (
        ".py", ".pyi", ".pyx",
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".md", ".markdown", ".rst", ".txt",
        ".html", ".htm", ".css", ".scss", ".less",
        ".go", ".rs", ".java", ".kt", ".kts",
        ".c", ".h", ".hpp", ".hh", ".cc", ".cpp", ".cxx",
        ".cs", ".php", ".rb", ".swift", ".scala", ".sql",
        ".sh", ".bash", ".zsh", ".fish",
        ".env", ".dockerfile", ".gradle", ".mk", ".make", ".bat", ".ps1",
    )

    ALWAYS_INCLUDE_FILENAMES: Sequence[str] = (
        "Makefile",
        "Dockerfile",
        "Jenkinsfile",
        "Procfile",
        "Gemfile",
        "CMakeLists.txt",
        "build.gradle",
        "settings.gradle",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "pyproject.toml",
    )

    @property
    def command_name(self) -> str:
        return "file_search"

    def _search_with_rg(
        self,
        *,
        root_path: str,
        query: str,
        match_case: bool,
        use_regex: bool,
        whole_word: bool,
        include_hidden: bool,
        max_results: int,
        max_per_file: int,
        max_file_size: int,
        include_patterns: List[str],
        exclude_patterns: List[str],
        max_line_length: int,
        using_default_includes: bool,
    ) -> Optional[Dict[str, Any]]:
        """Perform fast search using ripgrep if available."""
        if shutil.which("rg") is None:
            return None

        cmd = [
            "rg",
            "--json",
            "--line-number",
            "--color",
            "never",
            "--no-heading",
            "--max-count",
            str(max_per_file),
            f"--max-filesize={max_file_size}B",
        ]

        if not match_case:
            cmd.append("--ignore-case")
        if not use_regex:
            cmd.append("--fixed-strings")
        if whole_word:
            cmd.append("--word-regexp")
        if include_hidden:
            cmd.append("--hidden")

        if using_default_includes:
            for ext in self.DEFAULT_INCLUDE_EXTENSIONS:
                cmd.extend(["-g", f"*{ext}"])
            for name in self.ALWAYS_INCLUDE_FILENAMES:
                cmd.extend(["-g", name])
        for pattern in include_patterns:
            cmd.extend(["-g", pattern])
        for pattern in exclude_patterns:
            cmd.extend(["-g", f"!{pattern}"])

        cmd.append(query)
        cmd.append(".")

        matches: List[Dict[str, Any]] = []
        truncated = False
        truncated_count = 0
        files_scanned = 0
        errors: List[str] = []
        stop_search = False
        deadline = time.monotonic() + 10.0  # hard cap to avoid long-running scans

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=root_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            logger.warning("Failed to execute ripgrep: %s", exc)
            return None

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                if time.monotonic() > deadline:
                    truncated = True
                    errors.append("Search aborted after reaching 10s execution limit.")
                    stop_search = True
                    break

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = payload.get("type")

                if event_type == "begin":
                    files_scanned += 1
                    continue

                if event_type == "match":
                    data = payload.get("data", {})
                    line_text = data.get("lines", {}).get("text", "")
                    line_number = data.get("line_number")
                    path_info = data.get("path", {}).get("text") or data.get("path", {}).get("bytes")
                    if not path_info:
                        continue
                    absolute_path = os.path.join(root_path, path_info)
                    relative_path = path_info

                    submatches = data.get("submatches", [])
                    if len(matches) >= max_results:
                        truncated = True
                        truncated_count += len(submatches)
                        stop_search = True
                        break

                    available = max_results - len(matches)
                    spans: List[List[int]] = []
                    for submatch in submatches:
                        if len(spans) >= available:
                            truncated = True
                            truncated_count += len(submatches) - len(spans)
                            stop_search = True
                            break
                        start = submatch.get("start", {}).get("offset")
                        end = submatch.get("end", {}).get("offset")
                        if start is None or end is None:
                            continue
                        spans.append([start, end])

                    if spans:
                        clean_line = line_text.rstrip("\n")
                        truncated_line = clean_line
                        line_truncated = False
                        if len(clean_line) > max_line_length:
                            truncated_line = clean_line[:max_line_length] + "..."
                            line_truncated = True

                        matches.append(
                            {
                                "path": absolute_path,
                                "relative_path": relative_path,
                                "line_number": line_number,
                                "line": truncated_line,
                                "match_spans": spans,
                                "match_count": len(spans),
                                "line_truncated": line_truncated,
                            }
                        )

                    if stop_search:
                        break
                elif event_type == "message":
                    message = payload.get("data", {}).get("msg") or payload.get("data", {}).get("text")
                    if message:
                        errors.append(message)

                if stop_search:
                    break
        finally:
            if stop_search and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
            else:
                proc.wait()

        stderr_output = ""
        if proc.stderr:
            try:
                stderr_output = proc.stderr.read().strip()
            except Exception:
                stderr_output = ""
        if stderr_output:
            errors.append(stderr_output)

        return {
            "event": "file_search_response",
            "root_path": root_path,
            "query": query,
            "match_case": match_case,
            "regex": use_regex,
            "whole_word": whole_word,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "matches": matches,
            "matches_returned": len(matches),
            "total_matches": len(matches) + truncated_count,
            "files_scanned": files_scanned,
            "truncated": truncated or truncated_count > 0,
            "truncated_count": truncated_count,
            "max_results": max_results,
            "max_matches_per_file": max_per_file,
            "errors": errors,
        }

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        root_path = message.get("root_path")
        query = message.get("query")

        if not root_path:
            raise ValueError("root_path parameter is required")
        if not query:
            raise ValueError("query parameter is required")

        if not os.path.isdir(root_path):
            raise ValueError(f"Root path is not a directory: {root_path}")

        match_case = bool(message.get("match_case", False))
        use_regex = bool(message.get("regex", False))
        whole_word = bool(message.get("whole_word", False))
        include_hidden = bool(message.get("include_hidden", False))

        max_results = self._clamp_int(message.get("max_results"), default=40, min_value=1, max_value=500)
        max_per_file = self._clamp_int(
            message.get("max_matches_per_file"),
            default=5,
            min_value=1,
            max_value=50,
        )
        max_file_size = self._clamp_int(
            message.get("max_file_size"),
            default=1024 * 1024,
            min_value=1024,
            max_value=10 * 1024 * 1024,
        )
        max_line_length = self._clamp_int(
            message.get("max_line_length"),
            default=200,
            min_value=32,
            max_value=1024,
        )

        include_patterns = self._normalize_patterns(message.get("include_patterns"))
        using_default_includes = not include_patterns
        raw_exclude_patterns = self._normalize_patterns(message.get("exclude_patterns"))
        using_default_excludes = not raw_exclude_patterns
        if using_default_excludes:
            exclude_patterns = []
            for directory in self.DEFAULT_EXCLUDE_DIRS:
                exclude_patterns.append(f"{directory}/**")
                exclude_patterns.append(f"**/{directory}/**")
            exclude_patterns.extend(self.DEFAULT_EXCLUDE_FILE_GLOBS)
        else:
            exclude_patterns = raw_exclude_patterns

        flags = 0 if match_case else re.IGNORECASE
        pattern = query if use_regex else re.escape(query)
        if whole_word:
            pattern = r"\b" + pattern + r"\b"

        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc

        rg_result = self._search_with_rg(
            root_path=root_path,
            query=query,
            match_case=match_case,
            use_regex=use_regex,
            whole_word=whole_word,
            include_hidden=include_hidden,
            max_results=max_results,
            max_per_file=max_per_file,
            max_file_size=max_file_size,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            max_line_length=max_line_length,
            using_default_includes=using_default_includes,
        )
        if rg_result is not None:
            return rg_result

        matches: List[Dict[str, Any]] = []
        truncated = False
        truncated_count = 0
        files_scanned = 0
        errors: List[str] = []
        stop_search = False

        binary_exts = {ext.lower() for ext in self.BINARY_EXTENSIONS}
        allowed_exts = {ext.lower() for ext in self.DEFAULT_INCLUDE_EXTENSIONS}

        deadline = time.monotonic() + 10.0

        for dirpath, dirnames, filenames in os.walk(root_path):
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            for filename in filenames:
                if time.monotonic() > deadline:
                    truncated = True
                    errors.append("Search aborted after reaching 10s execution limit.")
                    stop_search = True
                    break
                if not include_hidden and filename.startswith("."):
                    continue

                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, root_path)

                if using_default_excludes:
                    path_parts = rel_path.replace("\\", "/").split("/")
                    if any(part in self.DEFAULT_EXCLUDE_DIRS for part in path_parts):
                        continue

                if using_default_includes:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in allowed_exts and filename not in self.ALWAYS_INCLUDE_FILENAMES:
                        continue

                if os.path.splitext(filename)[1].lower() in binary_exts:
                    continue

                if not self._should_include(rel_path, include_patterns, exclude_patterns):
                    continue

                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    errors.append(f"Failed to stat file: {rel_path}")
                    continue

                if size > max_file_size:
                    errors.append(f"Skipped (too large): {rel_path} ({size} bytes)")
                    continue

                files_scanned += 1
                matches_for_file = 0

                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as file_obj:
                        stop_current_file = False
                        for line_number, line in enumerate(file_obj, start=1):
                            if time.monotonic() > deadline:
                                truncated = True
                                errors.append("Search aborted after reaching 10s execution limit.")
                                stop_search = True
                                stop_current_file = True
                                break
                            iter_matches = list(compiled.finditer(line))
                            if not iter_matches:
                                continue

                            # Enforce per-file cap
                            remaining_per_file = max_per_file - matches_for_file
                            if remaining_per_file <= 0:
                                truncated = True
                                truncated_count += len(iter_matches)
                                stop_current_file = True
                                break

                            spans = [
                                [match.start(), match.end()] for match in iter_matches[:remaining_per_file]
                            ]
                            dropped_from_file = len(iter_matches) - len(spans)
                            if dropped_from_file > 0:
                                truncated = True
                                truncated_count += dropped_from_file

                            # Enforce global cap
                            remaining_global = max_results - len(matches)
                            if remaining_global <= 0:
                                truncated = True
                                truncated_count += len(spans)
                                stop_search = True
                                break

                            if len(spans) > remaining_global:
                                truncated = True
                                truncated_count += len(spans) - remaining_global
                                spans = spans[:remaining_global]
                                stop_search = True

                            if spans:
                                clean_line = line.rstrip("\n")
                                truncated_line = clean_line
                                line_truncated = False
                                if len(clean_line) > max_line_length:
                                    truncated_line = clean_line[:max_line_length] + "..."
                                    line_truncated = True

                                matches.append(
                                    {
                                        "path": abs_path,
                                        "relative_path": rel_path,
                                        "line_number": line_number,
                                        "line": truncated_line,
                                        "match_spans": spans,
                                        "match_count": len(spans),
                                        "line_truncated": line_truncated,
                                    }
                                )
                                matches_for_file += len(spans)

                            if stop_search or matches_for_file >= max_per_file:
                                break
                        if stop_current_file:
                            break
                except (OSError, UnicodeDecodeError):
                    errors.append(f"Failed to read file: {rel_path}")
                    continue

                if stop_search:
                    break
            if stop_search:
                break

        total_matches = len(matches) + truncated_count

        return {
            "event": "file_search_response",
            "root_path": root_path,
            "query": query,
            "match_case": match_case,
            "regex": use_regex,
            "whole_word": whole_word,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "matches": matches,
            "matches_returned": len(matches),
            "total_matches": total_matches,
            "files_scanned": files_scanned,
            "truncated": truncated,
            "truncated_count": truncated_count,
            "max_results": max_results,
            "max_matches_per_file": max_per_file,
            "errors": errors,
        }

    @staticmethod
    def _normalize_patterns(patterns: Optional[Any]) -> List[str]:
        if not patterns:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        normalized: List[str] = []
        for pattern in patterns:
            if isinstance(pattern, str) and pattern.strip():
                normalized.append(pattern.strip())
        return normalized

    @staticmethod
    def _should_include(
        relative_path: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
    ) -> bool:
        if include_patterns:
            if not any(fnmatch.fnmatch(relative_path, pat) for pat in include_patterns):
                return False
        if exclude_patterns:
            if any(fnmatch.fnmatch(relative_path, pat) for pat in exclude_patterns):
                return False
        return True

    @staticmethod
    def _clamp_int(
        value: Optional[Any],
        *,
        default: int,
        min_value: int,
        max_value: int,
    ) -> int:
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            coerced = default
        return max(min_value, min(max_value, coerced))


class ContentRequestHandler(AsyncHandler):
    """Handler for requesting content by hash for caching optimization."""
    
    @property
    def command_name(self) -> str:
        return "content_request"
    
    async def execute(self, message: Dict[str, Any]) -> None:
        """Return content by hash if available, chunked for large content."""
        content_hash = message.get("content_hash")
        source_client_session = message.get("source_client_session")
        server_project_id = message.get("project_id")

        if not content_hash:
            raise ValueError("content_hash parameter is required")

        # Check if content is in cache
        content = _content_cache.get(content_hash)

        if content is not None:

            base_response = {
                "event": "content_response",
                "content_hash": content_hash,
                "success": True,
            }

            # Add request_id if present in original message
            if "request_id" in message:
                base_response["request_id"] = message["request_id"]
            
            # Create chunked responses
            responses = create_chunked_response(base_response, "content", content)
            
            # Send all responses
            for response in responses:
                await self.send_response(response, project_id=server_project_id)
            
            logger.info(f"Sent content response in {len(responses)} chunk(s) for hash: {content_hash[:16]}...")
        else:

            response = {
                "event": "content_response",
                "content_hash": content_hash,
                "content": None,
                "success": False,
                "error": "Content not found in cache",
                "chunked": False,
            }
            # Add request_id if present in original message
            if "request_id" in message:
                base_response["request_id"] = message["request_id"]
            await self.send_response(response, project_id=server_project_id)


def cache_content(content_hash: str, content: str) -> None:
    """Cache content by hash for future retrieval."""
    _content_cache[content_hash] = content


def get_cached_content(content_hash: str) -> str:
    """Get cached content by hash."""
    return _content_cache.get(content_hash)
