"""Login flow test example with simplified assertions."""

import asyncio
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class LoginFlowTest(BaseTest):
    """Test the basic login flow of the application."""
    
    def __init__(self):
        super().__init__(
            name="login_flow_test",
            category=TestCategory.SMOKE,
            description="Verify that users can successfully log in to the application",
            tags=["login", "authentication", "smoke"],
            requires_login=False  # This test establishes login
        )
    
    async def run(self) -> TestResult:
        """Execute the login flow test using simplified assertions."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        
        if not page:
            return TestResult(self.name, False, "No active Playwright page")
        
        # Wait for page to be ready
        await page.wait_for_load_state("networkidle")
        await self.playwright_manager.take_screenshot("login_start")
        
        # Get current URL and try dashboard access
        current_url = page.url
        base_url = '/'.join(current_url.split('/')[:3])
        dashboard_url = f"{base_url}/dashboard/"
        
        # Navigate to dashboard to test authentication
        response = await page.goto(dashboard_url)
        await page.wait_for_load_state("networkidle")
        
        # Use simplified assertions
        assert_that.status_ok(response, "Dashboard request")
        assert_that.url_contains(page, "/dashboard", "Dashboard URL")
        
        # Check that we're not redirected to login
        login_indicators = ["login", "signin", "auth"]
        is_login_page = any(indicator in page.url.lower() for indicator in login_indicators)
        assert_that.is_false(is_login_page, "Should not be on login page")
        
        # Check client sessions for active connection
        sessions = self.inspect().load_client_sessions()
        active_sessions = self.inspect().get_active_sessions()
        assert_that.is_true(len(active_sessions) > 0, "Should have active sessions")
        
        if assert_that.has_failures():
            await self.playwright_manager.take_screenshot("login_failed")
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        await self.playwright_manager.take_screenshot("login_success")
        return TestResult(self.name, True, f"Login successful. Dashboard at {page.url}")
    
    async def setup(self):
        """Setup for login test - no additional setup needed as framework handles login."""
        self.logger.info("Login flow test setup - framework handles CLI and login automatically")
    
    async def teardown(self):
        """Teardown for login test."""
        self.logger.info("Login flow test teardown completed")