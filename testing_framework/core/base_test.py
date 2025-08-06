"""Base test class and category definitions."""

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, Optional, List
import logging


class TestCategory(Enum):
    """Test categories for organization and selective execution."""
    SMOKE = "smoke"
    INTEGRATION = "integration"
    UI = "ui"
    API = "api"
    PERFORMANCE = "performance"
    SECURITY = "security"
    CUSTOM = "custom"


class TestResult:
    """Represents the result of a test execution."""
    
    def __init__(self, test_name: str, success: bool, message: str = "", 
                 duration: float = 0.0, artifacts: Optional[Dict[str, Any]] = None):
        self.test_name = test_name
        self.success = success
        self.message = message
        self.duration = duration
        self.artifacts = artifacts or {}


class BaseTest(ABC):
    """Base class for all tests in the framework."""
    
    def __init__(self, name: str, category: TestCategory = TestCategory.CUSTOM, 
                 description: str = "", tags: Optional[List[str]] = None):
        self.name = name
        self.category = category
        self.description = description
        self.tags = tags or []
        self.logger = logging.getLogger(f"test.{self.name}")
        self.cli_manager = None
        self.playwright_manager = None
        
    @abstractmethod
    async def run(self) -> TestResult:
        """Execute the test and return results."""
        pass
    
    async def setup(self) -> None:
        """Setup method called before test execution."""
        pass
        
    async def teardown(self) -> None:
        """Teardown method called after test execution."""
        pass
    
    def set_cli_manager(self, cli_manager):
        """Set the CLI manager for this test."""
        self.cli_manager = cli_manager
        
    def set_playwright_manager(self, playwright_manager):
        """Set the Playwright manager for this test."""
        self.playwright_manager = playwright_manager