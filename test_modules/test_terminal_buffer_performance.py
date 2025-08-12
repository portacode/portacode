"""Test terminal buffer performance and WebSocket message sizes with high-volume output."""

import os
import time
import json
import shutil
from pathlib import Path
from playwright.async_api import expect
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

# Global test folder path
TESTING_FOLDER_PATH = "/home/menas/testing_folder"


class TerminalBufferPerformanceTest(BaseTest):
    """Test terminal buffer performance with high-volume output from Gemini CLI."""
    
    def __init__(self):
        super().__init__(
            name="terminal_buffer_performance_test",
            category=TestCategory.PERFORMANCE,
            description="Test terminal buffer behavior and WebSocket message sizes with high-volume output from Gemini CLI",
            tags=["terminal", "buffer", "performance", "websocket", "gemini", "memory"],
            depends_on=["device_online_test"],
            start_url="/dashboard/"
        )
        
    async def run(self) -> TestResult:
        """Test terminal buffer performance with massive output."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()
        stats = self.stats()
        
        # Step 1: Navigate to testing_folder project (copied from working test)
        device_card = page.locator(".device-card.online").filter(has_text="portacode streamer")
        await device_card.wait_for()
        
        # Click the Editor button in the device card
        stats.start_timer("editor_button_click")
        editor_button = device_card.get_by_text("Editor")
        await editor_button.wait_for()
        await editor_button.click()
        
        editor_click_time = stats.end_timer("editor_button_click")
        stats.record_stat("editor_button_click_time_ms", editor_click_time)
        
        # Navigate to testing_folder project
        stats.start_timer("project_navigation")
        
        # Wait for the project selector modal to appear
        await page.wait_for_selector("#projectSelectorModal.show", timeout=10000)
        
        # Wait for projects to load in the modal
        await page.wait_for_selector(".item-list .section-header", timeout=10000)
        
        # Look for testing_folder project item and click it
        # Projects are displayed as items with class "item project" 
        
        # First let's see what projects are available for debugging
        project_items = page.locator('.item.project')
        project_count = await project_items.count()
        
        # If there are projects, look for testing_folder specifically
        if project_count > 0:
            # Try to find testing_folder specifically first
            testing_folder_item = page.locator('.item.project').filter(has_text="testing_folder")
            testing_folder_count = await testing_folder_item.count()
            
            if testing_folder_count > 0:
                # Found testing_folder project - this is ideal!
                await testing_folder_item.first.click()
                stats.record_stat("found_testing_folder", True)
            else:
                # If no testing_folder, try any project with "test" in the name as fallback
                test_item = page.locator('.item.project').filter(has_text="test")
                test_count = await test_item.count()
                if test_count > 0:
                    await test_item.first.click()
                    stats.record_stat("found_testing_folder", False)
                    stats.record_stat("fallback_reason", "used_test_project")
                else:
                    # Use first available project as last resort
                    await project_items.first.click()
                    stats.record_stat("found_testing_folder", False)
                    stats.record_stat("fallback_reason", "used_first_available")
        else:
            # No projects found
            assert_that.is_true(False, "No projects found in modal")
        
        navigation_time = stats.end_timer("project_navigation")
        stats.record_stat("project_navigation_time_ms", navigation_time)
        
        # Wait for page to load with file explorer
        stats.start_timer("page_load")
        # Wait for the page to load properly
        await page.wait_for_timeout(2000)
        
        page_load_time = stats.end_timer("page_load")
        stats.record_stat("page_load_time_ms", page_load_time)
        
        # Step 2: Click the add terminal button (copied from working test)
        stats.start_timer("terminal_setup")
        add_terminal_btn = page.locator(".add-terminal-btn")
        await add_terminal_btn.wait_for(timeout=10000)
        await add_terminal_btn.click()
        
        # Wait for terminal to appear and focus on it properly
        terminal_textarea = page.locator("code-terminal")
        await terminal_textarea.wait_for()
        await terminal_textarea.focus()
        await page.wait_for_timeout(4000)  # Longer delay for focus stability
        
        terminal_setup_time = stats.end_timer("terminal_setup")
        stats.record_stat("terminal_setup_time_ms", terminal_setup_time)
        
        # Step 3: Run gemini with a prompt to generate massive output
        stats.start_timer("gemini_test")
        
        # Start gemini
        await page.keyboard.type("gemini")
        await page.keyboard.press("Enter")
        gemini_text_box_placeholder = "Type your message or @path/to/file"
        await page.wait_for_timeout(20000)  # Wait 20 seconds
        
        # Send a prompt that will generate lots of output
        gemini_prompt = "Please write a very detailed, comprehensive technical explanation of how neural networks work, including mathematical formulas, code examples, detailed explanations of backpropagation, different architectures like CNNs and RNNs, training procedures, and real-world applications. Make it as detailed and long as possible - at least 5000 words with examples and code snippets."
        await page.keyboard.type(gemini_prompt)
        await page.keyboard.press("Enter")
        
        # Wait for Gemini to generate output 
        await page.wait_for_timeout(20000)  # 20 seconds max wait
        
        gemini_time = stats.end_timer("gemini_test")
        stats.record_stat("gemini_test_time_ms", gemini_time)
        
        # Record final statistics
        total_time = editor_click_time + navigation_time + page_load_time + terminal_setup_time + gemini_time
        
        # Check for failures
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        # Success message with key metrics
        success_msg = f"""Terminal buffer performance test completed in {total_time:.1f}ms
