"""Test file operations: creating and opening a new file."""

from datetime import datetime
from playwright.async_api import expect
from playwright.async_api import Locator
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class FileOperationsTest(BaseTest):
    """Test creating a new file and opening it in the editor."""
    
    def __init__(self):
        super().__init__(
            name="file_operations_test",
            category=TestCategory.INTEGRATION,
            description="Create a new file 'new_file1.py' and open it in the editor",
            tags=["file", "operations", "editor", "creation"],
            depends_on=["navigate_testing_folder_test"]
        )
    
    async def run(self) -> TestResult:
        """Test file creation and opening."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        # Ensure we have access to the navigate_testing_folder_test result
        nav_result = self.get_dependency_result("navigate_testing_folder_test")
        if not nav_result or not nav_result.success:
            return TestResult(self.name, False, "Required dependency navigate_testing_folder_test failed")
        
        # Start timing for new file creation
        stats.start_timer("new_file_creation")
        
        # Look for the "New File" button - it could have different selectors
        new_file_button = page.locator('button[title="New File"], .new-file-btn, button:has-text("New File")')
        
        # Wait for the new file button to be visible
        await new_file_button.first.wait_for(timeout=10000)
        
        # Set up dialog handler for JavaScript prompt() before clicking the button
        dialog_handled = False
        filename_to_enter = "new_file1.py"
        
        async def handle_dialog(dialog):
            nonlocal dialog_handled
            
            # Accept the prompt with our filename
            await dialog.accept(filename_to_enter)
            dialog_handled = True
        
        # Register the dialog handler
        page.on("dialog", handle_dialog)
        
        # Click the new file button (this should trigger the prompt)
        await new_file_button.first.click()
        
        # Wait a moment for the dialog to be handled
        await page.wait_for_timeout(1000)
        
        # Check if dialog was handled
        if not dialog_handled:
            # If no dialog appeared, maybe it's a DOM-based modal instead
            # Try the original DOM-based approach as fallback
            try:
                file_name_input = page.locator('input[placeholder*="file"], input[type="text"]:visible, .file-name-input')
                await file_name_input.first.wait_for(timeout=3000)
                await file_name_input.first.fill(filename_to_enter)
                await file_name_input.first.press("Enter")
            except:
                # Last resort - try modal buttons
                try:
                    confirm_button = page.locator('button:has-text("OK"), button:has-text("Create"), button:has-text("Confirm"), .confirm-btn')
                    await confirm_button.first.click()
                except:
                    raise Exception("Could not handle file creation dialog - neither JavaScript prompt nor DOM modal found")
        
        # Remove the dialog handler
        page.remove_listener("dialog", handle_dialog)
        
        file_creation_time = stats.end_timer("new_file_creation")
        stats.record_stat("file_creation_time_ms", file_creation_time)
        
        # Verify the file was created - look for it in the file explorer
        stats.start_timer("file_verification")
        
        # Wait a moment for the file to appear in the explorer (it takes around a second)
        await page.wait_for_timeout(2000)
        
        # Look for the new file using the exact LitElement structure
        # From the file-explorer.js, files are rendered with this structure:
        # <div class="file-item-wrapper"><div class="file-item"><div class="file-content"><span class="file-name">
        
        # Target the .file-item that contains our filename
        new_file_item = page.locator('.file-item:has(.file-name:text("new_file1.py"))')
        
        # Wait for the file to appear
        await new_file_item.first.wait_for(timeout=15000)
        
        file_count = await new_file_item.count()
        assert_that.is_true(file_count > 0, "new_file1.py should appear as .file-item in file explorer")
        
        stats.record_stat("file_selector_used", ".file-item:has(.file-name:text(\"new_file1.py\"))")
        
        file_verification_time = stats.end_timer("file_verification")
        stats.record_stat("file_verification_time_ms", file_verification_time)
        
        # Verify the file exists
        file_count = await new_file_item.count()
        assert_that.is_true(file_count > 0, "new_file1.py should appear in file explorer")
        
        # Take a screenshot to see the file before clicking
        await self.playwright_manager.take_screenshot("before_clicking_file")
        
        # Click on the .file-item element to trigger handleFileClick -> selectFile -> openFile
        stats.start_timer("file_opening")
        
        # Single click should be enough on desktop to open file (based on file-explorer.js logic)
        await new_file_item.first.click()
        stats.record_stat("open_action", "single_click_on_file_item")
        
        # Wait for the file to open in the editor
        await page.wait_for_timeout(3000)
        
        # First, verify the tab opened properly 
        try:
            file_tab = page.locator('[role="tab"]:has-text("new_file1.py"), .tab:has-text("new_file1.py"), .editor-tab:has-text("new_file1.py")')
            await file_tab.first.wait_for(timeout=5000)
            tab_count = await file_tab.count()
            stats.record_stat("file_tab_found", tab_count > 0)
            assert_that.is_true(tab_count > 0, "File tab should be visible")
        except:
            stats.record_stat("file_tab_found", False)
            assert_that.is_true(False, "File tab should be visible after clicking file")
        
        # Verify we're not stuck in loading state
        loading_placeholder = page.locator('.loading-placeholder:has-text("Loading new_file1.py")')
        loading_error_placeholder = page.locator('.error-placeholder')
        
        # Wait for loading to finish (max 15 seconds)
        loading_timeout = False
        try:
            # Wait for loading placeholder to disappear or timeout
            await loading_placeholder.wait_for(state='hidden', timeout=15000)
        except:
            loading_count = await loading_placeholder.count()
            error_count = await loading_error_placeholder.count()
            if loading_count > 0:
                loading_timeout = True
                stats.record_stat("loading_timeout", True)
                # Take screenshot of stuck loading state
                await self.playwright_manager.take_screenshot("stuck_loading_state")
            elif error_count > 0:
                error_text = await loading_error_placeholder.inner_text()
                assert_that.is_true(False, f"Error loading file: {error_text}")
        
        assert_that.is_true(not loading_timeout, "File should finish loading within 15 seconds (not stuck in loading state)")
        
        # Wait for the ACE editor to load using the correct LitElement selectors
        editor_selectors = [
            'ace-editor',                    # The custom element
            '.ace-editor-container',         # The container inside the element  
            '.ace_editor',                   # The actual ACE editor instance
            '[class*="ace"]'                # Fallback for any ACE-related classes
        ]
        
        editor_found = False
        for selector in editor_selectors:
            try:
                editor_element = page.locator(selector)
                await editor_element.first.wait_for(timeout=5000)
                editor_count = await editor_element.count()
                if editor_count > 0:
                    stats.record_stat("editor_selector_used", selector)
                    editor_found = True
                    break
            except:
                continue
        
        assert_that.is_true(editor_found, "ACE editor should be visible and loaded after file opens")
        
        # Verify the ACE editor is interactive (not just visible but actually functional)
        if editor_found:
            try:
                # Try to focus the ACE editor and verify it's interactive
                ace_editor = page.locator('ace-editor')
                await ace_editor.first.click()
                
                # Check if ACE editor cursor is visible (indicates it's loaded and ready)
                ace_cursor = page.locator('.ace_cursor')
                await ace_cursor.first.wait_for(timeout=3000)
                cursor_count = await ace_cursor.count()
                stats.record_stat("ace_cursor_found", cursor_count > 0)
                assert_that.is_true(cursor_count > 0, "ACE editor cursor should be visible (indicating editor is fully loaded and interactive)")
                
            except Exception as e:
                stats.record_stat("ace_cursor_found", False)
                stats.record_stat("ace_cursor_error", str(e))
                assert_that.is_true(False, f"ACE editor should be interactive but failed: {e}")
        
        # Wait a bit more for the editor to fully stabilize
        await page.wait_for_timeout(1000)
        
        file_opening_time = stats.end_timer("file_opening")
        stats.record_stat("file_opening_time_ms", file_opening_time)
        
        # Take a screenshot using the playwright manager's proper method
        stats.start_timer("screenshot")
        screenshot_path = await self.playwright_manager.take_screenshot("ace_editor_with_file")
        stats.record_stat("screenshot_path", str(screenshot_path))
        screenshot_time = stats.end_timer("screenshot")
        stats.record_stat("screenshot_time_ms", screenshot_time)
        
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        total_time = file_creation_time + file_verification_time + file_opening_time
        
        return TestResult(
            self.name,
            True,
            f"Successfully created and opened new_file1.py in ACE editor in {total_time:.1f}ms",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for file operations test."""
        pass
    
    async def teardown(self):
        """Teardown for file operations test."""
        pass