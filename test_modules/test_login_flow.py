"""Login flow test example."""

import asyncio
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class LoginFlowTest(BaseTest):
    """Test the basic login flow of the application."""
    
    def __init__(self):
        super().__init__(
            name="login_flow_test",
            category=TestCategory.SMOKE,
            description="Verify that users can successfully log in to the application",
            tags=["login", "authentication", "smoke"]
        )
    
    async def run(self) -> TestResult:
        """Execute the login flow test."""
        try:
            # The Playwright manager is already set up and logged in by the framework
            page = self.playwright_manager.page
            
            if not page:
                return TestResult(self.name, False, "No active Playwright page")
            
            # Take screenshot of logged-in state
            await self.playwright_manager.take_screenshot("logged_in_dashboard")
            
            # Verify we're on the dashboard or home page after login
            await page.wait_for_load_state("networkidle")
            
            # Check for indicators that login was successful
            current_url = page.url
            self.logger.info(f"Current URL after login: {current_url}")
            
            # Look for common post-login elements
            success_indicators = [
                "dashboard", "home", "welcome", "logout", "profile", "menu"
            ]
            
            page_content = await page.content()
            found_indicators = [indicator for indicator in success_indicators 
                              if indicator.lower() in page_content.lower()]
            
            await self.playwright_manager.log_action("login_verification", {
                "url": current_url,
                "found_indicators": found_indicators
            })
            
            if found_indicators:
                return TestResult(
                    self.name, True, 
                    f"Login successful. Found indicators: {found_indicators}"
                )
            else:
                # Try to find any form of user indication
                user_elements = await page.query_selector_all("[class*='user'], [id*='user'], [class*='profile'], [id*='profile']")
                if user_elements:
                    return TestResult(
                        self.name, True,
                        "Login successful. Found user-related elements."
                    )
                
                return TestResult(
                    self.name, False,
                    "Could not verify successful login. No expected indicators found."
                )
                
        except Exception as e:
            self.logger.error(f"Login flow test failed: {e}")
            return TestResult(self.name, False, f"Test execution failed: {str(e)}")
    
    async def setup(self):
        """Setup for login test - no additional setup needed as framework handles login."""
        self.logger.info("Login flow test setup - framework handles CLI and login automatically")
    
    async def teardown(self):
        """Teardown for login test."""
        self.logger.info("Login flow test teardown completed")