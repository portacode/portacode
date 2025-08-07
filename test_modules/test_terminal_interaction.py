"""Test terminal interaction - opening terminal and running commands."""

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class TerminalInteractionTest(BaseTest):
    """Test terminal interaction - opening terminal and running commands."""
    
    def __init__(self):
        super().__init__(
            name="terminal_interaction_test",
            category=TestCategory.INTEGRATION,
            description="Test terminal interaction - click terminal chip, run ls command, measure timing",
            tags=["terminal", "interaction", "command", "timing"],
            depends_on=["terminal_start_test"],
            start_url="/dashboard/"
        )
    
    async def run(self) -> TestResult:
        """Test terminal interaction with command execution timing."""
        page = self.playwright_manager.page
        stats = self.stats()
        
        # Wait for any modal to be closed before clicking terminal chip
        await page.wait_for_function(
            "() => !document.querySelector('#startModal.show')",
            timeout=10000
        )
        
        # Click terminal chip to open terminal
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        terminal_chip = device_card.locator(".terminal-chip-channel")
        await terminal_chip.click()
        
        # Wait for terminal and prompt
        await page.wait_for_function(
            "() => document.querySelector('.xterm-rows')?.textContent.includes('menas@portacode-streamer:~$')"
        )
        
        # Ensure terminal is properly focused and ready for input
        terminal_textarea = page.locator(".xterm-helper-textarea")
        await terminal_textarea.focus()
        await page.wait_for_timeout(200)  # Longer delay for focus stability
        
        # Double-check focus and click terminal area if needed
        is_focused = await terminal_textarea.evaluate("el => document.activeElement === el")
        if not is_focused:
            await page.locator(".xterm-screen").click()
            await terminal_textarea.focus()
            await page.wait_for_timeout(100)
        
        # Send ls command and measure timing
        stats.start_timer("command_execution")
        await page.keyboard.type("ls\n")
        
        # Wait for output containing client_sessions.json
        await page.wait_for_function(
            "() => document.querySelector('.xterm-rows')?.textContent.includes('client_sessions.json')"
        )
        
        command_time = stats.end_timer("command_execution")
        
        return TestResult(
            self.name, 
            True, 
            f"Command executed in {command_time:.1f}ms",
            artifacts=stats.get_stats()
        )
    
    async def setup(self):
        """Setup for terminal interaction test."""
        pass
    
    async def teardown(self):
        """Teardown for terminal interaction test."""
        try:
            page = self.playwright_manager.page
            await page.evaluate('document.querySelector("#termModal")?.querySelector(".btn-close")?.click()')
        except:
            pass