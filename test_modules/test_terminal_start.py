"""Test starting a terminal in the device."""

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class TerminalStartTest(BaseTest):
    """Test starting a new terminal in the device."""
    
    def __init__(self):
        super().__init__(
            name="terminal_start_test",
            category=TestCategory.INTEGRATION,
            description="Verify new terminal can be started and measure timing",
            tags=["terminal", "device", "timing"],
            depends_on=["device_online_test"],
            requires_login=True
        )
    
    async def run(self) -> TestResult:
        """Test terminal start functionality with timing."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        # Verify device online dependency passed
        device_result = self.get_dependency_result("device_online_test")
        assert_that.is_true(device_result and device_result.success, "Device online dependency")
        
        # Wait for device card to be ready
        await page.wait_for_timeout(1000)
        await self.playwright_manager.take_screenshot("before_terminal_start")
        
        # Find the specific portacode streamer device card
        portacode_device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        
        # Assert the portacode device card is available
        try:
            await portacode_device_card.wait_for(timeout=5000)
            device_count = await portacode_device_card.count()
            assert_that.is_true(device_count > 0, "Portacode streamer device card found")
        except Exception as e:
            assert_that.is_true(False, f"Could not find portacode streamer device: {e}")
        
        # Look for "Terminal" button in the portacode device card
        terminal_button = portacode_device_card.get_by_text("Terminal")
        
        # Start timing the terminal creation
        stats.start_timer("terminal_creation")
        
        try:
            # Click the Terminal button
            await terminal_button.click()
            self.logger.info("Clicked Terminal button")
            
            # Wait for the "Start New Terminal" modal to appear
            await page.wait_for_selector("text=Start New Terminal", timeout=5000)
            self.logger.info("Start New Terminal modal appeared")
            
            # Click the "Start Terminal" button in the modal
            start_terminal_button = page.get_by_text("Start Terminal")
            await start_terminal_button.click()
            self.logger.info("Clicked Start Terminal button in modal")
            
            # Wait for terminal chip to appear in the portacode device card
            terminal_chip = portacode_device_card.locator(".terminal-chip-channel")
            await terminal_chip.wait_for(timeout=15000)
            
            # End timing
            creation_time_ms = stats.end_timer("terminal_creation")
            stats.record_stat("terminal_creation_time_ms", creation_time_ms)
            
            self.logger.info(f"Terminal created in {creation_time_ms:.1f}ms")
            
        except Exception as e:
            # End timing even if failed
            creation_time_ms = stats.end_timer("terminal_creation")
            stats.record_stat("terminal_creation_failed_time_ms", creation_time_ms)
            assert_that.is_true(False, f"Failed to create terminal: {e}")
        
        # Assert terminal chip appeared in the portacode device card
        try:
            terminal_chip = portacode_device_card.locator(".terminal-chip-channel")
            terminal_chip_count = await terminal_chip.count()
            assert_that.is_true(terminal_chip_count > 0, "Terminal chip appeared")
            stats.record_stat("terminal_chips_count", terminal_chip_count)
        except Exception as e:
            assert_that.is_true(False, f"Terminal chip did not appear: {e}")
            stats.record_stat("terminal_chips_count", 0)
        
        if assert_that.has_failures():
            await self.playwright_manager.take_screenshot("terminal_start_failed")
            return TestResult(self.name, False, assert_that.get_failure_message(), artifacts=stats.get_stats())
        
        await self.playwright_manager.take_screenshot("terminal_start_success")
        return TestResult(
            self.name, 
            True, 
            f"Terminal started in {creation_time_ms:.1f}ms",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for terminal start test."""
        self.logger.info("Terminal start test setup")
    
    async def teardown(self):
        """Teardown for terminal start test.""" 
        self.logger.info("Terminal start test teardown completed")