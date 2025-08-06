"""
Modular Testing Framework for Portacode

A comprehensive testing framework that supports:
- CLI connection management with background threading
- Playwright-based web automation testing
- Modular test organization with categories
- Selective test execution
- Comprehensive recording and logging
"""

from .core.base_test import BaseTest, TestCategory
from .core.runner import TestRunner
from .core.cli_manager import CLIManager
from .core.playwright_manager import PlaywrightManager

__all__ = ['BaseTest', 'TestCategory', 'TestRunner', 'CLIManager', 'PlaywrightManager']