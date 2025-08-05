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