from testing_framework.core.base_test import BaseTest, TestResult, TestCategory
import asyncio

class TerminalLoadingRaceConditionTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="terminal_loading_race_condition_test",
            category=TestCategory.INTEGRATION,
            description="Test that terminals load immediately without showing empty state message",
            tags=["terminal", "websocket", "race-condition", "critical"],
            depends_on=["device_online_test"],
            start_url="/project/1d98e739-de00-4d65-a13b-c6c82173683f/"
        )
    
    async def run(self) -> TestResult:
        """Test that terminals load immediately without ever showing empty state."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        
        try:
            # start_url should have navigated us to the project page already
            # Wait a moment for components to initialize
            await asyncio.sleep(1)
            
            # Click the add terminal button to start a terminal first
            add_terminal_btn = page.locator(".add-terminal-btn")
            await add_terminal_btn.wait_for(timeout=10000)
            await add_terminal_btn.click()
            await asyncio.sleep(2)
            
            # Now reload the page to test the race condition
            await page.reload()
            await asyncio.sleep(1)
            
            # Check after first reload
            empty_message_selector = "text=Click the '+' to create a new terminal."
            await asyncio.sleep(3)
            
            if await page.is_visible(empty_message_selector):
                return TestResult(
                    self.name, 
                    False, 
                    f"RACE CONDITION DETECTED: Empty terminal message shown after first reload"
                )
            
            # Do a hard reload (Ctrl+Shift+R) and test again
            await page.reload(wait_until="networkidle")
            await asyncio.sleep(1)
            await asyncio.sleep(3)
            
            if await page.is_visible(empty_message_selector):
                return TestResult(
                    self.name, 
                    False, 
                    f"RACE CONDITION DETECTED: Empty terminal message shown after hard reload"
                )
            
            # One more normal reload for final check
            await page.reload()
            await asyncio.sleep(1)
            await asyncio.sleep(3)
            
            # Check the final state - what actually appeared
            loading_visible = await page.is_visible("text=Loading Terminals...")
            empty_message_visible = await page.is_visible(empty_message_selector)
            terminal_area_visible = await page.is_visible("div#term-area")
            
            # The race condition manifests as showing empty message when terminals should exist
            if empty_message_visible:
                return TestResult(
                    self.name, 
                    False, 
                    f"RACE CONDITION DETECTED: Empty terminal message shown when terminals should exist. This means terminal_list processing failed."
                )
            elif terminal_area_visible:
                return TestResult(
                    self.name, 
                    True, 
                    "SUCCESS: Terminal area loaded properly without showing empty state"
                )
            elif loading_visible:
                return TestResult(
                    self.name, 
                    False, 
                    "STUCK: Still showing loading state after 3+ seconds"
                )
            else:
                return TestResult(
                    self.name, 
                    False, 
                    f"UNEXPECTED STATE: No terminal component states found. loading={loading_visible}, empty={empty_message_visible}, terminal={terminal_area_visible}"
                )
                
        except Exception as e:
            return TestResult(self.name, False, f"Test failed with exception: {str(e)}")