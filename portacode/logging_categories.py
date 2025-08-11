"""
Categorized logging system for Portacode CLI.

This module provides a logging system that allows filtering by categories
to help developers focus on specific aspects of the application during debugging.
"""

from __future__ import annotations

import logging
from typing import Set, Optional
from enum import Enum


class LogCategory(Enum):
    """Available log categories for filtering."""
    CONNECTION = "connection"
    AUTHENTICATION = "auth"
    WEBSOCKET = "websocket"
    TERMINAL = "terminal"
    PROJECT_STATE = "project_state"
    FILE_SYSTEM = "filesystem"
    GIT = "git"
    HANDLERS = "handlers"
    MULTIPLEXER = "mux"
    SYSTEM = "system"
    DEBUG = "debug"


class CategorizedLogger:
    """
    A logger that supports category-based filtering.
    
    This wraps the standard Python logger but adds category support
    for fine-grained filtering during debugging.
    """
    
    def __init__(self, name: str, enabled_categories: Optional[Set[str]] = None):
        self.logger = logging.getLogger(name)
        self.enabled_categories = enabled_categories or set()
        self._all_enabled = len(self.enabled_categories) == 0  # If no categories specified, show all
    
    def _should_log(self, category: LogCategory) -> bool:
        """Check if a log with this category should be output."""
        if self._all_enabled:
            return True
        return category.value in self.enabled_categories
    
    def _format_message(self, msg: str, category: LogCategory) -> str:
        """Format message with category prefix."""
        return f"[{category.value.upper()}] {msg}"
    
    def _parse_args(self, *args) -> tuple[LogCategory, tuple]:
        """Parse arguments to extract category and remaining args."""
        if args and isinstance(args[0], LogCategory):
            return args[0], args[1:]
        else:
            return LogCategory.DEBUG, args
    
    def debug(self, msg: str, *args, **kwargs):
        """Log debug message with optional category."""
        category, remaining_args = self._parse_args(*args)
        if self._should_log(category):
            formatted_msg = self._format_message(msg, category)
            self.logger.debug(formatted_msg, *remaining_args, **kwargs)
    
    def info(self, msg: str, *args, **kwargs):
        """Log info message with optional category."""
        category, remaining_args = self._parse_args(*args)
        if self._should_log(category):
            formatted_msg = self._format_message(msg, category)
            self.logger.info(formatted_msg, *remaining_args, **kwargs)
    
    def warning(self, msg: str, *args, **kwargs):
        """Log warning message with optional category."""
        category, remaining_args = self._parse_args(*args)
        if self._should_log(category):
            formatted_msg = self._format_message(msg, category)
            self.logger.warning(formatted_msg, *remaining_args, **kwargs)
    
    def error(self, msg: str, *args, **kwargs):
        """Log error message with optional category."""
        category, remaining_args = self._parse_args(*args)
        if self._should_log(category):
            formatted_msg = self._format_message(msg, category)
            self.logger.error(formatted_msg, *remaining_args, **kwargs)
    
    def exception(self, msg: str, *args, **kwargs):
        """Log exception message with optional category."""
        category, remaining_args = self._parse_args(*args)
        if self._should_log(category):
            formatted_msg = self._format_message(msg, category)
            self.logger.exception(formatted_msg, *remaining_args, **kwargs)


# Global registry of enabled categories
_enabled_categories: Set[str] = set()


def configure_logging_categories(categories: Set[str]) -> None:
    """Configure which log categories should be enabled globally."""
    global _enabled_categories
    _enabled_categories = categories


def get_categorized_logger(name: str) -> CategorizedLogger:
    """Get a categorized logger instance with current category settings."""
    return CategorizedLogger(name, _enabled_categories)


def list_available_categories() -> list[str]:
    """Return a list of all available log categories."""
    return [category.value for category in LogCategory]


def parse_category_string(category_str: str) -> Set[str]:
    """
    Parse a comma-separated string of categories into a set.
    
    Args:
        category_str: Comma-separated category names (e.g., "connection,auth,git")
    
    Returns:
        Set of valid category names
        
    Raises:
        ValueError: If any category is invalid
    """
    if not category_str.strip():
        return set()
    
    categories = {cat.strip().lower() for cat in category_str.split(',')}
    valid_categories = {cat.value for cat in LogCategory}
    
    invalid = categories - valid_categories
    if invalid:
        raise ValueError(f"Invalid categories: {', '.join(invalid)}. "
                        f"Valid categories: {', '.join(sorted(valid_categories))}")
    
    return categories