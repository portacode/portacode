"""Tab factory for creating TabInfo objects with appropriate content loading.

This module provides a centralized way to create tabs for different file types,
handling content loading, MIME type detection, and encoding appropriately.
"""

import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from .project_state_handlers import TabInfo
from .project_state.utils import generate_content_hash
from .file_handlers import cache_content

logger = logging.getLogger(__name__)

# Maximum file size for text content loading (10MB)
MAX_TEXT_FILE_SIZE = 10 * 1024 * 1024

# Maximum file size for binary content loading (50MB)
MAX_BINARY_FILE_SIZE = 50 * 1024 * 1024

# Text file extensions that should be treated as code/text
TEXT_EXTENSIONS = {
    # Programming languages
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.htm', '.css', '.scss', '.sass',
    '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.java', '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.cs', '.php', '.rb',
    '.go', '.rs', '.kt', '.swift', '.dart', '.scala', '.clj', '.hs', '.ml',
    '.r', '.m', '.pl', '.lua', '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat',
    '.sql', '.graphql', '.proto', '.thrift',
    
    # Markup and documentation
    '.md', '.markdown', '.rst', '.txt', '.rtf', '.tex', '.latex',
    '.adoc', '.asciidoc', '.org',
    
    # Configuration and data
    '.env', '.gitignore', '.gitattributes', '.dockerignore', '.editorconfig',
    '.eslintrc', '.prettierrc', '.babelrc', '.tsconfig', '.package-lock',
    '.requirements', '.pipfile', '.gemfile', '.makefile', '.cmake',
    
    # Web technologies
    '.vue', '.svelte', '.astro', '.ejs', '.hbs', '.handlebars', '.mustache',
    '.pug', '.jade', '.haml', '.slim',
    
    # Other text formats
    '.log', '.diff', '.patch', '.csv', '.tsv', '.properties'
}

# Binary file extensions that should be treated as media
MEDIA_EXTENSIONS = {
    # Images
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.svg',
    '.ico', '.icns', '.cur', '.psd', '.ai', '.eps', '.raw', '.cr2', '.nef',
    
    # Audio
    '.mp3', '.wav', '.flac', '.aac', '.ogg', '.oga', '.wma', '.m4a', '.opus', '.webm',
    
    # Video
    '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp',
    '.ogv', '.ts', '.mts', '.m2ts'
}

# Extensions that should be ignored/not loaded
IGNORED_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.cache', '.tmp', '.temp',
    '.lock', '.pid', '.swp', '.swo', '.bak', '.orig', '.pyc', '.pyo', '.class',
    '.o', '.obj', '.lib', '.a', '.jar', '.war', '.ear', '.zip', '.tar', '.gz',
    '.7z', '.rar', '.deb', '.rpm', '.dmg', '.iso', '.img'
}


