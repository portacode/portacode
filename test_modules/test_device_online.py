"""Test that device shows online in dashboard."""

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class DeviceOnlineTest(BaseTest):
    """Test that the device is showing as online in the dashboard."""
    
    def __init__(self):
        super().__init__(
            name="device_online_test",
            category=TestCategory.SMOKE,
            description="Verify device shows as online in dashboard after login",
            tags=["device", "online", "dashboard"],
            depends_on=["login_flow_test"],
            requires_login=True
        )
    
    async def run(self) -> TestResult:
        """Test device online status with simplified assertions."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        
        # Wait a few seconds after login for device to show online
        await page.wait_for_timeout(3000)
        await self.playwright_manager.take_screenshot("before_device_check")
        
        # Look for device card with online status
        try:
            await page.wait_for_selector(".device-card.online", timeout=10000)
        except:
            pass  # Continue to assertions
        
        # Assert device card exists and is online
        await assert_that.element_visible(page, ".device-card.online", "Device card online")
        
        # Find the specific device card with "portacode streamer" text
        portacode_device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        
        # Assert the portacode streamer device card exists and is online
        try:
            await portacode_device_card.wait_for(timeout=5000)
            device_count = await portacode_device_card.count()
            assert_that.is_true(device_count > 0, "Portacode streamer device card found")
            
            # Check if it has the device name text span
            device_name_span = portacode_device_card.locator(".device-name-text")
            device_name_text = await device_name_span.text_content()
            assert_that.contains(device_name_text.lower(), "portacode streamer", "Device name contains portacode streamer")
            
        except Exception as e:
            assert_that.is_true(False, f"Could not find portacode streamer device: {e}")
        
        # Verify login dependency passed
        login_result = self.get_dependency_result("login_flow_test")
        assert_that.is_true(login_result and login_result.success, "Login dependency")
        
        if assert_that.has_failures():
            await self.playwright_manager.take_screenshot("device_online_failed")
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        await self.playwright_manager.take_screenshot("device_online_success")
        return TestResult(self.name, True, f"Device shows online in dashboard")
    
    async def setup(self):
        """Setup for device online test."""
        self.logger.info("Device online test setup")
    
    async def teardown(self):
        """Teardown for device online test."""
        self.logger.info("Device online test teardown completed")