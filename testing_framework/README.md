# Modular Testing Framework

A comprehensive, modular testing framework designed specifically for Portacode projects that combines CLI connection management with Playwright-based web automation testing.

## 🚀 Features

- **Modular Architecture**: Organize tests by categories and tags
- **CLI Integration**: Automatically connects to Portacode CLI in background threads  
- **Playwright Automation**: Full web browser automation with comprehensive recording
- **Selective Execution**: Run all tests, specific categories, tags, or individual tests
- **Comprehensive Recording**: Screenshots, videos, network logs, console output, and traces
- **Rich Reporting**: HTML and JSON reports with detailed test results

## 📋 Quick Start

### 1. Installation

```bash
# Install testing framework dependencies
pip install -r requirements-testing.txt

# Install Playwright browsers
python -m playwright install
```

### 2. Environment Setup

Copy the example environment file and configure it:

```bash
cp .env.example .env
```

Edit `.env` with your configuration:

```env
TEST_BASE_URL=http://192.168.1.188:8001/
TEST_USERNAME=your_username
TEST_PASSWORD=your_password
```

### 3. Run Tests

```bash
# List all available tests
python -m testing_framework.cli list-tests

# Run all tests
python -m testing_framework.cli run-all

# Run tests by category
python -m testing_framework.cli run-category smoke

# Run tests by tags
python -m testing_framework.cli run-tags login authentication

# Run specific tests
python -m testing_framework.cli run-tests login_flow_test device_connection_test

# Run tests matching a pattern
python -m testing_framework.cli run-pattern "login.*"
```

## 📁 Project Structure

```
testing_framework/
├── __init__.py                 # Framework exports
├── cli.py                     # Command-line interface
└── core/
    ├── __init__.py
    ├── base_test.py           # Base test class and categories
    ├── cli_manager.py         # CLI connection management
    ├── playwright_manager.py  # Playwright session management
    ├── test_discovery.py      # Test discovery system
    └── runner.py              # Test runner with reporting

test_modules/
├── __init__.py
├── test_login_flow.py         # Login flow test example
├── test_device_connection.py  # Device connection test
├── test_ui_navigation.py      # UI navigation test
└── test_performance_check.py  # Performance test example
```

## 🔧 Framework Architecture

### Test Execution Flow

1. **CLI Connection**: Each test starts by connecting to Portacode CLI in a background thread
2. **Playwright Session**: Browser session starts and automatically logs in using provided credentials
3. **Test Execution**: Your test logic runs with access to both CLI and browser automation
4. **Recording**: Everything is recorded - screenshots, videos, network traffic, console logs
5. **Cleanup**: Resources are properly cleaned up and recordings are saved

### Test Categories

- `SMOKE`: Basic functionality tests
- `INTEGRATION`: Cross-system integration tests  
- `UI`: User interface tests
- `API`: API endpoint tests
- `PERFORMANCE`: Performance and load tests
- `SECURITY`: Security-focused tests
- `CUSTOM`: Custom test categories

## ✍️ Writing Custom Tests

### Basic Test Structure

```python
from testing_framework.core.base_test import BaseTest, TestResult, TestCategory

class MyCustomTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="my_custom_test",
            category=TestCategory.SMOKE,
            description="Description of what this test does",
            tags=["tag1", "tag2"]
        )
    
    async def run(self) -> TestResult:
        # Your test logic here
        page = self.playwright_manager.page
        
        # Take screenshots
        await self.playwright_manager.take_screenshot("test_step_1")
        
        # Interact with the page
        await page.click("button")
        
        # Check CLI connection
        if self.cli_manager.is_connection_active():
            # CLI is connected and working
            pass
            
        # Return test result
        return TestResult(self.name, True, "Test passed!")
    
    async def setup(self):
        # Optional setup code
        pass
    
    async def teardown(self):
        # Optional cleanup code
        pass
```

### Available Managers

#### CLI Manager (`self.cli_manager`)

- `is_connection_active()`: Check if CLI connection is active
- `get_connection_info()`: Get connection details
- `get_log_content()`: Get CLI output logs

#### Playwright Manager (`self.playwright_manager`)

- `page`: Direct access to Playwright page object
- `take_screenshot(name)`: Take named screenshot
- `log_action(type, details)`: Log custom actions
- `get_recordings_info()`: Get recording information

## 📊 Test Output

