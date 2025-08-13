"""Test git status expandable section in file explorer."""

import os
import time
from pathlib import Path
from playwright.async_api import expect
from playwright.async_api import Locator
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

# Global test folder path
TESTING_FOLDER_PATH = "/home/menas/testing_folder"


class GitStatusUITest(BaseTest):
    """Test the git status expandable section functionality in file explorer."""
    
    def __init__(self):
        super().__init__(
            name="git_status_ui_test",
            category=TestCategory.INTEGRATION,
            description="Test git status expandable section in file explorer UI",
            tags=["git", "ui", "file-explorer", "expandable"],
            depends_on=["device_online_test"],
            start_url="/dashboard/"
        )
        
    
    async def run(self) -> TestResult:
        """Test git status UI functionality."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        # Navigate to testing_folder project (same as navigate_testing_folder_test)
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        await device_card.wait_for()
        
        editor_button = device_card.get_by_text("Editor")
        await editor_button.wait_for()
        await editor_button.click()
        
        # Wait for the project selector modal and select testing_folder
        await page.wait_for_selector("#projectSelectorModal.show", timeout=10000)
        await page.wait_for_selector(".item-list .section-header", timeout=10000)
        
        testing_folder_item = page.locator('.item.project').filter(has_text="testing_folder")
        testing_folder_count = await testing_folder_item.count()
        
        if testing_folder_count > 0:
            await testing_folder_item.first.click()
        else:
            # Fallback to first project if testing_folder not found
            project_items = page.locator('.item.project')
            await project_items.first.click()
        
        # Wait for file explorer to load
        await page.wait_for_timeout(3000)
        
        # Step 1: Check if git branch section is visible
        stats.start_timer("git_section_detection")
        
        # Look for the git branch info section with our new class
        git_branch_section = page.locator(".git-branch-info")
        git_section_count = await git_branch_section.count()
        
        stats.record_stat("git_branch_sections_found", git_section_count)
        
        if git_section_count == 0:
            assert_that.is_true(False, "No git branch section found in file explorer")
            return TestResult(self.name, False, "Git branch section not detected")
        
        # Step 2: Check if git branch section looks clickable (has cursor pointer)
        git_section_cursor = await git_branch_section.evaluate("el => getComputedStyle(el).cursor")
        assert_that.eq(git_section_cursor, "pointer", "Git branch section should have cursor pointer")
        stats.record_stat("git_section_cursor", git_section_cursor)
        
        # Step 3: Check initial state (should not be expanded)
        is_expanded_initially = await git_branch_section.evaluate("el => el.classList.contains('expanded')")
        assert_that.is_false(is_expanded_initially, "Git section should not be expanded initially")
        stats.record_stat("initially_expanded", is_expanded_initially)
        
        # Step 4: Click the git branch section to expand it
        stats.start_timer("git_section_click")
        await git_branch_section.click()
        await page.wait_for_timeout(500)  # Wait for animation
        
        git_click_time = stats.end_timer("git_section_click")
        stats.record_stat("git_click_time_ms", git_click_time)
        
        # Step 5: Check if section is now expanded
        is_expanded_after_click = await git_branch_section.evaluate("el => el.classList.contains('expanded')")
        assert_that.is_true(is_expanded_after_click, "Git section should be expanded after click")
        stats.record_stat("expanded_after_click", is_expanded_after_click)
        
        # Step 6: Check if the detailed git section is now visible
        git_detailed_section = page.locator(".git-detailed-section")
        detailed_section_count = await git_detailed_section.count()
        assert_that.is_true(detailed_section_count > 0, "Git detailed section should be visible when expanded")
        stats.record_stat("detailed_sections_found", detailed_section_count)
        
        # Step 7: Check console for our debug logs to see what data is being accessed
        console_logs = []
        page.on("console", lambda msg: console_logs.append(f"{msg.type}: {msg.text}"))
        
        # Step 8: Check content of detailed section
        if detailed_section_count > 0:
            detailed_text = await git_detailed_section.text_content()
            stats.record_stat("detailed_section_text", detailed_text[:200])  # First 200 chars
            
            # Check if it shows "No detailed git status available"
            has_no_status_message = "No detailed git status available" in detailed_text
            stats.record_stat("shows_no_status_message", has_no_status_message)
            
            # Check for specific git status elements - look for untracked files section
            untracked_section_title = page.locator(".git-section-title").filter(has_text="Untracked Files")
            untracked_count = await untracked_section_title.count()
            stats.record_stat("untracked_section_found", untracked_count > 0)
            
            staged_section_title = page.locator(".git-section-title").filter(has_text="Staged Changes")
            staged_count = await staged_section_title.count()
            stats.record_stat("staged_section_found", staged_count > 0)
            
            # If we have untracked files, test the action buttons
            if untracked_count > 0:
                # Check for stage-all button in untracked files section
                stage_all_btn = page.locator(".git-group-btn.stage-all")
                stage_all_count = await stage_all_btn.count()
                stats.record_stat("stage_all_button_found", stage_all_count > 0)
                
                # Check for individual file action buttons
                git_action_btns = page.locator(".git-action-btn")
                action_btn_count = await git_action_btns.count()
                stats.record_stat("individual_action_buttons_found", action_btn_count)
                
                # Test expanding/collapsing untracked files section
                await untracked_section_title.click()
                await page.wait_for_timeout(300)
                
                # Check if section is expanded (should show files)
                git_file_items = page.locator(".git-file-item")
                file_item_count = await git_file_items.count()
                stats.record_stat("git_file_items_shown", file_item_count)
                
                # Test staging a file if we have files and a stage button
                if file_item_count > 0 and stage_all_count > 0:
                    # Click stage all button
                    await stage_all_btn.first.click()
                    await page.wait_for_timeout(1000)  # Wait for git operation
                    
                    # After staging, check if staged changes section appears
                    await page.wait_for_timeout(500)
                    staged_section_after_stage = await page.locator(".git-section-title").filter(has_text="Staged Changes").count()
                    stats.record_stat("staged_section_after_staging", staged_section_after_stage > 0)
                    
                    # Check if commit form appears when we have staged files
                    commit_form = page.locator(".git-commit-form")
                    commit_form_count = await commit_form.count()
                    stats.record_stat("commit_form_visible", commit_form_count > 0)
                    
                    # Test commit form functionality if it's visible
                    if commit_form_count > 0:
                        # Check for commit input field
                        commit_input = page.locator(".git-commit-input")
                        commit_input_count = await commit_input.count()
                        stats.record_stat("commit_input_found", commit_input_count > 0)
                        
                        # Check for commit button
                        commit_btn = page.locator(".git-commit-btn").filter(has_text="Commit")
                        commit_btn_count = await commit_btn.count()
                        stats.record_stat("commit_button_found", commit_btn_count > 0)
                        
                        if commit_input_count > 0:
                            # Test typing a commit message
                            test_commit_message = "Test commit from automated test"
                            await commit_input.fill(test_commit_message)
                            await page.wait_for_timeout(300)
                            
                            # Check if button becomes enabled after typing message
                            if commit_btn_count > 0:
                                is_disabled = await commit_btn.is_disabled()
                                stats.record_stat("commit_button_enabled_with_message", not is_disabled)
                                
                                # Actually test the commit functionality
                                if not is_disabled:
                                    # Listen for console messages to see if commit is called
                                    commit_console_logs = []
                                    def capture_commit_logs(msg):
                                        if "commit" in msg.text.lower():
                                            commit_console_logs.append(msg.text)
                                    
                                    page.on("console", capture_commit_logs)
                                    
                                    # Click the commit button
                                    await commit_btn.click()
                                    await page.wait_for_timeout(2000)  # Wait for commit to process
                                    
                                    # Check if any commit-related logs were generated
                                    stats.record_stat("commit_logs_generated", len(commit_console_logs))
                                    stats.record_stat("commit_log_messages", commit_console_logs[:3])  # First 3 messages
                                    
                                    # Check if message was cleared after successful commit
                                    input_value_after_commit = await commit_input.input_value()
                                    stats.record_stat("commit_message_cleared_after_commit", input_value_after_commit == "")
                            
                            # If message wasn't cleared by commit, clear manually (using Cancel button)
                            input_value = await commit_input.input_value()
                            if input_value:
                                cancel_btn = page.locator(".git-commit-cancel")
                                cancel_count = await cancel_btn.count()
                                if cancel_count > 0:
                                    await cancel_btn.click()
                                    await page.wait_for_timeout(200)
                                    
                                    # Check if message is cleared
                                    input_value_after_cancel = await commit_input.input_value()
                                    stats.record_stat("commit_message_cleared_after_cancel", input_value_after_cancel == "")
        
        # Step 9: Test collapse functionality
        await git_branch_section.click()
        await page.wait_for_timeout(500)
        
        is_collapsed = await git_branch_section.evaluate("el => !el.classList.contains('expanded')")
        assert_that.is_true(is_collapsed, "Git section should collapse when clicked again")
        
        detailed_section_after_collapse = await git_detailed_section.count()
        assert_that.eq(detailed_section_after_collapse, 0, "Detailed section should be hidden when collapsed")
        
        detection_time = stats.end_timer("git_section_detection")
        stats.record_stat("git_detection_time_ms", detection_time)
        
        # Collect relevant console logs for debugging
        git_related_logs = [log for log in console_logs if "git" in log.lower() or "detailed" in log.lower()]
        stats.record_stat("console_logs_count", len(console_logs))
        stats.record_stat("git_related_logs", git_related_logs[:10])  # First 10 relevant logs
        
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        return TestResult(
            self.name, 
            True, 
            f"Git status UI functionality tested successfully",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for git status UI test - ensure testing folder has git repo with untracked file."""
        try:
            # Ensure the testing folder exists
            os.makedirs(TESTING_FOLDER_PATH, exist_ok=True)
            
            # Clean out any existing content
            import shutil
            for item in os.listdir(TESTING_FOLDER_PATH):
                item_path = os.path.join(TESTING_FOLDER_PATH, item)
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            
            # Initialize git repo and create a test file to ensure we have git status
            os.chdir(TESTING_FOLDER_PATH)
            os.system("git init")
            os.system("git config user.name 'Test User'")
            os.system("git config user.email 'test@example.com'")
            
            # Create a test file (untracked)
            with open(os.path.join(TESTING_FOLDER_PATH, "test.py"), "w") as f:
                f.write('print("hello world")\\n')
                    
        except Exception as e:
            print(f"❌ Setup failed: {e}")
            raise Exception(f"Failed to set up git test environment: {e}")
    
    
    async def teardown(self):
        """Teardown for git status UI test."""
        try:
            if os.path.exists(TESTING_FOLDER_PATH):
                import shutil
                # Clean up all content
                for item in os.listdir(TESTING_FOLDER_PATH):
                    item_path = os.path.join(TESTING_FOLDER_PATH, item)
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
        except Exception as e:
            print(f"⚠️ Cleanup warning: {e}")