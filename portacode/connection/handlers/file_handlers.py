"""File operation handlers for demonstrating the send_command functionality."""

import os
import logging
from typing import Any, Dict
from pathlib import Path

from .base import AsyncHandler, SyncHandler

logger = logging.getLogger(__name__)


class FileReadHandler(SyncHandler):
    """Handler for reading file contents."""
    
    @property
    def command_name(self) -> str:
        return "file_read"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Read file contents."""
        file_path = message.get("path")
        if not file_path:
            raise ValueError("path parameter is required")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return {
                "event": "file_read_response",
                "path": file_path,
                "content": content,
                "size": len(content),
            }
        except FileNotFoundError:
            raise ValueError(f"File not found: {file_path}")
        except PermissionError:
            raise RuntimeError(f"Permission denied: {file_path}")
        except UnicodeDecodeError:
            raise RuntimeError(f"File is not text or uses unsupported encoding: {file_path}")


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
            
            return {
                "event": "directory_list_response",
                "path": path,
                "items": items,
                "count": len(items),
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