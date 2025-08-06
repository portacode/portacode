"""Device connection integration test."""

import asyncio
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class DeviceConnectionTest(BaseTest):
    """Test device connection functionality."""
    
    def __init__(self):
        super().__init__(
            name="device_connection_test",
            category=TestCategory.INTEGRATION,
            description="Verify device connection status and management",
            tags=["device", "connection", "integration"]
        )
    
    async def run(self) -> TestResult:
        """Execute device connection test."""
        try:
            page = self.playwright_manager.page
            
            if not page:
                return TestResult(self.name, False, "No active Playwright page")
            
            # Navigate to devices or dashboard page
            await page.wait_for_load_state("networkidle")
            await self.playwright_manager.take_screenshot("initial_page")
            
            # Look for device-related elements
            device_elements = [
                "[class*='device']", "[id*='device']",
                "[class*='connection']", "[id*='connection']",
                "button:has-text('Connect')", "button:has-text('Device')",
                ".device-card", ".device-list", "#device-status"
            ]
            
            found_elements = []
            for selector in device_elements:
                try:
                    elements = await page.query_selector_all(selector)
                    if elements:
                        found_elements.append(selector)
                        self.logger.info(f"Found device element: {selector}")
                except:
                    continue
            
            await self.playwright_manager.log_action("device_elements_search", {
                "searched_selectors": device_elements,
                "found_elements": found_elements
            })
            
            # Check CLI connection status
            cli_info = self.cli_manager.get_connection_info()
            cli_connected = cli_info["is_connected"]
            
            await self.playwright_manager.log_action("cli_status_check", {
                "cli_connected": cli_connected,
                "cli_info": cli_info
            })
            
            # Look for connection status indicators on the page
            status_indicators = [
                ".status", ".connected", ".online", ".device-status",
                "[class*='status']", "[class*='connect']"
            ]
            
            status_found = []
            for selector in status_indicators:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        text_content = await element.text_content()
                        if text_content:
                            status_found.append({
                                "selector": selector,
                                "text": text_content.strip()
                            })
                except:
                    continue
            
            await self.playwright_manager.take_screenshot("device_status_check")
            
            # Check if we can see the device in the UI
            page_content = await page.content()
            device_keywords = ["connected", "online", "active", "device"]
            found_keywords = [kw for kw in device_keywords if kw.lower() in page_content.lower()]
            
            await self.playwright_manager.log_action("device_connection_verification", {
                "cli_connected": cli_connected,
                "ui_device_elements": found_elements,
                "status_indicators": status_found,
                "found_keywords": found_keywords
            })
            
            # Determine test result
            if cli_connected and (found_elements or found_keywords):
                return TestResult(
                    self.name, True,
                    f"Device connection verified. CLI connected: {cli_connected}, UI elements found: {len(found_elements)}"
                )
            elif cli_connected:
                return TestResult(
                    self.name, True,
                    "CLI connection established, but no device UI elements found (may be expected)"
                )
            else:
                return TestResult(
                    self.name, False,
                    f"Device connection issues. CLI connected: {cli_connected}"
                )
                
        except Exception as e:
            self.logger.error(f"Device connection test failed: {e}")
            return TestResult(self.name, False, f"Test execution failed: {str(e)}")
    
    async def setup(self):
        """Setup for device connection test."""
        self.logger.info("Device connection test setup")
        # Additional setup if needed
    
    async def teardown(self):
        """Teardown for device connection test."""
        self.logger.info("Device connection test teardown completed")