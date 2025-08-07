# Test Modules Guide

This directory contains test modules for the **simplified** Portacode testing framework. The framework now supports **hierarchical test dependencies** and **easy assertions** for WebSocket messages, debug files, and more.

## 🆕 What's New - Simplified Framework

### ✨ Key Features
- **Hierarchical Dependencies**: Tests run in correct order automatically
- **Auto-Navigation**: `start_url` parameter ensures tests start from the right page
- **Simple Assertions**: Easy-to-use `assert_that()` helper
- **Debug File Inspection**: Built-in helpers for `client_sessions.json` and `project_state_debug.json`
- **WebSocket Debugging**: Detailed `websockets.json` logs for communication analysis
- **WebSocket Testing**: Assert on WebSocket messages easily
- **Auto Debug Mode**: CLI connects with `--debug` flag automatically
- **Full HD Recording**: High-quality video recording with proper viewport (1920x1080)

## 📝 Writing a Test Module

### Basic Structure with Dependencies

```python
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

class YourCustomTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="your_test_name",
            category=TestCategory.SMOKE,
            description="What this test validates",
            tags=["tag1", "tag2", "tag3"],
            depends_on=["login_flow_test"],  # Run after these tests
            start_url="/dashboard/"         # Auto-navigate to this URL before test
        )
    
    async def run(self) -> TestResult:
        """Main test logic with simple assertions."""
        page = self.playwright_manager.page
        assert_that = self.assert_that()  # Get assertion helper
        
        # Simple assertions
        response = await page.goto("/dashboard")
        assert_that.status_ok(response, "Dashboard request")
        assert_that.url_contains(page, "/dashboard", "Dashboard URL")
        
        # Check debug files
        assert_that.debug_file_contains("client_sessions.json", "status", "active")
        
        # Return result based on assertions
        if assert_that.has_failures():
            return TestResult(self.name, False, assert_that.get_failure_message())
        
        return TestResult(self.name, True, "Test passed!")
```

## 🎯 Easy Assertions

### Available Assertion Methods

```python
assert_that = self.assert_that()  # Get assertion helper

# Basic assertions
assert_that.eq(actual, expected, "Custom message")
assert_that.contains(container, item, "Should contain item")
assert_that.is_true(value, "Should be truthy")
assert_that.is_false(value, "Should be falsy")

# HTTP assertions
assert_that.status_ok(response, "Request should succeed")

# Page assertions  
assert_that.url_contains(page, "/dashboard", "Should be on dashboard")
await assert_that.element_visible(page, ".success-message", "Success shown")

# WebSocket assertions
messages = [...] # Your WebSocket message list
assert_that.websocket_message(messages, "connection_established")
assert_that.websocket_message(messages, "file_update", {"file": "test.py"})

# Debug file assertions
assert_that.debug_file_contains("client_sessions.json", "status", "active")
assert_that.debug_file_contains("project_state_debug.json", "file_count")

# Check results
if assert_that.has_failures():
    return TestResult(self.name, False, assert_that.get_failure_message())
```

## 🔍 Debug File Inspection & WebSocket Debugging

### Built-in Inspector Helpers

```python
inspector = self.inspect()  # Get debug inspector

# Load debug files
sessions = inspector.load_client_sessions()      # client_sessions.json  
project_state = inspector.load_project_state()  # project_state_debug.json

# Get specific data
active_sessions = inspector.get_active_sessions()     # List of active session IDs
session_info = inspector.get_session_info("sess_123") # Info for specific session
project_files = inspector.get_project_files()        # List of project files

# Use in assertions
assert_that.is_true(len(active_sessions) > 0, "Should have active sessions")
```

### WebSocket Debugging

Each test generates `websockets.json` with all WebSocket messages:

```json
[
  {
    "timestamp": "2025-08-07T07:37:46.712124",
    "type": "message_sent", 
    "url": "ws://localhost:8001/ws/terminal/channel_123/",
    "data": {"type": "command", "data": "ls -la"}
  }
]
```

Located in: `test_results/run_TIMESTAMP/recordings/session_NAME/websockets.json`

## 🔗 Hierarchical Dependencies

### Dependency Types

