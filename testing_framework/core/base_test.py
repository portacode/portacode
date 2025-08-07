"""Base test class and category definitions."""

import asyncio
import json
import os
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, Optional, List, Set, Callable, Union
from pathlib import Path
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


class TestStats:
    """Simple statistics tracking for tests."""
    
    def __init__(self, test_name: str):
        self.test_name = test_name
        self.stats: Dict[str, Any] = {}
        self.timings: Dict[str, float] = {}
        self._start_times: Dict[str, float] = {}
    
    def start_timer(self, name: str):
        """Start timing an operation."""
        import time
        self._start_times[name] = time.time()
    
    def end_timer(self, name: str) -> float:
        """End timing an operation and return duration in milliseconds."""
        import time
        if name in self._start_times:
            duration = (time.time() - self._start_times[name]) * 1000  # Convert to ms
            self.timings[name] = duration
            del self._start_times[name]
            return duration
        return 0.0
    
    def record_stat(self, name: str, value: Any):
        """Record a statistic."""
        self.stats[name] = value
    
    def get_stats(self) -> Dict[str, Any]:
        """Get all recorded stats and timings."""
        return {
            "stats": self.stats,
            "timings": self.timings
        }


class TestAssert:
    """Simple assertion utilities for tests."""
    
    def __init__(self, test_name: str):
        self.test_name = test_name
        self.failures: List[str] = []
    
    def eq(self, actual: Any, expected: Any, message: str = ""):
        """Assert equality."""
        if actual != expected:
            msg = f"{message}: Expected {expected}, got {actual}" if message else f"Expected {expected}, got {actual}"
            self.failures.append(msg)
        return self
    
    def contains(self, container: Any, item: Any, message: str = ""):
        """Assert item is in container."""
        if item not in container:
            msg = f"{message}: Expected {container} to contain {item}" if message else f"Expected {container} to contain {item}"
            self.failures.append(msg)
        return self
    
    def is_true(self, value: Any, message: str = ""):
        """Assert value is truthy."""
        if not value:
            msg = f"{message}: Expected truthy value, got {value}" if message else f"Expected truthy value, got {value}"
            self.failures.append(msg)
        return self
    
    def is_false(self, value: Any, message: str = ""):
        """Assert value is falsy."""
        if value:
            msg = f"{message}: Expected falsy value, got {value}" if message else f"Expected falsy value, got {value}"
            self.failures.append(msg)
        return self
    
    def status_ok(self, response, message: str = ""):
        """Assert HTTP response is 200."""
        if not response or response.status != 200:
            status = response.status if response else "No response"
            msg = f"{message}: Expected 200, got {status}" if message else f"Expected 200, got {status}"
            self.failures.append(msg)
        return self
    
    def url_contains(self, page, path: str, message: str = ""):
        """Assert URL contains path."""
        if path not in page.url:
            msg = f"{message}: Expected URL to contain '{path}', got '{page.url}'" if message else f"Expected URL to contain '{path}', got '{page.url}'"
            self.failures.append(msg)
        return self
    
    def element_visible(self, page, selector: str, message: str = ""):
        """Assert element is visible (for async use: await assert.element_visible(...))."""
        async def check():
            try:
                visible = await page.is_visible(selector)
                if not visible:
                    msg = f"{message}: Element '{selector}' not visible" if message else f"Element '{selector}' not visible"
                    self.failures.append(msg)
            except:
                msg = f"{message}: Element '{selector}' not found" if message else f"Element '{selector}' not found"
                self.failures.append(msg)
            return self
        return check()
    
    def websocket_message(self, messages: List[Dict], message_type: str, contains: Optional[Dict] = None, message: str = ""):
        """Assert websocket message exists."""
        found = False
        for msg in messages:
            if msg.get("type") == message_type:
                if contains:
                    if all(msg.get(k) == v for k, v in contains.items()):
                        found = True
                        break
                else:
                    found = True
                    break
        
        if not found:
            msg_text = f"{message}: WebSocket message type '{message_type}'" if message else f"WebSocket message type '{message_type}'"
            if contains:
                msg_text += f" with {contains}"
            msg_text += " not found"
            self.failures.append(msg_text)
        return self
    
    def debug_file_contains(self, file_path: str, key: str, expected_value: Any = None, message: str = ""):
        """Assert debug file contains key/value."""
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            if key not in data:
                msg = f"{message}: Key '{key}' not found in {file_path}" if message else f"Key '{key}' not found in {file_path}"
                self.failures.append(msg)
            elif expected_value is not None and data[key] != expected_value:
                msg = f"{message}: Key '{key}' in {file_path} expected {expected_value}, got {data[key]}" if message else f"Key '{key}' in {file_path} expected {expected_value}, got {data[key]}"
                self.failures.append(msg)
        except Exception as e:
            msg = f"{message}: Error reading {file_path}: {e}" if message else f"Error reading {file_path}: {e}"
            self.failures.append(msg)
        return self
    
    def has_failures(self) -> bool:
        """Check if any assertions failed."""
        return len(self.failures) > 0
    
    def get_failure_message(self) -> str:
        """Get formatted failure message."""
        if not self.failures:
            return ""
        return f"Assertions failed: {'; '.join(self.failures)}"


