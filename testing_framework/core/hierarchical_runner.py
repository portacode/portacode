"""Hierarchical test runner with dependency management."""

import asyncio
from collections import deque, defaultdict
from typing import List, Dict, Set, Optional, Any
import logging

from .base_test import BaseTest, TestResult
from .runner import TestRunner


class HierarchicalTestRunner(TestRunner):
    """Test runner that handles hierarchical dependencies between tests."""
    
    def __init__(self, base_path: str = ".", output_dir: str = "test_results"):
        super().__init__(base_path, output_dir)
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
        """Sort tests in dependency order using topological sort."""
        # Build dependency graph
        graph = self.build_dependency_graph(tests)
        
        # Calculate in-degrees
        in_degree = defaultdict(int)
        test_map = {test.name: test for test in tests}
        
        for test in tests:
            in_degree[test.name] = len(test.depends_on)
        
        # Find tests with no dependencies
        queue = deque([test for test in tests if in_degree[test.name] == 0])
        result = []
        
        while queue:
            current_test = queue.popleft()
            result.append(current_test)
            
            # Update in-degrees for dependent tests
            for dependent_name in graph.get(current_test.name, []):
                in_degree[dependent_name] -= 1
                if in_degree[dependent_name] == 0:
                    queue.append(test_map[dependent_name])
        
        # Check for circular dependencies
        if len(result) != len(tests):
            remaining = set(test.name for test in tests) - set(test.name for test in result)
            raise ValueError(f"Circular dependency detected among tests: {remaining}")
        
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
            error_msg = f"Test execution failed: {str(e)}"
            self.logger.error(f"Error in test {test.name}: {error_msg}")
            
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