Gemini output generated for {gemini_time:.1f}ms - check websockets.json for buffer behavior"""
        
        return TestResult(
            self.name,
            True,
            success_msg,
            artifacts=stats.get_stats()
        )
    
    async def _count_websocket_messages(self) -> int:
        """Count total WebSocket messages so far."""
        try:
            # The framework should be logging WebSocket messages to websockets.json
            # We'll try to read it if available
            ws_log_path = Path("test_results") / "current_run" / "websockets.json"
            if ws_log_path.exists():
                with open(ws_log_path, 'r') as f:
                    messages = json.load(f)
                    return len(messages) if isinstance(messages, list) else 0
            return 0
        except Exception:
            return 0
    
    async def _analyze_websocket_messages(self) -> dict:
        """Analyze WebSocket messages for size patterns."""
        try:
            ws_log_path = Path("test_results") / "current_run" / "websockets.json"
            if not ws_log_path.exists():
                return {"error": "websockets.json not found"}
            
            with open(ws_log_path, 'r') as f:
                messages = json.load(f)
            
            if not isinstance(messages, list):
                return {"error": "invalid websockets.json format"}
            
            analysis = {
                "total_messages": len(messages),
                "terminal_data_messages": 0,
                "terminal_list_messages": 0,
                "largest_message_size": 0,
                "largest_message_type": "",
                "message_sizes": [],
                "terminal_data_sizes": [],
                "terminal_list_sizes": []
            }
            
            for msg in messages:
                if isinstance(msg, dict) and "data" in msg:
                    msg_str = json.dumps(msg)
                    msg_size = len(msg_str.encode('utf-8'))
                    analysis["message_sizes"].append(msg_size)
                    
                    if msg_size > analysis["largest_message_size"]:
                        analysis["largest_message_size"] = msg_size
                        analysis["largest_message_type"] = msg.get("data", {}).get("event", "unknown")
                    
                    # Check message type
                    event = msg.get("data", {}).get("event", "")
                    if event == "terminal_data":
                        analysis["terminal_data_messages"] += 1
                        analysis["terminal_data_sizes"].append(msg_size)
                    elif event == "terminal_list":
                        analysis["terminal_list_messages"] += 1
                        analysis["terminal_list_sizes"].append(msg_size)
            
            # Calculate statistics
            if analysis["message_sizes"]:
                analysis["avg_message_size"] = sum(analysis["message_sizes"]) / len(analysis["message_sizes"])
                analysis["max_message_size"] = max(analysis["message_sizes"])
                analysis["min_message_size"] = min(analysis["message_sizes"])
            
            if analysis["terminal_data_sizes"]:
                analysis["avg_terminal_data_size"] = sum(analysis["terminal_data_sizes"]) / len(analysis["terminal_data_sizes"])
                analysis["max_terminal_data_size"] = max(analysis["terminal_data_sizes"])
            
            if analysis["terminal_list_sizes"]:
                analysis["avg_terminal_list_size"] = sum(analysis["terminal_list_sizes"]) / len(analysis["terminal_list_sizes"])
                analysis["max_terminal_list_size"] = max(analysis["terminal_list_sizes"])
            
            return analysis
        
        except Exception as e:
            return {"error": f"Failed to analyze WebSocket messages: {str(e)}"}
    
    async def setup(self):
        """Setup for terminal buffer performance test - ensure testing folder exists."""
        try:
            # Ensure the testing folder exists but is empty
            os.makedirs(TESTING_FOLDER_PATH, exist_ok=True)
            
            # Clean out any existing content so we start fresh
            for item in os.listdir(TESTING_FOLDER_PATH):
                item_path = os.path.join(TESTING_FOLDER_PATH, item)
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    
        except Exception as e:
            print(f"❌ Setup failed: {e}")
            raise Exception(f"Failed to set up test project: {e}")
    
    async def teardown(self):
        """Teardown for terminal buffer performance test."""
        try:
            if os.path.exists(TESTING_FOLDER_PATH):
                # Clean up all content
                for item in os.listdir(TESTING_FOLDER_PATH):
                    item_path = os.path.join(TESTING_FOLDER_PATH, item)
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
        except Exception as e:
            print(f"⚠️ Cleanup warning: {e}")
            # Don't fail the test just because cleanup had issues