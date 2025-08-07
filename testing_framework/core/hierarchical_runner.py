"""Hierarchical test runner with dependency management."""

import asyncio
from collections import deque, defaultdict
from typing import List, Dict, Set, Optional, Any
import logging
import traceback
import sys

from .base_test import BaseTest, TestResult
from .runner import TestRunner


class HierarchicalTestRunner(TestRunner):
    """Test runner that handles hierarchical dependencies between tests."""
    
    def __init__(self, base_path: str = ".", output_dir: str = "test_results", clear_results: bool = False):
        super().__init__(base_path, output_dir, clear_results)
        self.dependency_graph: Dict[str, List[str]] = {}
        self.test_states: Dict[str, TestResult] = {}
        
    def build_dependency_graph(self, tests: List[BaseTest]) -> Dict[str, List[str]]:
        """Build dependency graph from tests."""
        graph = defaultdict(list)
        
        for test in tests:
            for dependency in test.depends_on:
                graph[dependency].append(test.name)
                
        return dict(graph)
    
    def topological_sort(self, tests: List[BaseTest]) -> List[BaseTest]:
        """Sort tests using depth-first traversal that prioritizes children after parents."""
        test_map = {test.name: test for test in tests}
        graph = self.build_dependency_graph(tests)
        
        visited = set()
        temp_visited = set()
        result = []
        
        def visit_depth_first(test_name: str):
            if test_name in temp_visited:
                raise ValueError(f"Circular dependency detected involving test: {test_name}")
            if test_name in visited or test_name not in test_map:
                return
            
            temp_visited.add(test_name)
            
            # Visit all dependencies first
            for dep_name in test_map[test_name].depends_on:
                visit_depth_first(dep_name)
            
            temp_visited.remove(test_name)
            visited.add(test_name)
            result.append(test_map[test_name])
        
        # Custom ordering: prioritize depth-first by visiting tests that have the deepest dependency chains first
        def get_dependency_depth(test: BaseTest) -> int:
            """Calculate the maximum depth of dependencies for a test."""
            if not test.depends_on:
                return 0
            max_depth = 0
            for dep_name in test.depends_on:
                if dep_name in test_map:
                    max_depth = max(max_depth, get_dependency_depth(test_map[dep_name]) + 1)
            return max_depth
        
        # Sort all tests by dependency depth (deepest first) then by name for stability
        sorted_tests = sorted(tests, key=lambda t: (-get_dependency_depth(t), t.name))
        
        # Visit tests in the calculated order
        for test in sorted_tests:
            visit_depth_first(test.name)
        
        return result
    
    def resolve_dependencies(self, requested_tests: List[BaseTest]) -> List[BaseTest]:
        """Resolve and include all dependencies for requested tests."""
        all_tests = self.discovery.discover_tests(str(self.base_path))
        test_map = {test.name: test for test in all_tests.values()}
        
        needed_tests = set()
        to_process = [test.name for test in requested_tests]
        
        while to_process:
            current_name = to_process.pop(0)
            if current_name in needed_tests:
                continue
                
            needed_tests.add(current_name)
            
            # Add dependencies if they exist
            if current_name in test_map:
                current_test = test_map[current_name]
                for dep_name in current_test.depends_on:
                    if dep_name not in needed_tests:
                        to_process.append(dep_name)
        
        # Return tests in dependency order
        return [test_map[name] for name in needed_tests if name in test_map]
    
    async def run_tests_by_names(self, test_names: List[str], progress_callback=None) -> Dict[str, Any]:
        """Run specific tests by name, automatically including dependencies."""
        all_tests = self.discovery.discover_tests(str(self.base_path))
        requested_tests = [all_tests[name] for name in test_names if name in all_tests]
        
        if len(requested_tests) != len(test_names):
            found_names = {test.name for test in requested_tests}
            missing = set(test_names) - found_names
            self.logger.warning(f"Tests not found: {missing}")
        
        # Resolve dependencies
        tests_with_deps = self.resolve_dependencies(requested_tests)
        
        return await self.run_tests(tests_with_deps, progress_callback)
    
    def check_login_requirement(self, test: BaseTest, completed_tests: Set[str]) -> bool:
        """Check if login requirement is satisfied."""
        if not test.requires_login:
            return True
        
        # Look for any completed test that handles login
        for completed_name in completed_tests:
            if "login" in completed_name.lower() or "auth" in completed_name.lower():
                return True
        return False
    
    def check_ide_requirement(self, test: BaseTest, completed_tests: Set[str]) -> bool:
        """Check if IDE requirement is satisfied."""  
        if not test.requires_ide:
            return True
            
        # Look for any completed test that launches IDE
        for completed_name in completed_tests:
            if "ide" in completed_name.lower() or "launch" in completed_name.lower():
                return True
        return False
    
    async def run_tests(self, tests: List[BaseTest], progress_callback=None) -> Dict[str, Any]:
        """Run tests with dependency resolution."""
        if not tests:
            return {"success": False, "message": "No tests found", "results": []}
        
        # Sort tests by dependencies
        try:
            ordered_tests = self.topological_sort(tests)
        except ValueError as e:
            return {"success": False, "message": str(e), "results": []}
        
        self.logger.info(f"Running {len(ordered_tests)} tests in dependency order")
        
        # Track execution
        completed_tests: Set[str] = set()
        failed_tests: Set[str] = set()
        self.test_states = {}
        
        # Use the parent class setup
        await self._setup_test_run(ordered_tests, progress_callback)
        
        for i, test in enumerate(ordered_tests):
            if progress_callback:
                progress_callback('start', test, i + 1, len(ordered_tests))
            
            # Check if all dependencies passed
            skip_reason = None
            
            # Check explicit dependencies
            for dep_name in test.depends_on:
                if dep_name in failed_tests:
                    skip_reason = f"Dependency '{dep_name}' failed"
                    break
                elif dep_name not in completed_tests:
                    skip_reason = f"Dependency '{dep_name}' not completed"
                    break
            
            # Check implicit requirements
            if not skip_reason and not self.check_login_requirement(test, completed_tests):
                skip_reason = "Login requirement not satisfied"
            
            if not skip_reason and not self.check_ide_requirement(test, completed_tests):
                skip_reason = "IDE requirement not satisfied"
            
            if skip_reason:
                # Skip this test
                result = TestResult(
                    test.name, 
                    False, 
                    f"Skipped: {skip_reason}",
                    0.0
                )
                self.results.append(result)
                self.test_states[test.name] = result
                failed_tests.add(test.name)
                
                if progress_callback:
                    progress_callback('complete', test, i + 1, len(ordered_tests), result)
                continue
            
            # Pass dependency results to the test
            for dep_name in test.depends_on:
                if dep_name in self.test_states:
                    test.set_dependency_result(dep_name, self.test_states[dep_name])
            
            # Run the test
            result = await self._run_single_test_with_managers(test)
            self.results.append(result)
            self.test_states[test.name] = result
            
            if result.success:
                completed_tests.add(test.name)
                self.logger.info(f"✓ Test '{test.name}' passed")
            else:
                failed_tests.add(test.name)
                self.logger.error(f"✗ Test '{test.name}' failed: {result.message}")
            
            if progress_callback:
                progress_callback('complete', test, i + 1, len(ordered_tests), result)
        
        # Generate final report
        return await self._finalize_test_run()
    
    async def _setup_test_run(self, tests: List[BaseTest], progress_callback):
        """Setup for test run (extracted from parent class)."""
        import time
        from datetime import datetime
        from pathlib import Path
        
        self.start_time = time.time()
        self.results = []
        self.progress_callback = progress_callback
        
        # Setup logging for this test run
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = self.output_dir / f"run_{run_id}"
        self.run_dir.mkdir(exist_ok=True)
        
        # Setup file logging
        log_file = self.run_dir / "test_run.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        
        # Add file handler to all loggers
        logging.getLogger().addHandler(file_handler)
        self.file_handler = file_handler
    
    async def _run_single_test_with_managers(self, test: BaseTest) -> TestResult:
        """Run single test with shared managers."""
        import time
        from .shared_cli_manager import TestCLIProxy
        from .playwright_manager import PlaywrightManager
        
        test_start = time.time()
        
        try:
            # Setup shared CLI manager for this test
            cli_manager = TestCLIProxy(test.name, str(self.run_dir / "cli_logs"))
            
            # Setup shared playwright manager (reuse existing session if available)
            if not hasattr(self, '_shared_playwright_manager'):
                self._shared_playwright_manager = PlaywrightManager("shared_session", str(self.run_dir / "recordings"))
                # Start shared session once
                playwright_started = await self._shared_playwright_manager.start_session()
                if not playwright_started:
                    return TestResult(
                        test.name, False,
                        "Failed to start shared Playwright session", 
                        time.time() - test_start
                    )
            
            # Set managers on test
            test.set_cli_manager(cli_manager)
            test.set_playwright_manager(self._shared_playwright_manager)
            
            # Ensure CLI connection with --debug flag
            cli_connected = await cli_manager.connect(debug=True)
            if not cli_connected:
                return TestResult(
                    test.name, False, 
                    "Failed to establish CLI connection",
                    time.time() - test_start
                )
            
            # Run test setup
            self.logger.info(f"Running setup for {test.name}")
            await test.setup()
            
            # Navigate to start URL if needed
            await test.navigate_to_start_url()
            
            # Run the actual test
            self.logger.info(f"Executing test logic for {test.name}")
            result = await test.run()
            
            # Update duration
            result.duration = time.time() - test_start
            
            # Run test teardown (but don't cleanup shared playwright)
            self.logger.info(f"Running teardown for {test.name}")
            await test.teardown()
            
            return result
            
        except Exception as e:
            # Get detailed error information
            exc_type, exc_value, exc_traceback = sys.exc_info()
            
            # Extract the most relevant line from traceback (user's test code)
            tb_lines = traceback.format_tb(exc_traceback)
            user_code_line = None
            
            for line in tb_lines:
                if 'test_modules/' in line or 'run(self)' in line:
                    user_code_line = line.strip()
                    break
            
            # Create detailed error message
            error_details = [f"Test execution failed: {str(e)}"]
            
            if user_code_line:
                error_details.append(f"Location: {user_code_line}")
            
            # Add exception type
            if exc_type:
                error_details.append(f"Exception type: {exc_type.__name__}")
            
            # Add full traceback to logs but keep UI message concise
            full_traceback = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            self.logger.error(f"Full traceback for {test.name}:\n{full_traceback}")
            
            error_msg = '\n'.join(error_details)
            
            # Auto-open trace in browser for failed tests (with delay)
            try:
                if hasattr(self, '_shared_playwright_manager'):
                    # Wait a moment for trace file to be written
                    import asyncio
                    await asyncio.sleep(1)
                    await self._open_trace_on_failure(test.name, self._shared_playwright_manager)
            except Exception as trace_error:
                self.logger.warning(f"Could not open trace for {test.name}: {trace_error}")
            
            return TestResult(
                test.name, False, error_msg,
                time.time() - test_start
            )
            
        finally:
            # Don't disconnect shared CLI connection, just log completion
            try:
                await cli_manager.disconnect()  # This won't actually disconnect in shared mode
            except Exception as e:
                self.logger.error(f"Error during cleanup for {test.name}: {e}")
    
    async def _finalize_test_run(self) -> Dict[str, Any]:
        """Finalize test run and generate reports."""
        import time
        
        # Cleanup shared playwright manager
        if hasattr(self, '_shared_playwright_manager'):
            try:
                await self._shared_playwright_manager.cleanup()
            except Exception as e:
                self.logger.error(f"Error cleaning up shared playwright manager: {e}")
        
        # Cleanup logging
        try:
            logging.getLogger().removeHandler(self.file_handler)
            self.file_handler.close()
        except:
            pass
        
        self.end_time = time.time()
        
        # Generate summary report
        summary = await self._generate_summary_report(self.run_dir)
        
        self.logger.info(f"Hierarchical test run completed. Results saved to: {self.run_dir}")
        return summary