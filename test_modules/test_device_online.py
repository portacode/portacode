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
            start_url="/dashboard/"
        )
    
    async def run(self) -> TestResult:
        """Test device online status."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        
        # Find portacode streamer device card that's online
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        await device_card.wait_for()
        
        # Verify device name contains "portacode streamer"
        device_name = device_card.locator(".device-name-text")
        device_name_text = await device_name.text_content()
        assert_that.contains(device_name_text.lower(), "portacode streamer", "Device name")
        
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        return TestResult(self.name, True, "Device shows online in dashboard")
    
    async def setup(self):
        """Setup for device online test."""
        pass
    
    async def teardown(self):
        """Teardown for device online test."""
        pass