### Directory Structure

```
test_results/
└── run_20241201_143022/
    ├── summary.json           # Test run summary
    ├── report.html           # HTML report
    ├── test_run.log          # Framework logs
    ├── cli_logs/             # CLI output logs
    │   └── test_name_timestamp_cli.log
    └── recordings/           # Playwright recordings
        └── test_name_timestamp/
            ├── recording.webm    # Video recording
            ├── trace.zip        # Playwright trace
            ├── network.har      # Network logs
            ├── console.log      # Browser console
            ├── actions.json     # Logged actions
            ├── screenshots/     # All screenshots
            └── summary.json     # Recording summary
```

### HTML Report

The framework generates comprehensive HTML reports with:
- Test run statistics and timeline
- Pass/fail status for each test
- Screenshots and recordings links
- Error messages and logs
- Performance metrics

## 🎛️ Configuration Options

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TEST_BASE_URL` | Application URL | `http://192.168.1.188:8001/` |
| `TEST_USERNAME` | Login username | Required |
| `TEST_PASSWORD` | Login password | Required |
| `TEST_BROWSER` | Browser type | `chromium` |
| `TEST_HEADLESS` | Headless mode | `false` |
| `TEST_RESULTS_DIR` | Results directory | `test_results` |
| `TEST_RECORDINGS_DIR` | Recordings directory | `test_recordings` |
| `TEST_LOGS_DIR` | Logs directory | `test_results` |

### Command Line Options

```bash
# Enable debug logging
python -m testing_framework.cli --debug run-all

# All commands support debug mode
python -m testing_framework.cli --debug list-tests
```

## 🔍 Debugging and Troubleshooting

### Common Issues

1. **CLI Connection Fails**
   - Check that `portacode.cli` module is available
   - Verify CLI credentials and connectivity
   - Check CLI logs in `cli_logs/` directory

2. **Playwright Login Fails**
   - Verify `TEST_USERNAME` and `TEST_PASSWORD` in `.env`
   - Check login form selectors in `playwright_manager.py`
   - Review screenshots in recordings directory

3. **Tests Not Discovered**
   - Ensure test files are in `test_modules/` or `tests/` directories
   - Test files must start with `test_` or end with `_test.py`
   - Test classes must inherit from `BaseTest`

### Debug Mode

Run with debug logging to see detailed execution information:

```bash
python -m testing_framework.cli --debug run-all
```

### Manual Debugging

Access the recordings directory to inspect:
- Video recordings of test execution
- Screenshots at each step
- Network traffic logs
- Browser console output
- Detailed action logs

## 🤝 Contributing

### Adding New Test Categories

1. Add new category to `TestCategory` enum in `base_test.py`
2. Update CLI command choices in `cli.py`

### Extending Managers

- **CLI Manager**: Add new CLI interaction methods
- **Playwright Manager**: Add new browser automation helpers
- **Test Runner**: Add new execution modes or reporting formats

### Example: Custom Test Category

```python
# In base_test.py
class TestCategory(Enum):
    # ... existing categories ...
    ACCESSIBILITY = "accessibility"

# In your test
class AccessibilityTest(BaseTest):
    def __init__(self):
        super().__init__(
            name="accessibility_test",
            category=TestCategory.ACCESSIBILITY,
            description="Check accessibility compliance",
            tags=["a11y", "compliance"]
        )
```

## 📈 Advanced Usage

### Programmatic Test Execution

```python
import asyncio
from testing_framework.core.runner import TestRunner
from testing_framework.core.base_test import TestCategory

async def run_custom_suite():
    runner = TestRunner()
    
    # Run specific category
    results = await runner.run_tests_by_category(TestCategory.SMOKE)
    
    # Process results
    if results['statistics']['failed'] > 0:
        print("Some tests failed!")
    
    return results

# Run it
results = asyncio.run(run_custom_suite())
```

### Custom Reporting

```python
from testing_framework.core.runner import TestRunner

class CustomRunner(TestRunner):
    async def _generate_custom_report(self, results):
        # Your custom reporting logic
        pass
```

## 🔒 Security Considerations

- Store credentials in `.env` file, never in code
- Add `.env` to `.gitignore`
- Use environment-specific credential management
- Review recordings before sharing (may contain sensitive data)

## 📝 License

This testing framework is part of the Portacode project and follows the same license terms.