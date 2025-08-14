"""Hierarchical test runner with dependency management."""

import asyncio
from collections import deque, defaultdict
from typing import List, Dict, Set, Optional, Any
import logging
import traceback
import sys
from pathlib import Path

from .base_test import BaseTest, TestResult
from .runner import TestRunner


class HierarchicalTestRunner(TestRunner):
    """Test runner that handles hierarchical dependencies between tests."""
    
    def __init__(self, base_path: str = ".", output_dir: str = "test_results", clear_results: bool = False):
        super().__init__(base_path, output_dir, clear_results)
        self.dependency_graph: Dict[str, List[str]] = {}
        self.test_states: Dict[str, TestResult] = {}
        self.pending_teardowns: Dict[str, BaseTest] = {}  # Tests waiting for teardown
        self.dependents_map: Dict[str, Set[str]] = {}  # Map test_name -> set of dependent test names
        
    def build_dependency_graph(self, tests: List[BaseTest]) -> Dict[str, List[str]]:
        """Build dependency graph from tests."""
        graph = defaultdict(list)
        dependents_map = defaultdict(set)
        
        for test in tests:
            for dependency in test.depends_on:
                graph[dependency].append(test.name)
                dependents_map[dependency].add(test.name)
        
        self.dependents_map = dict(dependents_map)        
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
    
    def get_all_dependents(self, test_name: str) -> Set[str]:
        """Get all direct and indirect dependents of a test."""
        all_dependents = set()
        to_check = [test_name]
        
        while to_check:
            current = to_check.pop(0)
            if current in self.dependents_map:
                for dependent in self.dependents_map[current]:
                    if dependent not in all_dependents:
                        all_dependents.add(dependent)
                        to_check.append(dependent)
        
        return all_dependents
    
    def all_dependents_completed(self, test_name: str, completed_tests: Set[str], failed_tests: Set[str]) -> bool:
        """Check if all dependents of a test have completed (passed or failed)."""
        all_dependents = self.get_all_dependents(test_name)
        if not all_dependents:
            return True  # No dependents, can teardown immediately
            
        # Check if all dependents have completed
        for dependent in all_dependents:
            if dependent not in completed_tests and dependent not in failed_tests:
                return False
        return True
    
    async def run_pending_teardowns(self, completed_tests: Set[str], failed_tests: Set[str]):
        """Execute teardowns for tests whose dependents have all completed."""
        tests_to_teardown = []
        
        # Find tests ready for teardown
        for test_name, test_instance in list(self.pending_teardowns.items()):
            if self.all_dependents_completed(test_name, completed_tests, failed_tests):
                tests_to_teardown.append((test_name, test_instance))
        
        # Execute teardowns
        for test_name, test_instance in tests_to_teardown:
            try:
                self.logger.info(f"Running delayed teardown for {test_name}")
                await test_instance.teardown()
                del self.pending_teardowns[test_name]
            except Exception as e:
                self.logger.error(f"Error during delayed teardown for {test_name}: {e}")
    
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
    
    async def run_all_tests(self, progress_callback=None) -> Dict[str, Any]:
        """Run all discovered tests with full dependency resolution."""
        # Get all available tests
        all_tests = self.discovery.discover_tests(str(self.base_path))
        tests_list = list(all_tests.values())
        
        # Use full dependency resolution for all tests
        # This ensures if test A depends on B, B will be included even if not explicitly requested
        tests_with_deps = self.resolve_dependencies(tests_list)
        
        return await self.run_tests(tests_with_deps, progress_callback)
    
    async def run_tests(self, tests: List[BaseTest], progress_callback=None) -> Dict[str, Any]:
        """Run tests with dependency resolution."""
        if not tests:
            return {"success": False, "message": "No tests found", "results": []}
        
        # Sort tests by dependencies and build dependency maps
        try:
            ordered_tests = self.topological_sort(tests)
            self.dependency_graph = self.build_dependency_graph(tests)
        except ValueError as e:
            return {"success": False, "message": str(e), "results": []}
        
        self.logger.info(f"Running {len(ordered_tests)} tests in dependency order")
        
        # Track execution
        completed_tests: Set[str] = set()
        failed_tests: Set[str] = set()
        self.test_states = {}
        self.pending_teardowns = {}
        
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
                # Ensure trace is saved immediately for failed tests
                if hasattr(self, '_shared_playwright_manager'):
                    try:
                        trace_saved = await self._shared_playwright_manager.ensure_trace_saved()
                        if trace_saved:
                            self.logger.info(f"Trace saved for failed test: {test.name}")
                        else:
                            self.logger.warning(f"Failed to save trace for failed test: {test.name}")
                    except Exception as e:
                        self.logger.error(f"Error saving trace for failed test {test.name}: {e}")
            
            # Run any pending teardowns that are now ready
            await self.run_pending_teardowns(completed_tests, failed_tests)
            
            if progress_callback:
                progress_callback('complete', test, i + 1, len(ordered_tests), result)
        
        # Run any remaining pending teardowns
        if self.pending_teardowns:
            self.logger.info(f"Running final teardowns for {len(self.pending_teardowns)} remaining tests")
            for test_name, test_instance in self.pending_teardowns.items():
                try:
                    self.logger.info(f"Running final teardown for {test_name}")
                    await test_instance.teardown()
                except Exception as e:
                    self.logger.error(f"Error during final teardown for {test_name}: {e}")
            self.pending_teardowns.clear()
        
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
            
            # Check if this test has dependents that are still running
            if test.name in self.dependents_map and self.dependents_map[test.name]:
                # This test has dependents, delay teardown until all dependents complete
                self.logger.info(f"Test {test.name} has dependents {self.dependents_map[test.name]}, delaying teardown")
                self.pending_teardowns[test.name] = test
            else:
                # No dependents or dependents already completed, run teardown immediately
                self.logger.info(f"Running immediate teardown for {test.name}")
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
        from pathlib import Path
        
        # Get recordings directory path and cleanup playwright
        recordings_dir = None
        if hasattr(self, '_shared_playwright_manager'):
            try:
                # Save the recordings directory path before cleanup
                recordings_dir = Path(self._shared_playwright_manager.recordings_dir)
                
                # Now cleanup which will save the trace
                await self._shared_playwright_manager.cleanup()
            except Exception as e:
                self.logger.error(f"Error cleaning up shared playwright manager: {e}")
        
        # Find trace path after cleanup
        trace_path = None
        if recordings_dir and recordings_dir.exists():
            # Look for trace.zip in the recordings directory or its subdirectories
            for trace_file in recordings_dir.rglob("trace.zip"):
                trace_path = trace_file
                break
        
        # Open trace viewer if any tests failed (including skipped tests for debugging)
        failed_tests = [result for result in self.results if not result.success]
        
        if failed_tests and trace_path and trace_path.exists():
            try:
                self.logger.info(f"Opening trace viewer for {len(failed_tests)} failed tests...")
                import subprocess
                subprocess.Popen([
                    'npx', 'playwright', 'show-trace', 
                    '--host', '0.0.0.0', 
                    '--port', '9323',
                    str(trace_path)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"\n🔍 Trace viewer opened at: http://0.0.0.0:9323")
                print(f"   Trace file: {trace_path}")
            except Exception as trace_error:
                self.logger.warning(f"Could not open trace viewer: {trace_error}")
        
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
    
    async def _wait_for_trace_file(self, recording_dir: Path, timeout: float = 30.0) -> Optional[Path]:
        """Wait for trace.zip file to be available with proper timeout."""
        import time
        
        start_time = time.time()
        check_interval = 0.1  # Check every 100ms for responsiveness
        last_log_time = start_time
        
        while time.time() - start_time < timeout:
            # First check direct path
            direct_trace = recording_dir / "trace.zip"
            if direct_trace.exists() and direct_trace.stat().st_size > 0:
                self.logger.info(f"Found trace file at direct path: {direct_trace}")
                return direct_trace
                
            # Look for trace.zip in subdirectories (for shared sessions)
            for subdir in recording_dir.glob("*/"):
                potential_trace = subdir / "trace.zip"
                if potential_trace.exists() and potential_trace.stat().st_size > 0:
                    self.logger.info(f"Found trace file in subdirectory: {potential_trace}")
                    return potential_trace
            
            # Log progress every 5 seconds to avoid spam
            if time.time() - last_log_time >= 5.0:
                elapsed = time.time() - start_time
                self.logger.info(f"Still waiting for trace file... ({elapsed:.1f}s elapsed)")
                last_log_time = time.time()
                
            await asyncio.sleep(check_interval)
        
        return None
    
    async def _open_trace_on_failure(self, test_name: str, playwright_manager) -> None:
        """Open Playwright trace in browser when test fails."""
        try:
            # Get the trace file path from playwright manager
            recording_dir = Path(playwright_manager.recordings_dir)
            self.logger.info(f"Looking for trace in recording directory: {recording_dir}")
            
            # Wait for trace file to be available with proper timeout
            trace_file = await self._wait_for_trace_file(recording_dir, timeout=30.0)
            
            if trace_file and trace_file.exists():
                # Verify the file is not empty and not currently being written
                file_size = trace_file.stat().st_size
                if file_size == 0:
                    self.logger.warning(f"Trace file {trace_file} exists but is empty")
                    return
                
                # Wait a moment to ensure file writing is complete
                await asyncio.sleep(0.5)
                
                # Open trace viewer in browser
                self.logger.info(f"Opening trace viewer for failed test: {test_name}")
                self.logger.info(f"Trace file size: {file_size} bytes")
                
                # Use Playwright's trace viewer
                import subprocess
                
                # Try to open with playwright show-trace command with host/port options
                try:
                    # Run playwright show-trace with host and port for remote access
                    subprocess.Popen([
                        'npx', 'playwright', 'show-trace', 
                        '--host', '0.0.0.0', 
                        '--port', '9323',
                        str(trace_file)
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.logger.info(f"Trace viewer opened for {test_name} at http://0.0.0.0:9323")
                    print(f"\n🔍 Trace viewer opened at: http://0.0.0.0:9323")
                    print(f"   Trace file: {trace_file} ({file_size} bytes)")
                except (FileNotFoundError, subprocess.SubprocessError) as e:
                    self.logger.warning(f"Failed to open trace viewer with network access: {e}")
                    # Fallback: try without host/port options
                    try:
                        subprocess.Popen([
                            'npx', 'playwright', 'show-trace', str(trace_file)
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        self.logger.info(f"Trace viewer opened for {test_name} (local)")
                        print(f"\n🔍 Trace viewer opened locally")
                        print(f"   Trace file: {trace_file} ({file_size} bytes)")
                    except (FileNotFoundError, subprocess.SubprocessError):
                        # Final fallback: open trace directory in file manager
                        self.logger.warning("Playwright trace viewer not available, opening trace directory")
                        import os
                        if os.name == 'nt':  # Windows
                            os.startfile(str(recording_dir))
                        elif os.name == 'posix':  # Linux/Mac
                            subprocess.run(['xdg-open', str(recording_dir)], check=False)
            else:
                # Debug: show what we're looking for
                self.logger.error(f"No trace file found for {test_name} after 30 second timeout.")
                self.logger.error(f"Searched in recording directory: {recording_dir}")
                if recording_dir.exists():
                    self.logger.error(f"Directory contents:")
                    for item in recording_dir.rglob("*"):
                        if item.is_file():
                            self.logger.error(f"  - {item} ({item.stat().st_size} bytes)")
                        else:
                            self.logger.error(f"  - {item}/ (directory)")
                else:
                    self.logger.error(f"Recording directory does not exist: {recording_dir}")
                
        except Exception as e:
            self.logger.error(f"Failed to open trace for {test_name}: {e}")
            import traceback
            self.logger.error(f"Full traceback: {traceback.format_exc()}")