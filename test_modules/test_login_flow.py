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
            page = self.playwright_manager.page
            
            if not page:
                return TestResult(self.name, False, "No active Playwright page")
            
            # Wait for page to be ready
            await page.wait_for_load_state("networkidle")
            
            # Take screenshot of current page
            await self.playwright_manager.take_screenshot("current_page")
            
            current_url = page.url
            self.logger.info(f"Current URL: {current_url}")
            
            await self.playwright_manager.log_action("initial_page_check", {
                "url": current_url
            })
            
            # Test 1: Check if we're already on dashboard (login successful)
            if "/dashboard" in current_url or current_url.endswith("/dashboard/"):
                # We're on dashboard - verify it loads successfully (200 status)
                try:
                    response = await page.goto(current_url)
                    if response and response.status == 200:
                        await self.playwright_manager.take_screenshot("dashboard_success")
                        return TestResult(
                            self.name, True,
                            f"Login successful. Dashboard accessible at {current_url} with status {response.status}"
                        )
                    else:
                        status = response.status if response else "no response"
                        return TestResult(
                            self.name, False,
                            f"Dashboard returned status {status}, expected 200"
                        )
                except Exception as e:
                    return TestResult(
                        self.name, False,
                        f"Failed to access dashboard: {str(e)}"
                    )
            
            # Test 2: Try to navigate directly to dashboard to test authentication
            dashboard_url = current_url.replace(current_url.split('/', 3)[-1], 'dashboard/')
            if not dashboard_url.endswith('/dashboard/'):
                # Construct dashboard URL from base URL
                base_url = '/'.join(current_url.split('/')[:3])
                dashboard_url = f"{base_url}/dashboard/"
                
            await self.playwright_manager.log_action("dashboard_access_attempt", {
                "dashboard_url": dashboard_url
            })
            
            try:
                # Try to navigate to dashboard
                response = await page.goto(dashboard_url)
                await page.wait_for_load_state("networkidle")
                
                final_url = page.url
                status_code = response.status if response else None
                
                await self.playwright_manager.take_screenshot("dashboard_access_result")
                
                await self.playwright_manager.log_action("dashboard_access_result", {
                    "requested_url": dashboard_url,
                    "final_url": final_url,
                    "status_code": status_code
                })
                
                # Check if we successfully reached dashboard
                # Must check that we're actually ON the dashboard page, not just that URL contains "dashboard"
                if final_url == dashboard_url and status_code == 200:
                    return TestResult(
                        self.name, True,
                        f"Login successful. Dashboard accessible at {final_url} with status {status_code}"
                    )
                elif final_url.endswith('/dashboard/') and 'login' not in final_url and status_code == 200:
                    # Alternative dashboard URL format but not a login redirect
                    return TestResult(
                        self.name, True,
                        f"Login successful. Dashboard accessible at {final_url} with status {status_code}"
                    )
                else:
                    # We were redirected or got wrong page
                    if "login" in final_url.lower() or "signin" in final_url.lower():
                        return TestResult(
                            self.name, False,
                            f"Authentication failed. Redirected to login page: {final_url}"
                        )
                    elif final_url != dashboard_url:
                        return TestResult(
                            self.name, False,
                            f"Unexpected redirect from {dashboard_url} to {final_url}"
                        )
                    else:
                        return TestResult(
                            self.name, False,
                            f"Dashboard returned status {status_code}, expected 200"
                        )
                    
            except Exception as e:
                return TestResult(
                    self.name, False,
                    f"Failed to access dashboard: {str(e)}"
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