class TabFactory:
    """Factory class for creating TabInfo objects with appropriate content."""
    
    def __init__(self):
        self.logger = logger.getChild(self.__class__.__name__)
    
    async def create_file_tab(self, file_path: str, tab_id: Optional[str] = None) -> TabInfo:
        """Create a file tab with content loaded based on file type.
        
        Args:
            file_path: Absolute path to the file
            tab_id: Optional tab ID, will generate UUID if not provided
            
        Returns:
            TabInfo object with appropriate content loaded
        """
        if tab_id is None:
            tab_id = str(uuid.uuid4())
        
        file_path = Path(file_path)
        
        # Basic tab info
        tab_info = {
            'tab_id': tab_id,
            'tab_type': 'file',
            'title': file_path.name,
            'file_path': str(file_path),
            'content': None,
            'original_content': None,
            'modified_content': None,
            'is_dirty': False,
            'mime_type': None,
            'encoding': None,
            'metadata': {}
        }
        
        # Check if file exists
        if not file_path.exists():
            self.logger.warning(f"File does not exist: {file_path}")
            tab_info['metadata']['error'] = 'File not found'
            return TabInfo(**tab_info)
        
        # Check if it's a file (not directory)
        if not file_path.is_file():
            self.logger.warning(f"Path is not a file: {file_path}")
            tab_info['metadata']['error'] = 'Not a file'
            return TabInfo(**tab_info)
        
        # Get file info
        try:
            file_stat = file_path.stat()
            file_size = file_stat.st_size
            tab_info['metadata']['size'] = file_size
            tab_info['metadata']['modified_time'] = file_stat.st_mtime
        except OSError as e:
            self.logger.error(f"Error getting file info for {file_path}: {e}")
            tab_info['metadata']['error'] = f'Cannot access file: {e}'
            return TabInfo(**tab_info)
        
        # Determine file type and MIME type
        extension = file_path.suffix.lower()
        mime_type, _ = mimetypes.guess_type(str(file_path))
        tab_info['mime_type'] = mime_type
        
        # Determine how to handle the file
        if extension in IGNORED_EXTENSIONS:
            tab_info['metadata']['ignored'] = True
            content = f"# Binary file not displayed\n# File: {file_path.name}\n# Size: {self._format_file_size(file_size)}"
            tab_info['content'] = content
            content_hash = generate_content_hash(content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, content)
            return TabInfo(**tab_info)
        
        # Handle different file types
        if extension in TEXT_EXTENSIONS or self._is_text_file(file_path, mime_type):
            await self._load_text_content(file_path, tab_info, file_size)
        elif extension in MEDIA_EXTENSIONS or (mime_type and mime_type.startswith(('image/', 'audio/', 'video/'))):
            await self._load_media_content(file_path, tab_info, file_size, mime_type)
        else:
            # Try to detect if it's a text file by sampling
            if await self._detect_text_file(file_path):
                await self._load_text_content(file_path, tab_info, file_size)
            else:
                await self._load_binary_content(file_path, tab_info, file_size)
        
        return TabInfo(**tab_info)

    async def create_diff_tab_with_title(self, file_path: str, original_content: str, 
                                       modified_content: str, title: str, 
                                       tab_id: Optional[str] = None,
                                       diff_details: Optional[Dict[str, Any]] = None) -> TabInfo:
        """Create a diff tab with a custom title for git timeline comparisons.
        
        Args:
            file_path: Path to the file being compared
            original_content: Original version of the file
            modified_content: Modified version of the file
            title: Custom title for the diff tab
            tab_id: Optional tab ID, will generate UUID if not provided
            diff_details: Optional detailed diff information from diff-match-patch
            
        Returns:
            TabInfo object configured for diff viewing with custom title
        """
        if tab_id is None:
            tab_id = str(uuid.uuid4())
        
        metadata = {'diff_mode': True, 'timeline_diff': True}
        if diff_details:
            metadata['diff_details'] = diff_details
        
        # Cache diff content
        original_hash = generate_content_hash(original_content)
        modified_hash = generate_content_hash(modified_content)
        cache_content(original_hash, original_content)
        cache_content(modified_hash, modified_content)
        
        return TabInfo(
            tab_id=tab_id,
            tab_type='diff',
            title=title,
            file_path=str(file_path),
            content=None,  # Diff tabs don't use regular content
            original_content=original_content,
            modified_content=modified_content,
            original_content_hash=original_hash,
            modified_content_hash=modified_hash,
            is_dirty=False,
            mime_type=None,
            encoding='utf-8',
            metadata=metadata
        )
    
    async def create_untitled_tab(self, content: str = "", language: str = "plaintext", 
                                tab_id: Optional[str] = None) -> TabInfo:
        """Create an untitled tab for new content.
        
        Args:
            content: Initial content for the tab
            language: Programming language for syntax highlighting
            tab_id: Optional tab ID, will generate UUID if not provided
            
        Returns:
            TabInfo object for untitled content
        """
        if tab_id is None:
            tab_id = str(uuid.uuid4())
        
        # Cache untitled content
        content_hash = generate_content_hash(content)
        cache_content(content_hash, content)
        
        return TabInfo(
            tab_id=tab_id,
            tab_type='untitled',
            title="Untitled",
            file_path=None,
            content=content,
            content_hash=content_hash,
            original_content=None,
            modified_content=None,
            is_dirty=bool(content),  # Dirty if has initial content
            mime_type=None,
            encoding='utf-8',
            metadata={'language': language}
        )
    
    async def _load_text_content(self, file_path: Path, tab_info: Dict[str, Any], file_size: int):
        """Load text content from file."""
        if file_size > MAX_TEXT_FILE_SIZE:
            content = f"# File too large to display\n# File: {file_path.name}\n# Size: {self._format_file_size(file_size)}\n# Maximum size for text files: {self._format_file_size(MAX_TEXT_FILE_SIZE)}"
            tab_info['content'] = content
            content_hash = generate_content_hash(content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, content)
            tab_info['metadata']['truncated'] = True
            return
        
        try:
            # Try different encodings
            for encoding in ['utf-8', 'utf-16', 'latin-1', 'cp1252']:
                try:
                    content = file_path.read_text(encoding=encoding)
                    tab_info['content'] = content
                    content_hash = generate_content_hash(content)
                    tab_info['content_hash'] = content_hash
                    cache_content(content_hash, content)
                    tab_info['encoding'] = encoding
                    self.logger.debug(f"Successfully loaded {file_path} with {encoding} encoding")
                    return
                except UnicodeDecodeError:
                    continue
            
            # If all encodings fail, treat as binary
            self.logger.warning(f"Could not decode {file_path} as text, treating as binary")
            await self._load_binary_content(file_path, tab_info, file_size)
            
        except OSError as e:
            self.logger.error(f"Error reading file {file_path}: {e}")
            content = f"# Error reading file\n# {e}"
            tab_info['content'] = content
            content_hash = generate_content_hash(content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, content)
            tab_info['metadata']['error'] = str(e)
    
    async def _load_media_content(self, file_path: Path, tab_info: Dict[str, Any], 
                                file_size: int, mime_type: Optional[str]):
        """Load media content as base64."""
        if file_size > MAX_BINARY_FILE_SIZE:
            content = f"# Media file too large to display\n# File: {file_path.name}\n# Size: {self._format_file_size(file_size)}"
            tab_info['content'] = content
            content_hash = generate_content_hash(content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, content)
            tab_info['metadata']['too_large'] = True
            return
        
        try:
            # Determine tab type based on MIME type
            if mime_type:
                if mime_type.startswith('image/'):
                    tab_info['tab_type'] = 'image'
                elif mime_type.startswith('audio/'):
                    tab_info['tab_type'] = 'audio'
                elif mime_type.startswith('video/'):
                    tab_info['tab_type'] = 'video'
            
            # Read file as binary and encode as base64
            binary_content = file_path.read_bytes()
            base64_content = base64.b64encode(binary_content).decode('ascii')
            
            tab_info['content'] = base64_content
            content_hash = generate_content_hash(base64_content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, base64_content)
            tab_info['encoding'] = 'base64'
            tab_info['metadata']['original_size'] = file_size
            
            self.logger.debug(f"Loaded media file {file_path} as base64 ({file_size} bytes)")
            
        except OSError as e:
            self.logger.error(f"Error reading media file {file_path}: {e}")
            content = f"# Error loading media file\n# {e}"
            tab_info['content'] = content
            content_hash = generate_content_hash(content)
            tab_info['content_hash'] = content_hash
            cache_content(content_hash, content)
            tab_info['metadata']['error'] = str(e)
    
    async def _load_binary_content(self, file_path: Path, tab_info: Dict[str, Any], file_size: int):
        """Handle binary files that can't be displayed."""
        content = f"# Binary file\n# File: {file_path.name}\n# Size: {self._format_file_size(file_size)}\n# Type: {tab_info.get('mime_type', 'Unknown')}\n\n# This file contains binary data and cannot be displayed as text."
        tab_info['content'] = content
        content_hash = generate_content_hash(content)
        tab_info['content_hash'] = content_hash
        cache_content(content_hash, content)
        tab_info['metadata']['binary'] = True
        self.logger.debug(f"Marked {file_path} as binary file")
    
    def _is_text_file(self, file_path: Path, mime_type: Optional[str]) -> bool:
        """Check if a file should be treated as text based on MIME type."""
        if not mime_type:
            return False
        
        return (mime_type.startswith('text/') or 
                mime_type in ['application/json', 'application/xml', 'application/javascript',
                             'application/typescript', 'application/x-python', 'application/x-sh'])
    
    async def _detect_text_file(self, file_path: Path) -> bool:
        """Try to detect if a file is text by sampling the beginning."""
        try:
            # Read first 1024 bytes
            with open(file_path, 'rb') as f:
                sample = f.read(1024)
            
            # Check for null bytes (strong indicator of binary)
            if b'\x00' in sample:
                return False
            
            # Try to decode as UTF-8
            try:
                sample.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
                
        except OSError:
            return False
    
    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 ** 2:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 ** 3:
            return f"{size_bytes / (1024 ** 2):.1f} MB"
        else:
            return f"{size_bytes / (1024 ** 3):.1f} GB"


# Global factory instance
_tab_factory = None

def get_tab_factory() -> TabFactory:
    """Get the global tab factory instance."""
    global _tab_factory
    if _tab_factory is None:
        _tab_factory = TabFactory()
    return _tab_factory