class DebugInspector:
    """Helper for inspecting debug files and CLI state."""
    
    @staticmethod
    def load_client_sessions() -> List[Dict[str, Any]]:
        """Load client_sessions.json."""
        try:
            with open("client_sessions.json", 'r') as f:
                return json.load(f)
        except:
            return []
    
    @staticmethod 
    def load_project_state() -> Dict[str, Any]:
        """Load project_state_debug.json."""
        try:
            with open("project_state_debug.json", 'r') as f:
                return json.load(f)
        except:
            return {}
    
    @staticmethod
    def get_active_sessions() -> List[str]:
        """Get list of active session channel names."""
        sessions = DebugInspector.load_client_sessions()
        if isinstance(sessions, list):
            return [session.get("channel_name", "") for session in sessions if session.get("channel_name")]
        return []
    
    @staticmethod
    def get_session_info(channel_name: str) -> Dict[str, Any]:
        """Get info for specific session by channel name."""
        sessions = DebugInspector.load_client_sessions()
        if isinstance(sessions, list):
            for session in sessions:
                if session.get("channel_name") == channel_name:
                    return session
        return {}
    
    @staticmethod
    def get_project_files() -> List[str]:
        """Get list of project files from state."""
        state = DebugInspector.load_project_state()
        return state.get("files", [])


class BaseTest(ABC):
    """Base class for all tests in the framework."""
    
    def __init__(self, name: str, category: TestCategory = TestCategory.CUSTOM, 
                 description: str = "", tags: Optional[List[str]] = None,
                 depends_on: Optional[List[str]] = None, start_url: Optional[str] = None):
        self.name = name
        self.category = category
        self.description = description
        self.tags = tags or []
        self.depends_on = depends_on or []
        self.start_url = start_url
        self.logger = logging.getLogger(f"test.{self.name}")
        self.cli_manager = None
        self.playwright_manager = None
        
        # Test state tracking
        self._dependency_results: Dict[str, TestResult] = {}
        
    def assert_that(self) -> TestAssert:
        """Get assertion helper."""
        return TestAssert(self.name)
    
    def inspect(self) -> DebugInspector:
        """Get debug inspector."""
        return DebugInspector()
    
    def stats(self) -> TestStats:
        """Get statistics helper."""
        return TestStats(self.name)
        
    @abstractmethod
    async def run(self) -> TestResult:
        """Execute the test and return results."""
        pass
    
    async def setup(self) -> None:
        """Setup method called before test execution."""
        pass
        
    async def navigate_to_start_url(self) -> None:
        """Navigate to the start URL if specified and different from current URL."""
        if not self.start_url or not self.playwright_manager or not self.playwright_manager.page:
            return
            
        current_url = self.playwright_manager.page.url
        
        # Extract path from current URL and start URL for proper comparison
        try:
            from urllib.parse import urlparse
            current_path = urlparse(current_url).path
            start_path = urlparse(self.start_url).path if self.start_url.startswith('http') else self.start_url
            
            # Normalize paths (remove trailing slashes for comparison)
            current_path = current_path.rstrip('/')
            start_path = start_path.rstrip('/')
            
            if current_path == start_path:
                self.logger.debug(f"Already at correct URL: {current_url}")
                return
        except Exception as e:
            self.logger.warning(f"URL comparison failed: {e}, falling back to navigation")
            
        # Construct full URL if start_url is a relative path
        target_url = self.start_url
        if self.start_url.startswith('/') and hasattr(self.playwright_manager, 'base_url'):
            # Extract base URL (protocol + host) and combine with relative path
            base_parts = urlparse(self.playwright_manager.base_url)
            target_url = f"{base_parts.scheme}://{base_parts.netloc}{self.start_url}"
        elif self.start_url.startswith('/'):
            # Fallback: extract base from current URL
            current_parts = urlparse(current_url)
            target_url = f"{current_parts.scheme}://{current_parts.netloc}{self.start_url}"
            
        self.logger.info(f"Navigating from {current_url} to {target_url}")
        await self.playwright_manager.page.goto(target_url)
        
        # Wait for page to be ready
        await self.playwright_manager.page.wait_for_load_state('domcontentloaded')
        
    async def teardown(self) -> None:
        """Teardown method called after test execution."""
        pass
    
    def set_cli_manager(self, cli_manager):
        """Set the CLI manager for this test."""
        self.cli_manager = cli_manager
        
    def set_playwright_manager(self, playwright_manager):
        """Set the Playwright manager for this test."""
        self.playwright_manager = playwright_manager
        
    def set_dependency_result(self, test_name: str, result: TestResult):
        """Set result from dependent test."""
        self._dependency_results[test_name] = result
        
    def get_dependency_result(self, test_name: str) -> Optional[TestResult]:
        """Get result from dependent test."""
        return self._dependency_results.get(test_name)
        
    def all_dependencies_passed(self) -> bool:
        """Check if all dependencies passed."""
        return all(
            self._dependency_results.get(dep_name, TestResult(dep_name, False)).success 
            for dep_name in self.depends_on
        )