```python
class MyTest(BaseTest):
    def __init__(self):
        super().__init__(
            # ... other params ...
            depends_on=["login_flow_test", "ide_launch_test"]  # Explicit dependencies
        )
    
    async def run(self) -> TestResult:
        # Access dependency results
        login_result = self.get_dependency_result("login_flow_test")
        if login_result and login_result.success:
            # Login was successful, proceed
            pass
```

### Dependency Resolution

The framework automatically:
- **Sorts tests** in dependency order (topological sort)
- **Skips tests** whose dependencies failed  
- **Prevents circular dependencies**

## 🧭 Auto-Navigation with start_url

```python
class DashboardTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="dashboard_test",
            start_url="/dashboard/"  # Relative URL - auto-navigate before test runs
        )
```

- Use **relative URLs** like `/dashboard/`, `/project/123/`
- Framework navigates only if current page differs
- Prevents tests breaking from previous test page states

## 📂 Test Categories

- **`SMOKE`**: Basic functionality tests
- **`INTEGRATION`**: Cross-system tests
- **`UI`**: User interface tests
- **`API`**: API endpoint tests
- **`PERFORMANCE`**: Speed and load tests
- **`SECURITY`**: Security validation tests

## 🏷️ Test Tags

Use tags for flexible test filtering:
```python
tags=["login", "authentication", "smoke", "critical"]
```

Run tests by tags:
```bash
python -m testing_framework.cli run-tags login authentication
```

## ✅ Test Result Best Practices

### Success Criteria
- Always verify the expected outcome occurred
- Check HTTP status codes (200 for success)
- Validate redirects go to expected URLs
- Confirm elements/content are present

### Failure Handling
```python
try:
    # Test logic
    if not expected_condition:
        return TestResult(self.name, False, "Specific failure reason")
    return TestResult(self.name, True, "Success message")
except Exception as e:
    return TestResult(self.name, False, f"Exception: {str(e)}")
```

### Error Messages
- Be specific about what failed
- Include relevant URLs, status codes, or element selectors
- Help debugging with clear context

## 📋 Examples

### Login Test (Proper)
```python
async def run(self) -> TestResult:
    page = self.playwright_manager.page
    
    # Try accessing dashboard directly
    response = await page.goto(f"{base_url}/dashboard/")
    final_url = page.url
    
    if "/dashboard" in final_url and response.status == 200:
        return TestResult(self.name, True, f"Authenticated - Dashboard accessible")
    elif "login" in final_url:
        return TestResult(self.name, False, "Not authenticated - redirected to login")
    else:
        return TestResult(self.name, False, f"Unexpected response: {response.status}")
```

### Form Submission Test
```python
async def run(self) -> TestResult:
    page = self.playwright_manager.page
    
    # Fill form
    await page.fill("#email", "test@example.com")
    await page.fill("#message", "Test message")
    
    # Submit and wait for response
    await page.click("button[type='submit']")
    await page.wait_for_load_state("networkidle")
    
    # Check for success indicator
    if await page.is_visible(".success-alert"):
        return TestResult(self.name, True, "Form submitted successfully")
    elif await page.is_visible(".error-alert"):
        error_text = await page.text_content(".error-alert")
        return TestResult(self.name, False, f"Form error: {error_text}")
    else:
        return TestResult(self.name, False, "No response indicator found")
```

## 🔧 File Naming

- Files: `test_feature_name.py`
- Classes: `FeatureNameTest`
- Test names: `feature_name_test`

## 🚀 Running Tests

### Hierarchical Mode (Recommended)

```bash
# All tests with dependency resolution
python -m testing_framework.cli run-hierarchical

# Specific tests with dependencies 
python -m testing_framework.cli run-hierarchical-tests login_flow_test websocket_test

# All tests with hierarchical option
python -m testing_framework.cli run-all --hierarchical
```

### Standard Mode

```bash
# Single test
python -m testing_framework.cli run-tests your_test_name

# By category  
python -m testing_framework.cli run-category smoke

# By tags
python -m testing_framework.cli run-tags login authentication

# All tests (no dependencies)
python -m testing_framework.cli run-all
```

### CLI Features

- **Auto Debug Mode**: CLI connects with `--debug` flag automatically
- **Dependency Analysis**: Shows which tests were skipped and why
- **Shared Connection**: All tests share one CLI connection for efficiency