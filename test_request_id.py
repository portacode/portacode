#!/usr/bin/env python3
"""
Test request_id automatic passthrough in handlers
"""
import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portacode.connection.handlers.terminal_handlers import TerminalStartHandler
from portacode.connection.handlers.file_handlers import ContentRequestHandler


class MockChannel:
    """Mock channel for testing"""
    def __init__(self):
        self.sent_messages = []

    async def send(self, data):
        self.sent_messages.append(data)
        print(f"📤 Sent: {data}")


class MockSessionManager:
    """Mock session manager"""
    async def create_session(self, shell=None, cwd=None, project_id=None):
        return {
            "terminal_id": "test_terminal_123",
            "channel": "test_channel_456",
            "pid": 12345,
            "shell": shell or "bash",
            "cwd": cwd,
            "project_id": project_id
        }


async def test_terminal_start_with_request_id():
    """Test that terminal_start now automatically includes request_id"""
    print("\n" + "=" * 70)
    print("TEST 1: terminal_start with request_id")
    print("=" * 70)

    mock_channel = MockChannel()
    context = {
        "session_manager": MockSessionManager(),
        "client_session_manager": None
    }

    handler = TerminalStartHandler(mock_channel, context)

    # Message with request_id
    message = {
        "cmd": "terminal_start",
        "shell": "bash",
        "request_id": "req_test_001"
    }

    print(f"📥 Sending message: {message}")

    await handler.handle(message)

    # Check response
    assert len(mock_channel.sent_messages) > 0, "No response sent"
    response = mock_channel.sent_messages[0]

    print(f"📬 Response: {response}")

    # Verify request_id was automatically added
    assert "request_id" in response, "❌ request_id not found in response"
    assert response["request_id"] == "req_test_001", f"❌ request_id mismatch: {response['request_id']}"
    assert response["event"] == "terminal_started", f"❌ Wrong event: {response['event']}"

    print("✅ TEST PASSED: request_id automatically added to terminal_started response")


async def test_terminal_start_without_request_id():
    """Test backward compatibility - terminal_start without request_id"""
    print("\n" + "=" * 70)
    print("TEST 2: terminal_start without request_id (backward compatibility)")
    print("=" * 70)

    mock_channel = MockChannel()
    context = {
        "session_manager": MockSessionManager(),
        "client_session_manager": None
    }

    handler = TerminalStartHandler(mock_channel, context)

    # Message without request_id
    message = {
        "cmd": "terminal_start",
        "shell": "bash"
    }

    print(f"📥 Sending message: {message}")

    await handler.handle(message)

    # Check response
    assert len(mock_channel.sent_messages) > 0, "No response sent"
    response = mock_channel.sent_messages[0]

    print(f"📬 Response: {response}")

    # Verify request_id is NOT in response
    assert "request_id" not in response, f"❌ request_id should not be in response: {response}"
    assert response["event"] == "terminal_started", f"❌ Wrong event: {response['event']}"

    print("✅ TEST PASSED: No request_id in response when not in request")


async def main():
    """Run all tests"""
    print("\n🧪 Testing Centralized request_id Handling")
    print("=" * 70)

    try:
        await test_terminal_start_with_request_id()
        await test_terminal_start_without_request_id()

        print("\n" + "=" * 70)
        print("🎉 ALL TESTS PASSED")
        print("=" * 70)
        print("\nSummary:")
        print("✅ request_id automatically passed through when present")
        print("✅ Backward compatible - works without request_id")
        print("✅ terminal_start now supports request_id (previously didn't)")

        return 0

    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
