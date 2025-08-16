"""Utility functions for project state management.

This module contains shared utility functions used across the project state
management system, including tab key generation and other helper functions.
"""

import hashlib
import uuid


def generate_tab_key(tab_type: str, file_path: str, **kwargs) -> str:
    """Generate a unique key for a tab.
    
    Args:
        tab_type: Type of tab ('file', 'diff', 'untitled', etc.)
        file_path: Path to the file
        **kwargs: Additional parameters for diff tabs (from_ref, to_ref, from_hash, to_hash)
    
    Returns:
        Unique string key for the tab
    """
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


def generate_content_hash(content: str) -> str:
    """Generate SHA-256 hash of content for caching.
    
    Args:
        content: The string content to hash
        
    Returns:
        SHA-256 hash prefixed with 'sha256:'
    """
    if content is None:
        return None
    
    return "sha256:" + hashlib.sha256(content.encode('utf-8')).hexdigest()