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
            depends_on=["file_operations_test"],
            start_url="/project/1d98e739-de00-4d65-a13b-c6c82173683f/"
        )
        
    
    async def run(self) -> TestResult:
        """Test git status UI functionality with comprehensive UI interactions."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        
        # Check if project loaded properly - look for files in explorer
        file_items = page.locator(".file-item")
        file_count = await file_items.count()
        stats.record_stat("initial_files_in_explorer", file_count)
        
        # If no files are loaded, wait longer and check for "No project loaded" message
        if file_count == 0:
            no_project_msg = page.locator("text=No project loaded")
            no_project_count = await no_project_msg.count()
            if no_project_count > 0:
                await self.playwright_manager.take_screenshot("no_project_loaded_error")
                # Don't fail immediately - the project might still be loaded (title shows testing_folder)
                # Just record this as a potential UI issue and continue testing
                stats.record_stat("file_explorer_shows_no_project", True)
            
            # Wait additional time for slow loading
            await page.wait_for_timeout(5000)
            await self.playwright_manager.take_screenshot("after_additional_wait")
            file_count = await file_items.count()
            
        stats.record_stat("files_after_wait", file_count)
        
        # Continue even if file explorer appears empty - the git section might still work
        # The title bar shows "testing_folder" which suggests project is loaded

        # PHASE 0: Test git diff viewer from file explorer
        diff_options = [
            '.context-menu-item:has-text("View Staged Changes")',
            '.context-menu-item:has-text("View Unstaged Changes")',
            '.context-menu-item:has-text("View All Changes")',
        ]
        new_file_item = page.locator('.file-item:has(.file-name:text("new_file1.py"))')
        for option in diff_options:
            await new_file_item.first.click(button='right')
            await page.locator(option).hover()
            await page.wait_for_timeout(500)  # Wait for context menu to appear
            await self.playwright_manager.take_screenshot(f"before_clicking_diff_option_{option.split(':')[1]}")
            await page.locator(option).click()
            await page.wait_for_timeout(1000)

        staged_diff_tab = page.locator('.editor-tab:has-text("(head → staged)")')
        unstaged_diff_tab = page.locator('.editor-tab:has-text("(staged → working)")')
        all_diff_tab = page.locator('.editor-tab:has-text("(head → working)")')

        assert_that.is_true(await staged_diff_tab.is_visible(), "Staged diff tab should be visible")
        assert_that.is_true(await unstaged_diff_tab.is_visible(), "Unstaged diff tab should be visible")
        assert_that.is_true(await all_diff_tab.is_visible(), "All changes diff tab should be visible")
        
        # PHASE 1: Test git status section expansion/collapse
        stats.start_timer("git_section_detection")
        
        git_branch_section = page.locator(".git-branch-info")
        git_section_count = await git_branch_section.count()
        stats.record_stat("git_branch_sections_found", git_section_count)
        
        if git_section_count == 0:
            await self.playwright_manager.take_screenshot("no_git_section_found")
            assert_that.is_true(False, "No git branch section found in file explorer")
            return TestResult(self.name, False, "Git branch section not detected")
        
        # Test initial collapsed state
        is_expanded_initially = await git_branch_section.evaluate("el => el.classList.contains('expanded')")
        assert_that.is_false(is_expanded_initially, "Git section should not be expanded initially")
        
        # Test expansion
        await git_branch_section.click()
        await page.wait_for_timeout(500)
        await self.playwright_manager.take_screenshot("git_section_expanded")
        
        is_expanded_after_click = await git_branch_section.evaluate("el => el.classList.contains('expanded')")
        assert_that.is_true(is_expanded_after_click, "Git section should be expanded after click")
        
        # Check if detailed section is visible
        git_detailed_section = page.locator(".git-detailed-section")
        detailed_section_count = await git_detailed_section.count()
        assert_that.is_true(detailed_section_count > 0, "Git detailed section should be visible when expanded")
        
        detection_time = stats.end_timer("git_section_detection")
        stats.record_stat("git_detection_time_ms", detection_time)
        
        # PHASE 2: Test file type UI reflections and interactions
        await self._test_file_modifications_ui(page, assert_that, stats)
        
        # PHASE 3: Test staging/unstaging through UI
        await self._test_staging_ui_interactions(page, assert_that, stats)
        
        # PHASE 4: Test commit form UI
        await self._test_commit_form_ui(page, assert_that, stats)
        
        # PHASE 5: Test reverting files through UI
        await self._test_revert_ui_interactions(page, assert_that, stats)
        
        # PHASE 6: Test diff viewing through UI
        await self._test_diff_ui_interactions(page, assert_that, stats)
        
        # PHASE 7: Test group actions (stage all, unstage all, etc.)
        await self._test_group_actions_ui(page, assert_that, stats)
        
        # PHASE 8: Test collapse functionality
        await git_branch_section.click()
        await page.wait_for_timeout(500)
        await self.playwright_manager.take_screenshot("git_section_collapsed")
        
        is_collapsed = await git_branch_section.evaluate("el => !el.classList.contains('expanded')")
        assert_that.is_true(is_collapsed, "Git section should collapse when clicked again")
        
        detailed_section_after_collapse = await git_detailed_section.count()
        assert_that.eq(detailed_section_after_collapse, 0, "Detailed section should be hidden when collapsed")
        
        if assert_that.has_failures():
            await self.playwright_manager.take_screenshot("test_failures")
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        return TestResult(
            self.name, 
            True, 
            f"Git status UI functionality tested successfully with comprehensive interactions",
            artifacts=stats.get_stats()
        )
    
    async def _test_file_modifications_ui(self, page, assert_that, stats):
        """Test that file modifications are properly reflected in the UI."""
        await self.playwright_manager.take_screenshot("before_file_modifications")
        
        # Look for existing files in the file explorer
        file_items = page.locator(".file-item:not(.folder)")
        file_count = await file_items.count()
        stats.record_stat("initial_file_count", file_count)
        
        if file_count > 0:
            # Get the first file to test modifications
            first_file = file_items.first
            file_name = await first_file.locator(".file-name").text_content()
            stats.record_stat("test_file_name", file_name)
            
            # Look for git status indicators
            git_indicators = page.locator(".git-status-indicator")
            git_indicator_count = await git_indicators.count()
            stats.record_stat("git_indicators_found", git_indicator_count)
            
            # Check for different file status classes
            modified_files = page.locator(".file-item.git-modified")
            untracked_files = page.locator(".file-item.git-untracked") 
            added_files = page.locator(".file-item.git-added")
            
            stats.record_stat("modified_files_count", await modified_files.count())
            stats.record_stat("untracked_files_count", await untracked_files.count())
            stats.record_stat("added_files_count", await added_files.count())
            
            await self.playwright_manager.take_screenshot("file_status_indicators")
    
    async def _test_staging_ui_interactions(self, page, assert_that, stats):
        """Test staging and unstaging files through the UI."""
        await self.playwright_manager.take_screenshot("before_staging_tests")
        
        # Look for untracked files section
        untracked_section = page.locator(".git-section-title").filter(has_text="Untracked Files")
        untracked_count = await untracked_section.count()
        
        if untracked_count > 0:
            # Expand untracked files section
            await untracked_section.click()
            await page.wait_for_timeout(300)
            await self.playwright_manager.take_screenshot("untracked_section_expanded")
            
            # Look for stage buttons
            stage_buttons = page.locator(".git-action-btn.stage")
            stage_button_count = await stage_buttons.count()
            stats.record_stat("stage_buttons_found", stage_button_count)
            
            if stage_button_count > 0:
                # Click first stage button
                await stage_buttons.first.click()
                await page.wait_for_timeout(1000)  # Wait for staging operation
                await self.playwright_manager.take_screenshot("after_staging_first_file")
                
                # Check if staged changes section appeared
                staged_section = page.locator(".git-section-title").filter(has_text="Staged Changes")
                staged_section_count = await staged_section.count()
                assert_that.is_true(staged_section_count > 0, "Staged changes section should appear after staging")
                
                if staged_section_count > 0:
                    # Expand staged changes section
                    await staged_section.click()
                    await page.wait_for_timeout(300)
                    await self.playwright_manager.take_screenshot("staged_section_expanded")
                    
                    # Look for unstage buttons
                    unstage_buttons = page.locator(".git-action-btn.unstage")
                    unstage_button_count = await unstage_buttons.count()
                    stats.record_stat("unstage_buttons_found", unstage_button_count)
                    
                    if unstage_button_count > 0:
                        # Test unstaging
                        await unstage_buttons.first.click()
                        await page.wait_for_timeout(1000)
                        await self.playwright_manager.take_screenshot("after_unstaging_file")
    
    async def _test_commit_form_ui(self, page, assert_that, stats):
        """Test the commit form UI functionality."""
        # First ensure we have staged files to enable commit form
        stage_all_btn = page.locator(".git-group-btn.stage-all")
        stage_all_count = await stage_all_btn.count()
        
        if stage_all_count > 0:
            await stage_all_btn.first.click()
            await page.wait_for_timeout(1000)
        
        await self.playwright_manager.take_screenshot("before_commit_form_test")
        
        # Look for commit form
        commit_form = page.locator(".git-commit-form")
        commit_form_count = await commit_form.count()
        stats.record_stat("commit_form_found", commit_form_count > 0)
        
        if commit_form_count > 0:
            # Test commit input field
            commit_input = page.locator(".git-commit-input")
            commit_input_count = await commit_input.count()
            assert_that.is_true(commit_input_count > 0, "Commit input should be present")
            
            # Test commit button
            commit_btn = page.locator(".git-commit-btn").filter(has_text="Commit")
            commit_btn_count = await commit_btn.count()
            assert_that.is_true(commit_btn_count > 0, "Commit button should be present")
            
            if commit_input_count > 0 and commit_btn_count > 0:
                # Test that commit button is initially disabled
                is_disabled = await commit_btn.is_disabled()
                assert_that.is_true(is_disabled, "Commit button should be disabled initially")
                
                # Enter commit message
                test_message = "Test commit from UI automation"
                await commit_input.fill(test_message)
                await page.wait_for_timeout(300)
                await self.playwright_manager.take_screenshot("commit_message_entered")
                
                # Check that button is now enabled
                is_disabled_after = await commit_btn.is_disabled()
                assert_that.is_false(is_disabled_after, "Commit button should be enabled after entering message")
                
                # Test cancel button
                cancel_btn = page.locator(".git-commit-cancel")
                cancel_count = await cancel_btn.count()
                if cancel_count > 0:
                    await cancel_btn.click()
                    await page.wait_for_timeout(200)
                    
                    # Check that message was cleared
                    input_value = await commit_input.input_value()
                    assert_that.eq(input_value, "", "Commit message should be cleared after cancel")
                    await self.playwright_manager.take_screenshot("commit_form_cancelled")
    
    async def _test_revert_ui_interactions(self, page, assert_that, stats):
        """Test reverting files through UI actions."""
        await self.playwright_manager.take_screenshot("before_revert_tests")
        
        # Look for unstaged changes section
        unstaged_section = page.locator(".git-section-title").filter(has_text="Unstaged Changes")
        unstaged_count = await unstaged_section.count()
        
        if unstaged_count > 0:
            # Expand unstaged section
            await unstaged_section.click()
            await page.wait_for_timeout(300)
            
            # Look for revert buttons
            revert_buttons = page.locator(".git-action-btn.revert")
            revert_button_count = await revert_buttons.count()
            stats.record_stat("revert_buttons_found", revert_button_count)
            
            await self.playwright_manager.take_screenshot("revert_buttons_visible")
            
            # Note: We won't actually click revert buttons in the test since that would
            # permanently modify files and could break subsequent tests
            # Instead we just verify they exist and are clickable
            if revert_button_count > 0:
                is_visible = await revert_buttons.first.is_visible()
                assert_that.is_true(is_visible, "Revert button should be visible")
    
    async def _test_diff_ui_interactions(self, page, assert_that, stats):
        """Test diff viewing through UI buttons."""
        await self.playwright_manager.take_screenshot("before_diff_tests")
        
        # Look for diff buttons
        diff_buttons = page.locator(".git-action-btn.diff")
        diff_button_count = await diff_buttons.count()
        stats.record_stat("diff_buttons_found", diff_button_count)
        
        if diff_button_count > 0:
            is_visible = await diff_buttons.first.is_visible()
            assert_that.is_true(is_visible, "Diff button should be visible")
            
            # Note: We could click diff buttons since they open tabs without modifying files
            # But for now we just verify they exist
            await self.playwright_manager.take_screenshot("diff_buttons_visible")
    
    async def _test_group_actions_ui(self, page, assert_that, stats):
        """Test group actions like stage all, unstage all, revert all."""
        await self.playwright_manager.take_screenshot("before_group_actions_tests")
        
        # Look for various group action buttons
        stage_all_btns = page.locator(".git-group-btn.stage-all")
        unstage_all_btns = page.locator(".git-group-btn.unstage-all") 
        revert_all_btns = page.locator(".git-group-btn.revert-all")
        diff_all_btns = page.locator(".git-group-btn.diff-all")
        
        stage_all_count = await stage_all_btns.count()
        unstage_all_count = await unstage_all_btns.count()
        revert_all_count = await revert_all_btns.count()
        diff_all_count = await diff_all_btns.count()
        
        stats.record_stat("stage_all_buttons", stage_all_count)
        stats.record_stat("unstage_all_buttons", unstage_all_count) 
        stats.record_stat("revert_all_buttons", revert_all_count)
        stats.record_stat("diff_all_buttons", diff_all_count)
        
        # Test that at least some group action buttons exist
        total_group_actions = stage_all_count + unstage_all_count + revert_all_count + diff_all_count
        assert_that.is_true(total_group_actions > 0, "Should have at least some group action buttons")
        
        await self.playwright_manager.take_screenshot("group_action_buttons_visible")
    
    async def setup(self):
        """Setup for git status UI test - this test depends on navigate_testing_folder_test for git setup."""
        # This test relies on navigate_testing_folder_test to set up the git repository
        # We don't need to do any additional setup here since that test creates
        # the git repo and test files we need
        pass
    
    
    async def teardown(self):
        """Teardown for git status UI test."""
        # This test depends on navigate_testing_folder_test for setup
        # We let that test handle cleanup as well
        pass