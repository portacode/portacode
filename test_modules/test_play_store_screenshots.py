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

    async def capture(self, test_instance, post_editor_steps=None) -> TestResult:
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
        await editor_button.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)
        try:
            await editor_button.click(force=True)
        except Exception as exc:
            return TestResult(
                test_instance.name,
                False,
                f"Failed to open editor for {self.device_label}: {exc}",
            )

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

        if post_editor_steps:
            result = await post_editor_steps(page, test_instance.playwright_manager)
            if result:
                return result

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
        return await self.logic.capture(self, self._capture_phone_views)

    async def teardown(self):
        pass

    async def _capture_phone_views(self, page, manager) -> TestResult:
        async def take(name: str):
            await page.wait_for_timeout(500)
            await manager.take_screenshot(f"{self.logic.device_name}_{name}")

        def locator_by_text(selector: str, text: str):
            return page.locator(selector).filter(has_text=text).first

        # Explorer tab
        explorer_tab = locator_by_text(".mobile-nav-item", "Explorer")
        try:
            await explorer_tab.wait_for(timeout=5000)
            await explorer_tab.click()
        except Exception as exc:
            return TestResult(self.name, False, f"Explorer tab not accessible: {exc}")
        await take("explorer")

        # Git status expansion
        git_info = page.locator(".git-branch-info").first
        try:
            await git_info.wait_for(timeout=5000)
            await git_info.click()
        except Exception as exc:
            return TestResult(self.name, False, f"Git status section unavailable: {exc}")
        await take("git_status")

        # Diff button
        diff_btn = page.locator(".git-action-btn.diff").first
        try:
            await diff_btn.wait_for(timeout=5000)
            await diff_btn.click()
        except Exception as exc:
            return TestResult(self.name, False, f"Diff action unavailable: {exc}")
        await page.wait_for_timeout(1000)
        await take("git_diff")

        # Terminal tab
        terminal_tab = locator_by_text(".mobile-nav-item", "Terminal")
        try:
            await terminal_tab.wait_for(timeout=5000)
            await terminal_tab.click()
        except Exception as exc:
            return TestResult(self.name, False, f"Terminal tab not accessible: {exc}")
        await take("terminal")

        # AI Chat tab
        ai_chat_tab = locator_by_text(".mobile-nav-item", "AI Chat")
        try:
            await ai_chat_tab.wait_for(timeout=5000)
            await ai_chat_tab.click()
        except Exception as exc:
            return TestResult(self.name, False, f"AI Chat tab not accessible: {exc}")
        await take("ai_chat")

        # First chat item
        chat_item = page.locator(".chat-item").first
        try:
            await chat_item.wait_for(timeout=5000)
            await chat_item.click()
        except Exception as exc:
            return TestResult(self.name, False, f"No AI chat history available: {exc}")
        await take("ai_chat_thread")

        return TestResult(
            self.name,
            True,
            "Phone screenshots captured across Explorer, Git, Diff, Terminal, and AI Chat",
        )

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
        return await self.logic.capture(self, self._capture_tablet_views)

    async def teardown(self):
        pass

    async def _capture_tablet_views(self, page, manager) -> TestResult:
        async def click_and_wait(locator, description: str, screenshot_name: str = None):
            try:
                await locator.wait_for(timeout=5000)
                await locator.click()
            except Exception as exc:
                return TestResult(self.name, False, f"Failed to interact with {description}: {exc}")
            if screenshot_name:
                await page.wait_for_timeout(500)
                await manager.take_screenshot(f"{self.logic.device_name}_{screenshot_name}")
            return None

        # Helper locators
        def divider_lid(title_text):
            return page.locator(f'.divider-lid[title="Toggle {title_text}"]').first

        def persistent_toggle(title_text):
            return page.locator(
                f'.persistent-toggle[title="Show {title_text}"], '
                f'.persistent-toggle[title="Hide {title_text}"]'
            ).first

        # 1. Close terminal, expand git, open diff, capture
        result = await click_and_wait(divider_lid("Terminal"), "Terminal divider")
        if result:
            return result
        git_info = page.locator(".git-branch-info").first
        result = await click_and_wait(git_info, "Git branch info", "git_status")
        if result:
            return result
        git_diff_btn = page.locator(".git-action-btn.diff").first
        result = await click_and_wait(git_diff_btn, "Git diff button", "git_version_control")
        if result:
            return result

        # 2. Expand AI chat and open first chat
        ai_chat_toggle = page.locator('.persistent-toggle.ai-chat-toggle')
        result = await click_and_wait(ai_chat_toggle, "AI Chat toggle")
        if result:
            return result
        chat_item = page.locator(".chat-item").first
        result = await click_and_wait(chat_item, "AI chat conversation", "ai_chat_thread_tablet")
        if result:
            return result

        # 3. Expand terminal, collapse file explorer, capture
        terminal_tab = page.locator('.persistent-toggle.terminal-toggle-center')
        result = await click_and_wait(terminal_tab, "Terminal toggle")
        if result:
            return result
        explorer_divider = page.locator('.divider-lid.horizontal[title="Toggle File Explorer"]').first
        result = await click_and_wait(explorer_divider, "Explorer divider", "terminal_focus")
        if result:
            return result

        # 4. Collapse AI chat and expand file explorer to reset
        return TestResult(
            self.name,
            True,
            "Tablet screenshots captured across Git, diff, terminal, and AI chat views",
        )
