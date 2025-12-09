"""Playwright session management with comprehensive recording and logging."""

import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
import logging
import json
import time
from datetime import datetime
from urllib.parse import urlparse

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class PlaywrightManager:
    """Manages Playwright sessions with comprehensive recording and logging."""
    
    def __init__(self, test_name: str, recordings_dir: str = "test_recordings"):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("Playwright is not installed. Run: pip install playwright")
            
        self.test_name = test_name
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(exist_ok=True)
        
        # Create subdirectory for this test
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.test_recordings_dir = self.recordings_dir / f"{test_name}_{timestamp}"
        self.test_recordings_dir.mkdir(exist_ok=True)
        
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        self.logger = logging.getLogger(f"playwright_manager.{test_name}")
        self.logger.setLevel(logging.WARNING)  # Only show warnings and errors
        
        # Recording and logging paths
        self.video_path = self.test_recordings_dir / "recording.webm"
        self.screenshot_dir = self.test_recordings_dir / "screenshots"
        self.screenshot_dir.mkdir(exist_ok=True)
        self.trace_path = self.test_recordings_dir / "trace.zip"
        self.har_path = self.test_recordings_dir / "network.har"
        self.console_log_path = self.test_recordings_dir / "console.log"
        self.actions_log_path = self.test_recordings_dir / "actions.json"
        self.websocket_log_path = self.test_recordings_dir / "websockets.json"

        # Action tracking
        self.actions_log: List[Dict[str, Any]] = []
        self.screenshot_counter = 0
        self.websocket_logs: List[Dict[str, Any]] = []

    async def start_session(self,
                          url: Optional[str] = None,
                          username: Optional[str] = None,
                          password: Optional[str] = None,
                          browser_type: Optional[str] = None,
                          headless: Optional[bool] = None) -> bool:
        """Start Playwright session with comprehensive recording."""
        try:
            # Load environment variables
            env_url = os.getenv('TEST_BASE_URL', 'http://192.168.1.188:8001/')
            env_username = os.getenv('TEST_USERNAME')
            env_password = os.getenv('TEST_PASSWORD')
            env_browser = os.getenv('TEST_BROWSER', 'chromium')
            env_headless = os.getenv('TEST_HEADLESS', 'false').lower() in ('true', '1', 'yes')
            env_video_width = int(os.getenv('TEST_VIDEO_WIDTH', '1920'))
            env_video_height = int(os.getenv('TEST_VIDEO_HEIGHT', '1080'))
            env_viewport_width = os.getenv('TEST_VIEWPORT_WIDTH')
            env_viewport_height = os.getenv('TEST_VIEWPORT_HEIGHT')
            env_device_scale = os.getenv('TEST_DEVICE_SCALE_FACTOR')
            env_is_mobile = os.getenv('TEST_IS_MOBILE')
            env_has_touch = os.getenv('TEST_HAS_TOUCH')
            env_user_agent = os.getenv('TEST_USER_AGENT')
            automation_token = os.getenv('TEST_RUNNER_BYPASS_TOKEN')
            
            # Use provided values or fall back to environment
            self.base_url = url or env_url
            self.username = username or env_username
            self.password = password or env_password
            browser_type = browser_type or env_browser
            headless = headless if headless is not None else env_headless

            if not self.username or not self.password:
                self.logger.error("Username and password must be provided via parameters or environment variables")
                return False

            self.logger.info(f"Starting Playwright session for test: {self.test_name}")
            self.logger.info(f"Target URL: {self.base_url}")
            self.logger.info(f"Browser: {browser_type}, Headless: {headless}")

            # Start Playwright
            try:
                self.playwright = await async_playwright().start()
                self.logger.info("Playwright started successfully")
            except Exception as e:
                raise Exception(f"Failed to start Playwright: {e}")

            # Launch browser with optimized settings for video recording
            try:
                # Common args for better video recording quality
                launch_args = [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--disable-gpu' if headless else '--force-gpu-mem-available-mb=2048',
                    '--no-sandbox',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding'
                ]
                
                if browser_type == "firefox":
                    self.browser = await self.playwright.firefox.launch(
                        headless=headless,
                        args=launch_args[:3]  # Firefox doesn't support all Chromium args
                    )
                elif browser_type == "webkit":
                    self.browser = await self.playwright.webkit.launch(headless=headless)
                else:
                    self.browser = await self.playwright.chromium.launch(
                        headless=headless,
                        args=launch_args
                    )
                self.logger.info(f"Browser ({browser_type}) launched successfully with optimized recording settings")
            except Exception as e:
                raise Exception(f"Failed to launch {browser_type} browser: {e}")

            # Create context with recording enabled and proper viewport
            viewport_size = {
                "width": int(env_viewport_width) if env_viewport_width else env_video_width,
                "height": int(env_viewport_height) if env_viewport_height else env_video_height
            }
            video_size = {"width": env_video_width, "height": env_video_height}
            context_kwargs = {
                "record_video_dir": str(self.test_recordings_dir),
                "record_video_size": video_size,
                "record_har_path": str(self.har_path),
                "record_har_omit_content": False,
                "viewport": viewport_size
            }

            if env_device_scale:
                try:
                    context_kwargs["device_scale_factor"] = float(env_device_scale)
                except ValueError:
                    self.logger.warning(f"Invalid TEST_DEVICE_SCALE_FACTOR '{env_device_scale}' - ignoring")

            if env_is_mobile:
                context_kwargs["is_mobile"] = env_is_mobile.lower() in ('true', '1', 'yes')

            if env_has_touch:
                context_kwargs["has_touch"] = env_has_touch.lower() in ('true', '1', 'yes')

            if env_user_agent:
                context_kwargs["user_agent"] = env_user_agent

            self.context = await self.browser.new_context(**context_kwargs)
            self.logger.info(
                "Viewport configured: %sx%s (device scale: %s, mobile: %s, touch: %s)",
                viewport_size["width"],
                viewport_size["height"],
                context_kwargs.get("device_scale_factor", 1.0),
                context_kwargs.get("is_mobile", False),
                context_kwargs.get("has_touch", False),
            )
            if automation_token:
                parsed_base = urlparse(self.base_url)
                target_host = parsed_base.hostname
                target_scheme = parsed_base.scheme or "http"
                header_name = "X-Portacode-Automation"

                async def automation_header_route(route, request):
                    headers = dict(request.headers)
                    parsed_request = urlparse(request.url)
                    if parsed_request.hostname == target_host and parsed_request.scheme == target_scheme:
                        headers[header_name] = automation_token
                    else:
                        headers.pop(header_name, None)
                    await route.continue_(headers=headers)

                await self.context.route("**/*", automation_header_route)
                self.logger.info("Automation bypass header restricted to same-origin requests")

            self.logger.info(f"Video recording configured: {env_video_width}x{env_video_height}")

            # Start tracing
            await self.context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=True
            )

            # Create page
            self.page = await self.context.new_page()

            # Set up console logging
            self.console_logs = []
            self.page.on("console", self._handle_console_message)

            # Set up WebSocket logging
            self.page.on("websocket", self._handle_websocket)

            # Set up request/response logging
            self.page.on("request", self._handle_request)
            self.page.on("response", self._handle_response)

            # Navigate to base URL
            await self.log_action("navigate", {"url": self.base_url})
            await self.page.goto(self.base_url)
            await self.take_screenshot("initial_load")
            
            # Perform login if credentials provided
            if self.username and self.password:
                await self._perform_login()
            
            self.logger.info("Playwright session started successfully")
            return True
            
        except Exception as e:
            error_msg = f"Failed to start Playwright session: {e}"
            self.logger.error(error_msg)
            await self.cleanup()
            return False
    
    async def _perform_login(self):
        """Perform login using provided credentials."""
        try:
            # Navigate to login page first
            login_url = f"{self.base_url}accounts/login/"
            await self.page.goto(login_url)
            await self.log_action("navigate_to_login", {"url": login_url})
            await self.take_screenshot("login_page")

            await self.log_action("login_start", {"username": self.username})

            # Look for common login form elements
            username_selectors = [
                'input[name="username"]',
                'input[name="email"]', 
                'input[type="email"]',
                'input[id="username"]',
                'input[id="email"]',
                '#id_username',
                '#id_email'
            ]
            
            password_selectors = [
                'input[name="password"]',
                'input[type="password"]',
                'input[id="password"]',
                '#id_password'
            ]
            
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Login")',
                'button:has-text("Sign In")',
                '.btn-primary',
                '#login-button'
            ]
            
            # Find and fill username
            username_filled = False
            for selector in username_selectors:
                try:
                    if await self.page.is_visible(selector):
                        await self.page.fill(selector, self.username)
                        await self.log_action("fill_username", {"selector": selector})
                        username_filled = True
                        break
                except:
                    continue
            
            if not username_filled:
                raise Exception("Could not find username input field")
            
            # Find and fill password  
            password_filled = False
            for selector in password_selectors:
                try:
                    if await self.page.is_visible(selector):
                        await self.page.fill(selector, self.password)
                        await self.log_action("fill_password", {"selector": selector})
                        password_filled = True
                        break
                except:
                    continue
            
            if not password_filled:
                raise Exception("Could not find password input field")
                
            await self.take_screenshot("login_form_filled")
            
            # Submit form
            submitted = False
            for selector in submit_selectors:
                try:
                    if await self.page.is_visible(selector):
                        await self.page.click(selector)
                        await self.log_action("click_submit", {"selector": selector})
                        submitted = True
                        break
                except:
                    continue
            
            if not submitted:
                # Try pressing Enter on password field
                for selector in password_selectors:
                    try:
                        await self.page.press(selector, "Enter")
                        await self.log_action("press_enter", {"selector": selector})
                        submitted = True
                        break
                    except:
                        continue
            
            if not submitted:
                raise Exception("Could not submit login form")
            
            # Wait for navigation or login success
            await self.page.wait_for_load_state("networkidle")
            await self.take_screenshot("post_login")
            
            await self.log_action("login_complete", {"success": True})
            self.logger.info("Login completed successfully")
            
        except Exception as e:
            await self.log_action("login_error", {"error": str(e)})
            self.logger.error(f"Login failed: {e}")
            await self.take_screenshot("login_error")
            raise
    
    async def take_screenshot(self, name: str) -> Path:
        """Take a screenshot with automatic naming."""
        if not self.page:
            raise RuntimeError("No active page for screenshot")
            
        self.screenshot_counter += 1
        screenshot_path = self.screenshot_dir / f"{self.screenshot_counter:03d}_{name}.png"
        
        await self.page.screenshot(path=str(screenshot_path))
        await self.log_action("screenshot", {
            "name": name,
            "path": str(screenshot_path),
            "counter": self.screenshot_counter
        })
        
        self.logger.info(f"Screenshot saved: {screenshot_path}")
        return screenshot_path
    
    async def log_action(self, action_type: str, details: Dict[str, Any]):
        """Log an action with timestamp and details."""
        action_entry = {
            "timestamp": datetime.now().isoformat(),
            "action_type": action_type,
            "details": details
        }
        
        self.actions_log.append(action_entry)
        
        # Write to actions log file
        try:
            with open(self.actions_log_path, 'w') as f:
                json.dump(self.actions_log, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to write actions log: {e}")
    
    async def log_timeline_marker(self, phase: str, description: str = ""):
        """Log a timeline marker for better test debugging and trace correlation."""
        timestamp = datetime.now().isoformat()
        marker_details = {
            "phase": phase,
            "description": description,
            "timestamp": timestamp
        }
        
        # Log to actions for timeline tracking
        await self.log_action("TIMELINE_MARKER", marker_details)
        
        # Also log to console for visibility in trace viewer
        if self.page:
            try:
                # Inject a console log into the page that will show up in traces
                script = f"""
                console.log('🧪 TEST PHASE: {phase}' + ({repr(description)} ? ' - ' + {repr(description)} : ''));
                """
                asyncio.create_task(self.page.evaluate(script))
            except Exception as e:
                self.logger.warning(f"Could not inject timeline marker into page: {e}")
    
    def _handle_console_message(self, msg):
        """Handle console messages from the page."""
        console_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": msg.type,
            "text": msg.text
        }
        self.console_logs.append(console_entry)
        
        # Write to console log file
        try:
            with open(self.console_log_path, 'w') as f:
                json.dump(self.console_logs, f, indent=2)
        except:
            pass

    def _handle_websocket(self, websocket):
        """Handle WebSocket connections."""
        self.logger.info(f"WebSocket opened: {websocket.url}")
        websocket.on("framesent", lambda payload: self._log_websocket_message("sent", websocket.url, payload))
        websocket.on("framereceived", lambda payload: self._log_websocket_message("received", websocket.url, payload))
        websocket.on("close", lambda: self.logger.info(f"WebSocket closed: {websocket.url}"))

    def _log_websocket_message(self, direction: str, url: str, payload: Any):
        """Log a WebSocket message."""
        try:
            parsed_payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            parsed_payload = payload

        message_entry = {
            "timestamp": datetime.now().isoformat(),
            "direction": direction,
            "url": url,
            "payload": parsed_payload
        }
        self.websocket_logs.append(message_entry)

        try:
            with open(self.websocket_log_path, 'w') as f:
                json.dump(self.websocket_logs, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to write websocket log: {e}")

    def _handle_request(self, request):
        """Handle network requests."""
        self.logger.debug(f"Request: {request.method} {request.url}")
    
    def _handle_response(self, response):
        """Handle network responses.""" 
        self.logger.debug(f"Response: {response.status} {response.url}")
    
    async def cleanup(self):
        """Clean up Playwright resources and finalize recordings."""
        try:
            if self.context:
                # Stop tracing
                await self.context.tracing.stop(path=str(self.trace_path))
                
            if self.page:
                await self.page.close()
                
            if self.context:
                await self.context.close()
                
            if self.browser:
                await self.browser.close()
                
            if self.playwright:
                await self.playwright.stop()
            
            # Generate summary report
            await self._generate_summary_report()
            
            self.logger.info(f"Playwright session cleaned up. Recordings saved to: {self.test_recordings_dir}")
            
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
    
    async def _generate_summary_report(self):
        """Generate a summary report of the test session."""
        try:
            summary = {
                "test_name": self.test_name,
                "start_time": self.actions_log[0]["timestamp"] if self.actions_log else None,
                "end_time": datetime.now().isoformat(),
                "total_actions": len(self.actions_log),
                "total_screenshots": self.screenshot_counter,
                "total_console_logs": len(getattr(self, 'console_logs', [])),
                "total_websocket_logs": len(self.websocket_logs),
                "recordings": {
                    "video": str(self.video_path) if self.video_path.exists() else None,
                    "trace": str(self.trace_path) if self.trace_path.exists() else None,
                    "har": str(self.har_path) if self.har_path.exists() else None,
                    "screenshots_dir": str(self.screenshot_dir),
                    "console_log": str(self.console_log_path),
                    "actions_log": str(self.actions_log_path),
                    "websocket_log": str(self.websocket_log_path)
                }
            }
            
            summary_path = self.test_recordings_dir / "summary.json"
            with open(summary_path, 'w') as f:
                json.dump(summary, f, indent=2)
                
            self.logger.info(f"Summary report generated: {summary_path}")
            
        except Exception as e:
            self.logger.error(f"Failed to generate summary report: {e}")
    
    def get_recordings_info(self) -> Dict[str, Any]:
        """Get information about all recordings for this test."""
        return {
            "test_name": self.test_name,
            "recordings_dir": str(self.test_recordings_dir),
            "video_path": str(self.video_path) if self.video_path.exists() else None,
            "trace_path": str(self.trace_path) if self.trace_path.exists() else None,
            "har_path": str(self.har_path) if self.har_path.exists() else None,
            "screenshot_count": self.screenshot_counter,
            "actions_count": len(self.actions_log),
            "console_logs_count": len(getattr(self, 'console_logs', [])),
            "websocket_logs_count": len(self.websocket_logs)
        }
