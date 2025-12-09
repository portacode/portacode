"""Play Store screenshot capture test."""

import os
from urllib.parse import urljoin

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


class PlayStoreScreenshotTest(BaseTest):
    """Capture curated UI screenshots for store listings."""

    def __init__(self):
        self.device_name = os.getenv("SCREENSHOT_DEVICE_NAME", "default")
        self.dashboard_zoom = float(os.getenv("SCREENSHOT_ZOOM", "1.0"))
        super().__init__(
            name="play_store_screenshot_test",
            category=TestCategory.UI,
            description="Capture dashboard and editor screenshots for store listings",
            tags=["screenshots", "store", "ui"],
            depends_on=["login_flow_test"],
            start_url="/dashboard/",
        )

    async def setup(self):
        """Apply optional zoom before interactions."""
        if self.dashboard_zoom != 1.0:
            percent = int(self.dashboard_zoom * 100)
            await self.playwright_manager.page.evaluate(
                f"document.body.style.zoom='{percent}%'"
            )

    async def run(self) -> TestResult:
        page = self.playwright_manager.page
        base_url = self.playwright_manager.base_url

        # Ensure dashboard is loaded
        await page.goto(urljoin(base_url, "/dashboard/"))
        await page.wait_for_load_state("networkidle")

        # Locate ubuntu-01 card and ensure online
        device_card = (
            page.locator(".device-card.online")
            .filter(has_text="ubuntu-01")
            .first
        )
        try:
            await device_card.wait_for(timeout=10000)
        except Exception:
            return TestResult(
                self.name,
                False,
                "Device ubuntu-01 is not online or not visible",
            )

        # Scroll past the navbar (66px) and capture dashboard screenshot
        await page.evaluate("window.scrollTo(0, 66)")
        await page.wait_for_timeout(500)
        await self.playwright_manager.take_screenshot(
            f"{self.device_name}_dashboard"
        )

        # Open the editor from the ubuntu-01 card
        editor_button = device_card.get_by_text("Editor")
        await editor_button.wait_for(timeout=5000)
        await editor_button.click()

        # Select the first project in the modal
        await page.wait_for_selector("#projectSelectorModal.show", timeout=10000)
        await page.wait_for_selector(".item.project", timeout=10000)
        first_project = page.locator(".item.project").first
        await first_project.click()
        await page.wait_for_selector(
            "#projectSelectorModal.show",
            state="hidden",
            timeout=10000,
        )

        # Wait for IDE to appear with device online indicator
        await page.locator(".system-resources").wait_for(timeout=10000)


        await page.wait_for_timeout(1000)
        await self.playwright_manager.take_screenshot(
            f"{self.device_name}_editor"
        )

        return TestResult(
            self.name,
            True,
            f"Screenshots captured for {self.device_name}",
        )

    async def teardown(self):
        """No teardown actions required."""
        pass
