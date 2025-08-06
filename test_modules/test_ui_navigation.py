"""UI navigation test example."""

import asyncio
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class UINavigationTest(BaseTest):
    """Test basic UI navigation functionality."""
    
    def __init__(self):
        super().__init__(
            name="ui_navigation_test",
            category=TestCategory.UI,
            description="Test navigation between different sections of the application",
            tags=["ui", "navigation", "frontend"]
        )
    
    async def run(self) -> TestResult:
        """Execute UI navigation test."""
        try:
            page = self.playwright_manager.page
            
            if not page:
                return TestResult(self.name, False, "No active Playwright page")
            
            navigation_results = []
            
            # Test 1: Take initial screenshot
            await self.playwright_manager.take_screenshot("navigation_start")
            initial_url = page.url
            
            # Test 2: Look for navigation elements
            nav_selectors = [
                "nav", ".navbar", ".navigation", ".menu", ".sidebar",
                "a[href]", "button[onclick]", "[role='navigation']"
            ]
            
            found_nav_elements = []
            for selector in nav_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    if elements:
                        found_nav_elements.append({
                            "selector": selector,
                            "count": len(elements)
                        })
                except:
                    continue
            
            await self.playwright_manager.log_action("navigation_elements_found", {
                "elements": found_nav_elements
            })
            
            # Test 3: Try to find and click navigation links
            clickable_elements = await page.query_selector_all("a[href]:not([href^='#']):not([href^='javascript:'])")
            
            navigation_attempts = 0
            successful_navigations = 0
            
            for i, element in enumerate(clickable_elements[:3]):  # Test first 3 links
                try:
                    href = await element.get_attribute('href')
                    text = await element.text_content()
                    
                    if href and not href.startswith('mailto:') and not href.startswith('tel:'):
                        navigation_attempts += 1
                        
                        await self.playwright_manager.log_action("attempting_navigation", {
                            "link_text": text,
                            "href": href,
                            "attempt": navigation_attempts
                        })
                        
                        # Click the link
                        await element.click()
                        
                        # Wait for navigation
                        await page.wait_for_load_state("networkidle", timeout=5000)
                        
                        new_url = page.url
                        if new_url != initial_url:
                            successful_navigations += 1
                            await self.playwright_manager.take_screenshot(f"navigation_success_{navigation_attempts}")
                            
                            navigation_results.append({
                                "success": True,
                                "from_url": initial_url,
                                "to_url": new_url,
                                "link_text": text
                            })
                            
                            # Navigate back
                            await page.go_back()
                            await page.wait_for_load_state("networkidle", timeout=5000)
                            
                        else:
                            navigation_results.append({
                                "success": False,
                                "reason": "URL did not change",
                                "link_text": text,
                                "href": href
                            })
                        
                        # Small delay between attempts
                        await asyncio.sleep(1)
                        
                except Exception as e:
                    navigation_results.append({
                        "success": False,
                        "reason": f"Click failed: {str(e)}",
                        "link_text": text if 'text' in locals() else "unknown"
                    })
                    
                if navigation_attempts >= 3:  # Limit attempts
                    break
            
            # Test 4: Check for responsive elements
            await page.set_viewport_size({"width": 768, "height": 1024})  # Tablet size
            await self.playwright_manager.take_screenshot("responsive_tablet")
            
            await page.set_viewport_size({"width": 375, "height": 667})  # Mobile size  
            await self.playwright_manager.take_screenshot("responsive_mobile")
            
            await page.set_viewport_size({"width": 1920, "height": 1080})  # Desktop size
            await self.playwright_manager.take_screenshot("responsive_desktop")
            
            # Final evaluation
            await self.playwright_manager.log_action("navigation_test_summary", {
                "total_nav_elements": len(found_nav_elements),
                "navigation_attempts": navigation_attempts,
                "successful_navigations": successful_navigations,
                "navigation_results": navigation_results
            })
            
            if successful_navigations > 0:
                return TestResult(
                    self.name, True,
                    f"Navigation test passed. {successful_navigations}/{navigation_attempts} navigations successful."
                )
            elif navigation_attempts == 0:
                return TestResult(
                    self.name, False,
                    "No navigable links found on the page."
                )
            else:
                return TestResult(
                    self.name, False,
                    f"Navigation test failed. 0/{navigation_attempts} navigations successful."
                )
                
        except Exception as e:
            self.logger.error(f"UI navigation test failed: {e}")
            return TestResult(self.name, False, f"Test execution failed: {str(e)}")
    
    async def setup(self):
        """Setup for navigation test."""
        self.logger.info("UI navigation test setup")
    
    async def teardown(self):
        """Teardown for navigation test."""  
        self.logger.info("UI navigation test teardown completed")