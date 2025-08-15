"""Test discovery and categorization system."""

import os
import importlib.util
import inspect
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
import logging

from .base_test import BaseTest, TestCategory


class TestDiscovery:
    """Discovers and categorizes tests from the filesystem."""
    
    def __init__(self, test_directories: Optional[List[str]] = None):
        self.test_directories = test_directories or ["tests", "test_modules"]
        self.logger = logging.getLogger("test_discovery")
        self.logger.setLevel(logging.ERROR)  # Show errors during discovery
        self.discovered_tests: Dict[str, BaseTest] = {}
        
    def discover_tests(self, base_path: str = ".") -> Dict[str, BaseTest]:
        """Discover all test classes in the specified directories."""
        base_path = Path(base_path)
        self.discovered_tests = {}
        
        for test_dir in self.test_directories:
            test_path = base_path / test_dir
            if test_path.exists() and test_path.is_dir():
                self.logger.info(f"Discovering tests in: {test_path}")
                self._discover_in_directory(test_path)
        
        self.logger.info(f"Discovered {len(self.discovered_tests)} tests")
        return self.discovered_tests
    
    def _discover_in_directory(self, directory: Path):
        """Recursively discover tests in a directory."""
        for item in directory.rglob("*.py"):
            if item.name.startswith("test_") or item.name.endswith("_test.py"):
                self._discover_in_file(item)
    
    def _discover_in_file(self, file_path: Path):
        """Discover test classes in a Python file."""
        try:
            # Load the module
            spec = importlib.util.spec_from_file_location("test_module", file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                # Find test classes
                for name, obj in inspect.getmembers(module):
                    if (inspect.isclass(obj) and 
                        issubclass(obj, BaseTest) and 
                        obj is not BaseTest):
                        
                        # Instantiate the test class
                        try:
                            test_instance = obj()
                            self.discovered_tests[test_instance.name] = test_instance
                            self.logger.debug(f"Discovered test: {test_instance.name}")
                        except Exception as e:
                            error_msg = f"Failed to instantiate test {name} in {file_path}: {e}"
                            self.logger.error(error_msg)
                            print(f"❌ {error_msg}")  # Also print to console for immediate visibility
                            
        except Exception as e:
            error_msg = f"Failed to load test file {file_path}: {e}"
            self.logger.error(error_msg)
            print(f"❌ {error_msg}")  # Also print to console for immediate visibility
    
    def get_tests_by_category(self, category: TestCategory) -> List[BaseTest]:
        """Get all tests belonging to a specific category."""
        return [test for test in self.discovered_tests.values() 
                if test.category == category]
    
    def get_tests_by_tags(self, tags: Set[str]) -> List[BaseTest]:
        """Get all tests that have any of the specified tags."""
        return [test for test in self.discovered_tests.values()
                if any(tag in test.tags for tag in tags)]
    
    def get_tests_by_name_pattern(self, pattern: str) -> List[BaseTest]:
        """Get all tests whose names match the pattern."""
        import re
        regex = re.compile(pattern, re.IGNORECASE)
        return [test for test in self.discovered_tests.values()
                if regex.search(test.name)]
    
    def list_all_categories(self) -> Set[TestCategory]:
        """Get all categories found in discovered tests."""
        return {test.category for test in self.discovered_tests.values()}
    
    def list_all_tags(self) -> Set[str]:
        """Get all tags found in discovered tests."""
        all_tags = set()
        for test in self.discovered_tests.values():
            all_tags.update(test.tags)
        return all_tags
    
    def get_test_info(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed information about all discovered tests."""
        info = {}
        for name, test in self.discovered_tests.items():
            info[name] = {
                "name": test.name,
                "category": test.category.value,
                "description": test.description,
                "tags": test.tags,
                "class_name": test.__class__.__name__,
                "module": test.__class__.__module__
            }
        return info