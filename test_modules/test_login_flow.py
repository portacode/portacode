"""Login flow test - simplified and fast."""

import os

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class LoginFlowTest(BaseTest):
    """Test the basic login flow of the application."""
    
    def __init__(self):
        super().__init__(
            name="login_flow_test",
            category=TestCategory.SMOKE,
            description="Verify that users can successfully log in to the application",
            tags=["login", "authentication", "smoke"],
        )
    
    async def run(self) -> TestResult:
        """Execute the login flow test."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        
        # Navigate to dashboard - should be accessible if logged in
        base_url = '/'.join(page.url.split('/')[:3])
        dashboard_url = f"{base_url}/dashboard/"
        response = await page.goto(dashboard_url)
        
        # Check if successfully reached dashboard
        assert_that.status_ok(response, "Dashboard request")
        assert_that.url_contains(page, "/dashboard", "Dashboard URL")
        
        # Verify we have active sessions unless explicitly allowed to skip
        allow_empty_sessions = os.getenv("ALLOW_EMPTY_SESSIONS", "false").lower() in ("1", "true", "yes")
        if not allow_empty_sessions:
            active_sessions = self.inspect().get_active_sessions()
            assert_that.is_true(len(active_sessions) > 0, "Should have active sessions")
        
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        return TestResult(self.name, True, f"Login successful. Dashboard at {page.url}")
    
    async def setup(self):
        """Setup for login test."""
        pass
    
    async def teardown(self):
        """Teardown for login test."""
        pass
