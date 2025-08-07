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
            start_url="/dashboard/"
        )
    
    async def run(self) -> TestResult:
        """Test terminal start functionality with timing."""
        page = self.playwright_manager.page
        stats = self.stats()
        
        # Find portacode device and click Terminal button
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        terminal_button = device_card.get_by_text("Terminal")
        
        # Start timing and create terminal
        stats.start_timer("terminal_creation")
        await terminal_button.click()
        
        # Wait for modal and click Start Terminal
        await page.wait_for_selector("text=Start New Terminal")
        await page.get_by_text("Start Terminal").click()
        
        # Wait for terminal chip to appear
        terminal_chip = device_card.locator(".terminal-chip-channel")
        await terminal_chip.wait_for()
        
        creation_time = stats.end_timer("terminal_creation")
        stats.record_stat("terminal_creation_time_ms", creation_time)
        
        return TestResult(
            self.name, 
            True, 
            f"Terminal started in {creation_time:.1f}ms",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for terminal start test."""
        pass
    
    async def teardown(self):
        """Teardown for terminal start test."""
        pass