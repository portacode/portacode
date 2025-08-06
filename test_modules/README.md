# Test Modules Guide

This directory contains test modules for the Portacode testing framework. Each test module defines specific test cases that can be executed individually or as part of test suites.

## 📝 Writing a Test Module

### Basic Structure

```python
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

class YourCustomTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="your_test_name",
            category=TestCategory.SMOKE,  # or UI, INTEGRATION, PERFORMANCE, etc.
            description="What this test validates",
            tags=["tag1", "tag2", "tag3"]  # For filtering tests
        )
    
    async def run(self) -> TestResult:
        """Main test logic - MUST return TestResult."""
        try:
            # Your test logic here
            page = self.playwright_manager.page
            
            # Perform test actions...
            
            # Return success
            return TestResult(self.name, True, "Test passed!")
            
        except Exception as e:
            # Return failure with error message
            return TestResult(self.name, False, f"Test failed: {str(e)}")
    
    async def setup(self):
        """Optional: Setup before test runs."""
        pass
        
    async def teardown(self):
        """Optional: Cleanup after test runs."""
        pass
```

## 🎭 Playwright Testing

### Available Managers

- **`self.playwright_manager`**: Browser automation
- **`self.cli_manager`**: CLI connection (shared across tests)

### Common Playwright Patterns

#### Navigation and URL Testing
```python
# Get current URL
current_url = page.url

# Navigate to a page
response = await page.goto("http://example.com/dashboard")

# Check response status
if response.status == 200:
    # Success
else:
    # Handle error
```

#### Element Interactions
```python
# Click elements
await page.click("button#submit")
await page.click("text=Login")

# Fill forms
await page.fill("input[name='username']", "testuser")
await page.fill("#password", "password123")

# Wait for elements
await page.wait_for_selector(".dashboard-content")
await page.wait_for_load_state("networkidle")
```

#### Assertions and Validations
```python
# Check if element exists
if await page.is_visible(".success-message"):
    # Element is visible
    
# Get text content
text = await page.text_content(".status")
if "Success" in text:
    # Validation passed

# Check URL patterns
if "/dashboard" in page.url:
    # On dashboard page

# Multiple elements
buttons = await page.query_selector_all("button")
if len(buttons) > 0:
    # Found buttons
```

#### Screenshots and Logging
```python
# Take screenshot
await self.playwright_manager.take_screenshot("step_name")

# Log actions
await self.playwright_manager.log_action("action_type", {
    "url": page.url,
    "status": "success",
    "data": {"key": "value"}
})
```

## 🔍 Proper Test Assertions

### Authentication Testing
```python
# Test 1: Check if already authenticated
if "/dashboard" in page.url:
    response = await page.goto(page.url)
    if response and response.status == 200:
        return TestResult(self.name, True, "Already authenticated")

# Test 2: Try accessing protected page
dashboard_url = base_url + "/dashboard/"
response = await page.goto(dashboard_url)
final_url = page.url

if "/dashboard" in final_url and response.status == 200:
    return TestResult(self.name, True, "Authentication successful")
elif "login" in final_url:
    return TestResult(self.name, False, "Not authenticated - redirected to login")
```

### HTTP Status Testing
```python
# Always check response status
response = await page.goto(target_url)
if response:
    if response.status == 200:
        # Success
    elif response.status in [301, 302]:
        # Redirect - check final URL
        if page.url != target_url:
            # Handle redirect
    else:
        return TestResult(self.name, False, f"HTTP {response.status}")
```

### Element Validation
```python
# Wait for elements to ensure they exist
try:
    await page.wait_for_selector(".dashboard-content", timeout=5000)
    return TestResult(self.name, True, "Dashboard loaded")
except:
    return TestResult(self.name, False, "Dashboard content not found")
```

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

```bash
# Single test
python -m testing_framework.cli run-tests your_test_name

# By category  
python -m testing_framework.cli run-category smoke

# By tags
python -m testing_framework.cli run-tags login authentication

# All tests
python -m testing_framework.cli run-all
```