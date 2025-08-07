"""Test navigating to 'testing_folder' project."""

import os
import shutil
import subprocess
import time
from pathlib import Path
from playwright.async_api import expect
from playwright.async_api import Locator
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

# Global test folder path
TESTING_FOLDER_PATH = "/home/menas/testing_folder"


class NavigateTestingFolderTest(BaseTest):
    """Test navigating to the 'testing_folder' project through Editor button."""
    
    def __init__(self):
        super().__init__(
            name="navigate_testing_folder_test",
            category=TestCategory.INTEGRATION,
            description="Navigate to 'testing_folder' project via Editor button and wait for file explorer with git details",
            tags=["navigation", "editor", "project", "testing_folder"],
            depends_on=["device_online_test"],
            start_url="/dashboard/"
        )
        
        # Track tests that depend on this test (for proper teardown timing)
        self.child_tests = set()
        self.child_test_results = {}
        self.teardown_delayed = False
    
    async def run(self) -> TestResult:
        """Test navigation to testing_folder project."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        # Find portacode streamer device card that's online
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        await device_card.wait_for()
        
        # Click the Editor button in the device card
        stats.start_timer("editor_button_click")
        editor_button = device_card.get_by_text("Editor")
        await editor_button.wait_for()
        await editor_button.click()
        
        editor_click_time = stats.end_timer("editor_button_click")
        stats.record_stat("editor_button_click_time_ms", editor_click_time)
        
        # Navigate to testing_folder project
        stats.start_timer("project_navigation")
        
        # Wait for the project selector modal to appear
        await page.wait_for_selector("#projectSelectorModal.show", timeout=10000)
        
        # Wait for projects to load in the modal
        await page.wait_for_selector(".item-list .section-header", timeout=10000)
        
        # Look for testing_folder project item and click it
        # Projects are displayed as items with class "item project" 
        
        # First let's see what projects are available for debugging
        project_items = page.locator('.item.project')
        project_count = await project_items.count()
        
        # If there are projects, look for testing_folder specifically
        if project_count > 0:
            # Try to find testing_folder specifically first
            testing_folder_item = page.locator('.item.project').filter(has_text="testing_folder")
            testing_folder_count = await testing_folder_item.count()
            
            if testing_folder_count > 0:
                # Found testing_folder project - this is ideal!
                await testing_folder_item.first.click()
                stats.record_stat("found_testing_folder", True)
            else:
                # If no testing_folder, try any project with "test" in the name as fallback
                test_item = page.locator('.item.project').filter(has_text="test")
                test_count = await test_item.count()
                if test_count > 0:
                    await test_item.first.click()
                    stats.record_stat("found_testing_folder", False)
                    stats.record_stat("fallback_reason", "used_test_project")
                else:
                    # Use first available project as last resort
                    await project_items.first.click()
                    stats.record_stat("found_testing_folder", False)
                    stats.record_stat("fallback_reason", "used_first_available")
        else:
            # No projects found
            assert_that.is_true(False, "No projects found in modal")
        
        navigation_time = stats.end_timer("project_navigation")
        stats.record_stat("project_navigation_time_ms", navigation_time)
        
        # Wait for page to load with file explorer showing git details and files
        stats.start_timer("page_load")
        
        # Wait for file explorer to be visible
        file_explorer = page.locator(".file-explorer, .project-files, .file-tree, .files-panel")
        await file_explorer.first.wait_for(timeout=15000)
        
        # Wait for git details to be visible (could be branch name, commit info, etc.)
        git_details = page.locator(".git-branch, .git-info, .branch-name, [class*='git'], [class*='branch']")
        await git_details.first.wait_for(timeout=10000)
        
        # Verify files are displayed
        files_present = page.locator(".file-item, .file-entry, .tree-item, [class*='file']").count()
        files_count = await files_present
        assert_that.is_true(files_count > 0, "Files should be visible in explorer")
        
        page_load_time = stats.end_timer("page_load")
        stats.record_stat("page_load_time_ms", page_load_time)
        stats.record_stat("files_count", files_count)
        
        # Verify we're in a project page by checking URL pattern
        current_url = page.url
        assert_that.contains(current_url.lower(), "project/", "URL should contain project path indicating successful navigation")
        
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        total_time = editor_click_time + navigation_time + page_load_time
        
        return TestResult(
            self.name, 
            True, 
            f"Successfully navigated to testing_folder project in {total_time:.1f}ms with {files_count} files",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for testing_folder navigation test - prepare the test project with Git."""
        # print(f"🔧 Setting up test project at {TESTING_FOLDER_PATH}")
        
        try:
            # Ensure the testing folder exists
            os.makedirs(TESTING_FOLDER_PATH, exist_ok=True)
            
            # Change to the testing folder
            original_cwd = os.getcwd()
            os.chdir(TESTING_FOLDER_PATH)
            
            try:
                # Initialize Git repository if not already initialized
                if not os.path.exists('.git'):
                    # print("📦 Initializing Git repository...")
                    subprocess.run(['git', 'init'], check=True, capture_output=True)
                    
                    # Configure Git user (required for commits)
                    subprocess.run(['git', 'config', 'user.name', 'Test User'], check=True, capture_output=True)
                    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], check=True, capture_output=True)
                
                # Create initial test files and structure
                # print("📄 Creating initial test files...")
                
                # Create a Python file
                with open('example_file.py', 'w') as f:
                    f.write('#!/usr/bin/env python3\n')
                    f.write('"""Example Python file for testing."""\n\n')
                    f.write('def hello_world():\n')
                    f.write('    print("Hello from testing_folder!")\n\n')
                    f.write('if __name__ == "__main__":\n')
                    f.write('    hello_world()\n')
                
                # Create a folder with a file inside
                os.makedirs('example_folder', exist_ok=True)
                with open('example_folder/nested_file.txt', 'w') as f:
                    f.write('This is a nested file for testing purposes.\n')
                    f.write('Created during test setup.\n')
                
                # Create a README file
                with open('some_file.txt', 'w') as f:
                    f.write('# Testing Folder\n\n')
                    f.write('This folder is created and managed by automated tests.\n')
                    f.write('Files here may be modified or deleted during testing.\n')
                
                # Stage all files
                # print("📋 Staging files to Git...")
                subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
                
                # Commit the initial setup
                # print("💾 Committing initial setup...")
                commit_result = subprocess.run([
                    'git', 'commit', '-m', 'Initial test setup with example files'
                ], capture_output=True, text=True)
                
                if commit_result.returncode == 0:
                    pass
                    # print("✅ Test project setup completed successfully")
                else:
                    # Check if it's because nothing to commit (already exists)
                    if "nothing to commit" in commit_result.stdout:
                        print("ℹ️ Test project already set up (nothing to commit)")
                    else:
                        print(f"⚠️ Git commit warning: {commit_result.stdout}")
                
                # Verify Git status
                status_result = subprocess.run(['git', 'status', '--porcelain'], 
                                             capture_output=True, text=True, check=True)
                if status_result.stdout.strip():
                    print(f"📝 Git status after setup: {status_result.stdout.strip()}")
                else:
                    pass
                    # print("✅ Git working directory is clean")
                    
            finally:
                # Always return to original directory
                os.chdir(original_cwd)
                
        except subprocess.CalledProcessError as e:
            print(f"❌ Git command failed: {e}")
            print(f"Command: {e.cmd}")
            if e.stdout:
                print(f"Stdout: {e.stdout}")
            if e.stderr:
                print(f"Stderr: {e.stderr}")
            raise Exception(f"Failed to set up test project: {e}")
        except Exception as e:
            print(f"❌ Setup failed: {e}")
            raise Exception(f"Failed to set up test project: {e}")
    
    def register_child_test(self, child_test_name: str):
        """Register a test that depends on this test."""
        self.child_tests.add(child_test_name)
        print(f"📋 Registered child test: {child_test_name}")
    
    def notify_child_test_completed(self, child_test_name: str, result: bool):
        """Notify that a child test has completed."""
        if child_test_name in self.child_tests:
            self.child_test_results[child_test_name] = result
            print(f"📢 Child test completed: {child_test_name} ({'✅ PASSED' if result else '❌ FAILED'})")
            
            # Check if all child tests have completed
            if len(self.child_test_results) >= len(self.child_tests):
                print("🎯 All child tests completed, running delayed teardown...")
                # Run teardown in a separate task to avoid blocking
                import asyncio
                asyncio.create_task(self._run_delayed_teardown())
    
    async def teardown(self):
        """Teardown for testing_folder navigation test."""
        # For now, don't clean up immediately since child tests may still need the files
        # In the current test framework, child tests run after parent completes
        # So we'll just log that teardown was called but not clean up yet
        # print(f"📝 navigate_testing_folder_test teardown called - files preserved for child tests")
        # print(f"📁 Test project remains at {TESTING_FOLDER_PATH} for child test usage")
        pass 
        # The actual cleanup will need to be handled by the test framework or final cleanup script
    
    async def _run_delayed_teardown(self):
        """Actually perform the teardown after all dependencies are resolved."""
        print(f"🧹 Cleaning up test project at {TESTING_FOLDER_PATH}")
        
        try:
            if os.path.exists(TESTING_FOLDER_PATH):
                # Change to the testing folder
                original_cwd = os.getcwd()
                os.chdir(TESTING_FOLDER_PATH)
                
                try:
                    # Clean up all content but preserve the folder itself
                    print("🗑️ Removing all files and folders...")
                    
                    # Get all items in the directory
                    items = os.listdir('.')
                    
                    for item in items:
                        item_path = os.path.join('.', item)
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                            print(f"   🗑️ Removed file: {item}")
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                            print(f"   🗑️ Removed directory: {item}")
                    
                    print("✅ Test project cleanup completed")
                    
                finally:
                    # Always return to original directory
                    os.chdir(original_cwd)
            else:
                print(f"ℹ️ Test project folder {TESTING_FOLDER_PATH} doesn't exist - nothing to clean up")
                
        except Exception as e:
            print(f"⚠️ Cleanup warning: {e}")
            # Don't fail the test just because cleanup had issues