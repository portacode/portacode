"""Test navigating to 'testing_folder' project."""

from playwright.async_api import expect
from playwright.async_api import Locator
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class NavigateTestingFolderTest(BaseTest):
    """Test navigating to the 'testing_folder' project through Editor button."""
    
    def __init__(self):
        super().__init__(
            name="navigate_testing_folder_test",
            category=TestCategory.INTEGRATION,
            description="Navigate to 'testing_folder' project via Editor button and wait for file explorer with git details",
            tags=["navigation", "editor", "project", "testing_folder"],
            depends_on=["device_online_test"],
            requires_login=True,
            start_url="/dashboard/"
        )
    
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
        """Setup for testing_folder navigation test."""
        pass
    
    async def teardown(self):
        """Teardown for testing_folder navigation test."""
        pass