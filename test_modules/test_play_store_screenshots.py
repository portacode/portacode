"""Play Store screenshot tests for phone and tablet layouts."""

import os
from urllib.parse import urljoin

from testing_framework.core.base_test import BaseTest, TestResult, TestCategory


DEVICE_LABEL = os.getenv("PLAY_STORE_DEVICE_LABEL", "Workshop Seat 01")


class PlayStoreScreenshotLogic:
    """Shared workflow for capturing dashboard and editor screenshots."""

    def __init__(self):
        self.device_label = DEVICE_LABEL
        self.device_name = os.getenv("SCREENSHOT_DEVICE_NAME", "default")
        self.dashboard_zoom = float(os.getenv("SCREENSHOT_ZOOM", "1.0"))

    async def apply_zoom(self, page):
        if self.dashboard_zoom != 1.0:
            percent = int(self.dashboard_zoom * 100)
            await page.evaluate(
                f"document.body.style.zoom='{percent}%'"
            )

    async def capture(self, test_instance) -> TestResult:
        page = test_instance.playwright_manager.page
        base_url = test_instance.playwright_manager.base_url

        # Ensure dashboard is loaded
        await page.goto(urljoin(base_url, "/dashboard/"))
        await page.wait_for_load_state("networkidle")

        # Locate specific device card and ensure it's online
        device_card = (
            page.locator(".device-card.online")
            .filter(has_text=self.device_label)
            .first
        )
        try:
            await device_card.wait_for(timeout=10000)
        except Exception:
            return TestResult(
                test_instance.name,
                False,
                f"Device '{self.device_label}' is not online or not visible",
            )

        # Scroll past navbar and capture dashboard screenshot
        await page.evaluate("window.scrollTo(0, 66)")
        await page.wait_for_timeout(500)
        await test_instance.playwright_manager.take_screenshot(
            f"{self.device_name}_dashboard"
        )

        # Open the editor from this device card
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

        # Handle LitElement/Shadow DOM editor readiness
        try:
            await page.wait_for_selector("ace-editor", timeout=15000)
            await page.wait_for_function(
                """
                () => {
                    const el = document.querySelector('ace-editor');
                    if (!el) return false;
                    const shadow = el.shadowRoot;
                    if (shadow && shadow.querySelector('.ace_editor')) return true;
                    return !!el.querySelector('.ace_editor');
                }
                """,
                timeout=20000,
            )
        except Exception:
            test_instance.logger.warning(
                "ACE editor shadow DOM not detected, proceeding with screenshot"
            )

        await page.wait_for_timeout(1000)
        await test_instance.playwright_manager.take_screenshot(
            f"{self.device_name}_editor"
        )

        return TestResult(
            test_instance.name,
            True,
            f"Screenshots captured for {self.device_name}",
        )


class PlayStorePhoneScreenshotTest(BaseTest):
    """Capture phone-friendly screenshots for Play Store listing."""

    def __init__(self):
        self.logic = PlayStoreScreenshotLogic()
        super().__init__(
            name="play_store_phone_screenshot_test",
            category=TestCategory.UI,
            description="Capture phone-friendly screenshots for Play Store listing",
            tags=["screenshots", "store", "phone"],
            depends_on=["login_flow_test"],
            start_url="/dashboard/",
        )

    async def setup(self):
        await self.logic.apply_zoom(self.playwright_manager.page)

    async def run(self) -> TestResult:
        return await self.logic.capture(self)

    async def teardown(self):
        pass


class PlayStoreTabletScreenshotTest(BaseTest):
    """Capture tablet-friendly screenshots for Play Store listing."""

    def __init__(self):
        self.logic = PlayStoreScreenshotLogic()
        super().__init__(
            name="play_store_tablet_screenshot_test",
            category=TestCategory.UI,
            description="Capture tablet-friendly screenshots for Play Store listing",
            tags=["screenshots", "store", "tablet"],
            depends_on=["login_flow_test"],
            start_url="/dashboard/",
        )

    async def setup(self):
        await self.logic.apply_zoom(self.playwright_manager.page)

    async def run(self) -> TestResult:
        return await self.logic.capture(self)

    async def teardown(self):
        pass
