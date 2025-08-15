"""Test runner with selective execution and comprehensive reporting."""

import asyncio
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
import json
import shutil
import traceback
import webbrowser
import os
import sys
from datetime import datetime

from .base_test import BaseTest, TestResult, TestCategory
from .test_discovery import TestDiscovery
from .cli_manager import CLIManager
from .shared_cli_manager import SharedCLIManager, TestCLIProxy
from .playwright_manager import PlaywrightManager


class TestRunner:
    """Main test runner that orchestrates test execution."""
    
    def __init__(self, base_path: str = ".", output_dir: str = "test_results", clear_results: bool = False):
        self.base_path = Path(base_path)
        self.output_dir = Path(output_dir)
        
        # Clear results directory if requested
        if clear_results and self.output_dir.exists():
            shutil.rmtree(self.output_dir)
            
        self.output_dir.mkdir(exist_ok=True)
        
        self.discovery = TestDiscovery()
        self.logger = logging.getLogger("test_runner")
        
        # Results tracking
        self.results: List[TestResult] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        
    async def run_all_tests(self, progress_callback=None) -> Dict[str, Any]:
        """Run all discovered tests."""
        tests = self.discovery.discover_tests(str(self.base_path))
        return await self.run_tests(list(tests.values()), progress_callback)
    
    async def run_tests_by_category(self, category: TestCategory, progress_callback=None) -> Dict[str, Any]:
        """Run all tests in a specific category."""
        self.discovery.discover_tests(str(self.base_path))
        tests = self.discovery.get_tests_by_category(category)
        return await self.run_tests(tests, progress_callback)
    
    async def run_tests_by_tags(self, tags: Set[str], progress_callback=None) -> Dict[str, Any]:
        """Run all tests that have any of the specified tags."""
        self.discovery.discover_tests(str(self.base_path))
        tests = self.discovery.get_tests_by_tags(tags)
        return await self.run_tests(tests, progress_callback)
    
    async def run_tests_by_names(self, test_names: List[str], progress_callback=None) -> Dict[str, Any]:
        """Run specific tests by name."""
        all_tests = self.discovery.discover_tests(str(self.base_path))
        tests = [all_tests[name] for name in test_names if name in all_tests]
        
        if len(tests) != len(test_names):
            found_names = {test.name for test in tests}
            missing = set(test_names) - found_names
            # Only log to file, not console
            pass
            
        return await self.run_tests(tests, progress_callback)
    
    async def run_tests_by_pattern(self, pattern: str, progress_callback=None) -> Dict[str, Any]:
        """Run tests whose names match the pattern."""
        self.discovery.discover_tests(str(self.base_path))
        tests = self.discovery.get_tests_by_name_pattern(pattern)
        return await self.run_tests(tests, progress_callback)
    
    async def run_tests(self, tests: List[BaseTest], progress_callback=None) -> Dict[str, Any]:
        """Run a list of tests with full orchestration."""
        if not tests:
            return {"success": False, "message": "No tests found", "results": []}
        
        self.start_time = time.time()
        self.results = []
        self.progress_callback = progress_callback
        
        # Setup logging for this test run
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.output_dir / f"run_{run_id}"
        run_dir.mkdir(exist_ok=True)
        
        # Setup file logging
        log_file = run_dir / "test_run.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        
        # Add file handler to all loggers
        logging.getLogger().addHandler(file_handler)
        
        # We'll establish the CLI connection when the first test runs
        
        try:
            for i, test in enumerate(tests):
                # Notify progress
                if self.progress_callback:
                    self.progress_callback('start', test, i + 1, len(tests))
                
                # Setup managers for this test - use shared CLI
                cli_manager = TestCLIProxy(test.name, str(run_dir / "cli_logs"))
                playwright_manager = PlaywrightManager(test.name, str(run_dir / "recordings"))
                
                test.set_cli_manager(cli_manager)
                test.set_playwright_manager(playwright_manager)
                
                # Run the test
                result = await self._run_single_test(test, cli_manager, playwright_manager)
                self.results.append(result)
                
                # Notify completion
                if self.progress_callback:
                    self.progress_callback('complete', test, i + 1, len(tests), result)
        
        finally:
            # Remove file handler
            logging.getLogger().removeHandler(file_handler)
            file_handler.close()
        
        self.end_time = time.time()
        
        # Generate summary report
        summary = await self._generate_summary_report(run_dir)
        
        self.logger.info(f"Test run completed. Results saved to: {run_dir}")
        return summary
    
    async def _run_single_test(self, test: BaseTest, 
                             cli_manager, 
                             playwright_manager: PlaywrightManager) -> TestResult:
        """Run a single test with full setup and teardown."""
        test_start = time.time()
        
        try:
            # Step 1: Ensure CLI connection (will reuse existing if available)
            cli_connected = await cli_manager.connect()
            if not cli_connected:
                return TestResult(
                    test.name, False, 
                    "Failed to establish CLI connection",
                    time.time() - test_start
                )
            
            # Step 2: Start Playwright session
            self.logger.info(f"Starting Playwright session for {test.name}")
            playwright_started = await playwright_manager.start_session()
            
            if not playwright_started:
                return TestResult(
                    test.name, False,
                    "Failed to start Playwright session", 
                    time.time() - test_start
                )
            
            # Step 3: Run test setup
            self.logger.info(f"Running setup for {test.name}")
            await test.setup()
            
            # Step 3.5: Navigate to start URL if needed
            await test.navigate_to_start_url()
            
            # Step 4: Run the actual test
            self.logger.info(f"Executing test logic for {test.name}")
            result = await test.run()
            
            # Update duration
            result.duration = time.time() - test_start
            
            # Step 5: Run test teardown
            self.logger.info(f"Running teardown for {test.name}")
            await test.teardown()
            
            return result
            
        except Exception as e:
            # Get detailed error information
            exc_type, exc_value, exc_traceback = sys.exc_info()
            
            # Extract the most relevant line from traceback (user's test code)
            tb_lines = traceback.format_tb(exc_traceback)
            user_code_line = None
            
            # Look for the LAST occurrence in user test code (most specific failure point)
            for line in reversed(tb_lines):
                if 'test_modules/' in line and '.py' in line:
                    user_code_line = line.strip()
                    break
            
            # If no test_modules line found, look for any line with async context
            if not user_code_line:
                for line in reversed(tb_lines):
                    if 'await' in line or 'async' in line:
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
            
            # Don't open trace here - let hierarchical runner handle it at the end
            
            return TestResult(
                test.name, False, error_msg,
                time.time() - test_start
            )
            
        finally:
            # Cleanup
            try:
                await playwright_manager.cleanup()
                await cli_manager.disconnect()  # This won't actually disconnect shared connection
            except Exception as e:
                self.logger.error(f"Error during cleanup for {test.name}: {e}")
    
    async def _open_trace_on_failure(self, test_name: str, playwright_manager) -> None:
        """Open Playwright trace in browser when test fails."""
        try:
            # Get the trace file path from playwright manager
            recording_dir = Path(playwright_manager.recordings_dir)
            self.logger.info(f"Looking for trace in recording directory: {recording_dir}")
            
            # Retry logic to wait for trace file to be written
            trace_file = None
            max_retries = 10
            retry_delay = 0.5
            
            for attempt in range(max_retries):
                # First check direct path
                direct_trace = recording_dir / "trace.zip"
                self.logger.info(f"Attempt {attempt + 1}: Checking direct trace {direct_trace} (exists: {direct_trace.exists()})")
                if direct_trace.exists():
                    trace_file = direct_trace
                    break
                    
                # Look for trace.zip in subdirectories (for shared sessions)
                for subdir in recording_dir.glob("*/"):
                    potential_trace = subdir / "trace.zip"
                    self.logger.info(f"Attempt {attempt + 1}: Checking subdir trace {potential_trace} (exists: {potential_trace.exists()})")
                    if potential_trace.exists():
                        trace_file = potential_trace
                        break
                        
                if trace_file:
                    break
                    
                if attempt < max_retries - 1:
                    self.logger.info(f"Trace file not found yet (attempt {attempt + 1}/{max_retries}), waiting...")
                    await asyncio.sleep(retry_delay)
            
            if trace_file:
                self.logger.info(f"Found trace file after {attempt + 1} attempts: {trace_file}")
            
            if trace_file and trace_file.exists():
                # Open trace viewer in browser
                self.logger.info(f"Opening trace viewer for failed test: {test_name}")
                
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
                    print(f"   Trace file: {trace_file}")
                except (FileNotFoundError, subprocess.SubprocessError) as e:
                    # Fallback: try without host/port options
                    try:
                        subprocess.Popen([
                            'npx', 'playwright', 'show-trace', str(trace_file)
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        self.logger.info(f"Trace viewer opened for {test_name} (local)")
                    except (FileNotFoundError, subprocess.SubprocessError):
                        # Final fallback: open trace directory in file manager
                        self.logger.warning("Playwright trace viewer not available, opening trace directory")
                        if os.name == 'nt':  # Windows
                            os.startfile(str(recording_dir))
                        elif os.name == 'posix':  # Linux/Mac
                            subprocess.run(['xdg-open', str(recording_dir)], check=False)
            else:
                # Debug: show what we're looking for
                self.logger.warning(f"No trace file found for {test_name}. Searched in:")
                self.logger.warning(f"  - Direct path: {recording_dir / 'trace.zip'}")
                for subdir in recording_dir.glob("*/"):
                    self.logger.warning(f"  - Subdir path: {subdir / 'trace.zip'} (exists: {(subdir / 'trace.zip').exists()})")
                
        except Exception as e:
            self.logger.error(f"Failed to open trace for {test_name}: {e}")
    
    async def _generate_summary_report(self, run_dir: Path) -> Dict[str, Any]:
        """Generate a comprehensive summary report."""
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r.success)
        failed_tests = total_tests - passed_tests
        
        total_duration = self.end_time - self.start_time if self.start_time and self.end_time else 0
        
        summary = {
            "run_info": {
                "start_time": datetime.fromtimestamp(self.start_time).isoformat() if self.start_time else None,
                "end_time": datetime.fromtimestamp(self.end_time).isoformat() if self.end_time else None,
                "duration": total_duration,
                "run_directory": str(run_dir)
            },
            "statistics": {
                "total_tests": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
                "success_rate": (passed_tests / total_tests * 100) if total_tests > 0 else 0
            },
            "results": [
                {
                    "test_name": result.test_name,
                    "success": result.success,
                    "message": result.message,
                    "duration": result.duration,
                    "artifacts": result.artifacts
                }
                for result in self.results
            ]
        }
        
        # Save summary to file
        summary_file = run_dir / "summary.json"
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)
        
        # Generate HTML report
        await self._generate_html_report(run_dir, summary)
        
        self.logger.info(f"Summary report saved to: {summary_file}")
        return summary
    
    async def _generate_html_report(self, run_dir: Path, summary: Dict[str, Any]):
        """Generate an HTML report for easy viewing."""
        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Test Run Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .header {{ background: #f5f5f5; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
        .stats {{ display: flex; gap: 20px; margin: 20px 0; }}
        .stat {{ background: #e9ecef; padding: 15px; border-radius: 5px; text-align: center; flex: 1; }}
        .passed {{ background: #d4edda; color: #155724; }}
        .failed {{ background: #f8d7da; color: #721c24; }}
        .test-result {{ margin: 10px 0; padding: 15px; border-radius: 5px; border-left: 4px solid; }}
        .test-passed {{ background: #d4edda; border-color: #28a745; }}
        .test-failed {{ background: #f8d7da; border-color: #dc3545; }}
        .duration {{ color: #6c757d; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Test Run Report</h1>
        <p><strong>Start Time:</strong> {summary['run_info']['start_time']}</p>
        <p><strong>Duration:</strong> {summary['run_info']['duration']:.2f} seconds</p>
        <p><strong>Run Directory:</strong> {summary['run_info']['run_directory']}</p>
    </div>
    
    <div class="stats">
        <div class="stat">
            <h3>{summary['statistics']['total_tests']}</h3>
            <p>Total Tests</p>
        </div>
        <div class="stat passed">
            <h3>{summary['statistics']['passed']}</h3>
            <p>Passed</p>
        </div>
        <div class="stat failed">
            <h3>{summary['statistics']['failed']}</h3>
            <p>Failed</p>
        </div>
        <div class="stat">
            <h3>{summary['statistics']['success_rate']:.1f}%</h3>
            <p>Success Rate</p>
        </div>
    </div>
    
    <h2>Test Results</h2>
"""
        
        for result in summary['results']:
            status_class = "test-passed" if result['success'] else "test-failed"
            status_text = "PASSED" if result['success'] else "FAILED"
            
            html_content += f"""
    <div class="test-result {status_class}">
        <h3>{result['test_name']} - {status_text}</h3>
        <p class="duration">Duration: {result['duration']:.2f}s</p>
        {f"<p><strong>Message:</strong> {result['message']}</p>" if result['message'] else ""}
    </div>
"""
        
        html_content += """
</body>
</html>
"""
        
        html_file = run_dir / "report.html"
        with open(html_file, 'w') as f:
            f.write(html_content)
            
        self.logger.info(f"HTML report saved to: {html_file}")
    
    def list_available_tests(self) -> Dict[str, Any]:
        """List all available tests with their information."""
        tests = self.discovery.discover_tests(str(self.base_path))
        return {
            "total_tests": len(tests),
            "categories": list(self.discovery.list_all_categories()),
            "tags": list(self.discovery.list_all_tags()),
            "tests": self.discovery.get_test_info()
        }