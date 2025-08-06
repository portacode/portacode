"""Performance check test example."""

import asyncio
import time
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class PerformanceCheckTest(BaseTest):
    """Test basic performance metrics of the application."""
    
    def __init__(self):
        super().__init__(
            name="performance_check_test",
            category=TestCategory.PERFORMANCE,
            description="Check basic performance metrics like page load time and responsiveness",
            tags=["performance", "speed", "metrics"]
        )
    
    async def run(self) -> TestResult:
        """Execute performance check test."""
        try:
            page = self.playwright_manager.page
            
            if not page:
                return TestResult(self.name, False, "No active Playwright page")
            
            performance_metrics = {}
            
            # Test 1: Page load time
            start_time = time.time()
            await page.reload()
            await page.wait_for_load_state("networkidle")
            load_time = time.time() - start_time
            
            performance_metrics["page_load_time"] = load_time
            await self.playwright_manager.take_screenshot("performance_page_loaded")
            
            # Test 2: Get performance metrics from browser
            try:
                browser_metrics = await page.evaluate("""
                    () => {
                        const perfData = performance.getEntriesByType('navigation')[0];
                        return {
                            dns_lookup: perfData.domainLookupEnd - perfData.domainLookupStart,
                            connection_time: perfData.connectEnd - perfData.connectStart,
                            request_time: perfData.responseStart - perfData.requestStart,
                            response_time: perfData.responseEnd - perfData.responseStart,
                            dom_processing: perfData.domContentLoadedEventEnd - perfData.domContentLoadedEventStart,
                            total_load_time: perfData.loadEventEnd - perfData.navigationStart
                        };
                    }
                """)
                performance_metrics["browser_metrics"] = browser_metrics
            except Exception as e:
                self.logger.warning(f"Could not get browser performance metrics: {e}")
                performance_metrics["browser_metrics"] = None
            
            # Test 3: Interaction responsiveness
            responsiveness_tests = []
            
            # Find clickable elements to test responsiveness
            clickable_elements = await page.query_selector_all("button, a, [onclick], [role='button']")
            
            for i, element in enumerate(clickable_elements[:5]):  # Test first 5 elements
                try:
                    # Check if element is visible
                    if await element.is_visible():
                        start_time = time.time()
                        
                        # Hover over element
                        await element.hover()
                        hover_time = time.time() - start_time
                        
                        responsiveness_tests.append({
                            "element_index": i,
                            "hover_response_time": hover_time,
                            "responsive": hover_time < 0.1  # Consider responsive if under 100ms
                        })
                        
                        await asyncio.sleep(0.1)  # Small delay between tests
                        
                except Exception as e:
                    responsiveness_tests.append({
                        "element_index": i,
                        "error": str(e),
                        "responsive": False
                    })
            
            performance_metrics["responsiveness_tests"] = responsiveness_tests
            
            # Test 4: Network resource timing
            try:
                network_metrics = await page.evaluate("""
                    () => {
                        const resources = performance.getEntriesByType('resource');
                        return resources.map(resource => ({
                            name: resource.name,
                            duration: resource.duration,
                            size: resource.transferSize || 0,
                            type: resource.initiatorType
                        })).sort((a, b) => b.duration - a.duration).slice(0, 10);
                    }
                """)
                performance_metrics["slowest_resources"] = network_metrics
            except Exception as e:
                self.logger.warning(f"Could not get network metrics: {e}")
                performance_metrics["slowest_resources"] = []
            
            # Test 5: Memory usage (if available)
            try:
                memory_info = await page.evaluate("""
                    () => {
                        if (performance.memory) {
                            return {
                                used: performance.memory.usedJSHeapSize,
                                total: performance.memory.totalJSHeapSize,
                                limit: performance.memory.jsHeapSizeLimit
                            };
                        }
                        return null;
                    }
                """)
                performance_metrics["memory_usage"] = memory_info
            except Exception as e:
                self.logger.warning(f"Could not get memory metrics: {e}")
                performance_metrics["memory_usage"] = None
            
            await self.playwright_manager.log_action("performance_metrics_collected", {
                "metrics": performance_metrics
            })
            
            # Evaluate performance
            issues = []
            warnings = []
            
            # Check load time
            if load_time > 5.0:
                issues.append(f"Page load time too slow: {load_time:.2f}s")
            elif load_time > 3.0:
                warnings.append(f"Page load time slower than ideal: {load_time:.2f}s")
            
            # Check responsiveness
            responsive_count = sum(1 for test in responsiveness_tests if test.get("responsive", False))
            total_responsiveness_tests = len(responsiveness_tests)
            
            if total_responsiveness_tests > 0:
                responsiveness_rate = responsive_count / total_responsiveness_tests
                if responsiveness_rate < 0.5:
                    issues.append(f"Poor responsiveness: {responsive_count}/{total_responsiveness_tests} elements responsive")
                elif responsiveness_rate < 0.8:
                    warnings.append(f"Some responsiveness issues: {responsive_count}/{total_responsiveness_tests} elements responsive")
            
            # Check for slow resources
            slow_resources = [r for r in performance_metrics.get("slowest_resources", []) if r["duration"] > 1000]
            if slow_resources:
                warnings.append(f"Found {len(slow_resources)} slow-loading resources")
            
            # Determine result
            if issues:
                return TestResult(
                    self.name, False,
                    f"Performance issues detected: {'; '.join(issues)}",
                    artifacts={"performance_metrics": performance_metrics}
                )
            elif warnings:
                return TestResult(
                    self.name, True,
                    f"Performance acceptable with warnings: {'; '.join(warnings)}",
                    artifacts={"performance_metrics": performance_metrics}
                )
            else:
                return TestResult(
                    self.name, True,
                    f"Good performance: {load_time:.2f}s load time, {responsive_count}/{total_responsiveness_tests} responsive elements",
                    artifacts={"performance_metrics": performance_metrics}
                )
                
        except Exception as e:
            self.logger.error(f"Performance check test failed: {e}")
            return TestResult(self.name, False, f"Test execution failed: {str(e)}")
    
    async def setup(self):
        """Setup for performance test."""
        self.logger.info("Performance check test setup")
    
    async def teardown(self):
        """Teardown for performance test."""
        self.logger.info("Performance check test teardown completed")