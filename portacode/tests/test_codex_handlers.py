"""Tests for the Codex chat command handlers."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from portacode.connection.handlers.codex_handlers import (
    CodexChatManager,
    CodexPrepareHandler,
    CodexStatusHandler,
    CodexThreadListHandler,
    CodexThreadStartHandler,
    CodexThreadResumeHandler,
    CodexTurnStartHandler,
    CodexTurnInterruptHandler,
)


class DummyClientSessionManager:
    def __init__(self, sessions: list[str] | None = None):
        self.sessions = sessions or ["sess-1"]

    def has_interested_clients(self) -> bool:
        return bool(self.sessions)

    def get_target_sessions(self, project_id: str | None) -> list[str]:
        return self.sessions

    def get_reply_channel_for_compatibility(self) -> str | None:
        return "rc-1"


class DummyControlChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, Any] = {
            # Match current Codex app-server response shapes.
            "thread/start": {"thread": {"id": "th-1", "name": "New chat"}},
            "thread/list": {
                "data": [{"id": "th-1", "name": "Existing chat", "preview": "hi"}],
                "nextCursor": None,
            },
            "thread/resume": {"thread": {"id": "th-1"}},
            "thread/read": {
                "thread": {
                    "id": "th-1",
                    "turns": [{"id": "turn-1", "items": [{"id": "msg-1", "role": "user", "text": "hello"}]}],
                }
            },
            "turn/start": {"turn": {"id": "turn-1", "threadId": "th-1"}},
            "turn/interrupt": {},
        }

    async def start(self) -> None:
        pass

    async def healthy(self) -> bool:
        return True

    async def call(self, method: str, params: dict) -> Any:
        self.calls.append((method, params))
        return self.responses.get(method, {})


def _context(**extra) -> dict:
    return {
        "client_session_manager": DummyClientSessionManager(),
        **extra,
    }


@pytest.mark.asyncio
async def test_codex_status_ready():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexStatusHandler(channel, context)
    await handler.handle({"cmd": "codex_status", "project_id": "p-1"}, reply_channel="rc-1")

    assert len(channel.sent) == 1
    payload = channel.sent[0]
    assert payload["event"] == "codex_status"
    assert payload["ready"] is True
    assert payload["prepare_running"] is False
    assert payload["project_id"] == "p-1"
    assert payload["client_sessions"] == ["sess-1"]
    assert "model_select" in payload.get("features", [])
    assert "attach_files" in payload.get("features", [])
    assert payload.get("attach_dir")


@pytest.mark.asyncio
async def test_codex_thread_list():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexThreadListHandler(channel, context)
    await handler.handle({"cmd": "codex_thread_list", "project_id": "p-1", "cwd": "/tmp/proj"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_thread_list"
    assert payload["threads"][0]["id"] == "th-1"
    assert payload["project_id"] == "p-1"
    list_call = manager.bridge.calls[-1]
    assert list_call[0] == "thread/list"
    assert list_call[1]["cwd"] == "/tmp/proj"
    assert "appServer" in list_call[1]["sourceKinds"]
    assert "useStateDbOnly" not in list_call[1]


@pytest.mark.asyncio
async def test_codex_thread_start_records_mapping():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexThreadStartHandler(channel, context)
    await handler.handle({"cmd": "codex_thread_start", "project_id": "p-1", "cwd": "/tmp/proj"}, reply_channel="rc-1")

    assert manager._thread_project["th-1"] == "p-1"
    assert manager._cwd_project["/tmp/proj"] == "p-1"
    payload = channel.sent[0]
    assert payload["event"] == "codex_thread_started"
    assert payload["threadId"] == "th-1"


@pytest.mark.asyncio
async def test_codex_thread_resume():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexThreadResumeHandler(channel, context)
    await handler.handle({"cmd": "codex_thread_resume", "project_id": "p-1", "threadId": "th-1", "cwd": "/tmp/proj"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_thread_resumed"
    assert payload["items"][0]["text"] == "hello"
    assert manager._thread_project["th-1"] == "p-1"
    read_call = next(c for c in manager.bridge.calls if c[0] == "thread/read")
    assert read_call[1]["includeTurns"] is True


@pytest.mark.asyncio
async def test_codex_turn_start():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexTurnStartHandler(channel, context)
    await handler.handle({"cmd": "codex_turn_start", "project_id": "p-1", "threadId": "th-1", "text": "hi"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_turn_started"
    assert payload["threadId"] == "th-1"
    assert manager.bridge.calls[-1] == ("turn/start", {"threadId": "th-1", "input": [{"type": "text", "text": "hi"}]})


@pytest.mark.asyncio
async def test_codex_turn_start_with_image_attachment():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexTurnStartHandler(channel, context)
    await handler.handle(
        {
            "cmd": "codex_turn_start",
            "project_id": "p-1",
            "threadId": "th-1",
            "text": "look",
            "attachments": [
                {
                    "path": "/home/u/.codex/tmp/portacode-attach/a/photo.png",
                    "name": "photo.png",
                    "mime_type": "image/png",
                    "kind": "image",
                }
            ],
        },
        reply_channel="rc-1",
    )

    assert manager.bridge.calls[-1] == (
        "turn/start",
        {
            "threadId": "th-1",
            "input": [
                {"type": "text", "text": "look"},
                {"type": "localImage", "path": "/home/u/.codex/tmp/portacode-attach/a/photo.png"},
            ],
        },
    )


@pytest.mark.asyncio
async def test_codex_turn_start_with_non_image_attachment_mentions_path():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexTurnStartHandler(channel, context)
    await handler.handle(
        {
            "cmd": "codex_turn_start",
            "project_id": "p-1",
            "threadId": "th-1",
            "text": "",
            "attachments": [
                {
                    "path": "/tmp/notes.pdf",
                    "name": "notes.pdf",
                    "mime_type": "application/pdf",
                }
            ],
        },
        reply_channel="rc-1",
    )

    turn_input = manager.bridge.calls[-1][1]["input"]
    assert turn_input[0]["type"] == "text"
    assert "/tmp/notes.pdf" in turn_input[0]["text"]
    assert all(item.get("type") != "localImage" for item in turn_input)


@pytest.mark.asyncio
async def test_codex_turn_start_forwards_model():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexTurnStartHandler(channel, context)
    await handler.handle(
        {
            "cmd": "codex_turn_start",
            "project_id": "p-1",
            "threadId": "th-1",
            "text": "hi",
            "model": "gpt-5.6-sol",
        },
        reply_channel="rc-1",
    )

    assert manager.bridge.calls[-1] == (
        "turn/start",
        {
            "threadId": "th-1",
            "input": [{"type": "text", "text": "hi"}],
            "model": "gpt-5.6-sol",
        },
    )


@pytest.mark.asyncio
async def test_codex_thread_start_forwards_model():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexThreadStartHandler(channel, context)
    await handler.handle(
        {
            "cmd": "codex_thread_start",
            "project_id": "p-1",
            "cwd": "/tmp/proj",
            "model": "gpt-5.6-terra",
        },
        reply_channel="rc-1",
    )

    start_call = next(c for c in manager.bridge.calls if c[0] == "thread/start")
    assert start_call[1]["model"] == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_codex_turn_interrupt():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    handler = CodexTurnInterruptHandler(channel, context)
    await handler.handle({"cmd": "codex_turn_interrupt", "project_id": "p-1", "threadId": "th-1", "turnId": "turn-1"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_turn_interrupted"
    assert manager.bridge.calls[-1] == ("turn/interrupt", {"threadId": "th-1", "turnId": "turn-1"})


@pytest.mark.asyncio
async def test_notification_forwarding_targets_project():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.record_thread("th-1", "/tmp/proj", "p-1")
    await manager._on_notification("item/agentMessage/delta", {"threadId": "th-1", "delta": "hello"})

    assert len(channel.sent) == 1
    payload = channel.sent[0]
    assert payload["event"] == "codex_event"
    assert payload["project_id"] == "p-1"
    assert payload["notification"]["method"] == "item/agentMessage/delta"
    assert payload["client_sessions"] == ["sess-1"]


@pytest.mark.asyncio
async def test_error_notification_attaches_resets_at(monkeypatch):
    from portacode import codex_usage_limit

    monkeypatch.setattr(codex_usage_limit, "_last_resets_at", 1772180859)
    monkeypatch.setattr(codex_usage_limit, "_last_noted_at", __import__("time").time())

    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.record_thread("th-1", "/tmp/proj", "p-1")
    await manager._on_notification(
        "error",
        {
            "threadId": "th-1",
            "error": {
                "message": "usage limit",
                "codexErrorInfo": "usageLimitExceeded",
            },
        },
    )

    params = channel.sent[0]["notification"]["params"]
    assert params["error"]["resetsAt"] == 1772180859
    assert params["error"]["resets_at"] == 1772180859


@pytest.mark.asyncio
async def test_codex_prepare_already_running_reports_step():
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    manager._prepare_running = True
    manager._prepare_step = "Installing Codex CLI…"
    context["codex_manager"] = manager

    handler = CodexPrepareHandler(channel, context)
    await handler.handle({"cmd": "codex_prepare", "project_id": "p-1"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_prepare_started"
    assert payload["already_running"] is True
    assert payload["step"] == "Installing Codex CLI…"


@pytest.mark.asyncio
async def test_codex_status_includes_prepare_state(monkeypatch):
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    manager._prepare_running = True
    manager._prepare_step = "Installing Node.js if needed…"
    context["codex_manager"] = manager

    monkeypatch.setattr(
        "portacode.connection.handlers.codex_handlers.CodexAppServer.get_binary_path",
        staticmethod(lambda: None),
    )

    handler = CodexStatusHandler(channel, context)
    await handler.handle({"cmd": "codex_status", "project_id": "p-1"}, reply_channel="rc-1")

    payload = channel.sent[0]
    assert payload["event"] == "codex_status"
    assert payload["ready"] is False
    assert payload["prepare_running"] is True
    assert payload["prepare_step"] == "Installing Node.js if needed…"


@pytest.mark.asyncio
async def test_codex_prepare_emits_progress_and_done(monkeypatch):
    channel = DummyControlChannel()
    context = _context()
    manager = CodexChatManager(channel, context)
    manager.bridge = FakeBridge()
    context["codex_manager"] = manager

    steps: list[str] = []

    def fake_prepare(*, on_progress=None):
        if on_progress:
            on_progress("Installing Codex CLI…")
            steps.append("Installing Codex CLI…")
        return "/tmp/config.toml"

    async def fake_recycle():
        return None

    monkeypatch.setattr("portacode.codex_prepare.prepare_codex", fake_prepare)
    manager.bridge.recycle = fake_recycle  # type: ignore[method-assign]

    handler = CodexPrepareHandler(channel, context)
    result = await handler.execute({"cmd": "codex_prepare", "project_id": "p-1"})
    assert result["event"] == "codex_prepare_started"

    # Allow the background task to finish.
    for _ in range(50):
        if any(msg.get("event") == "codex_prepare_done" for msg in channel.sent):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("codex_prepare_done was not emitted")

    events = [msg.get("event") for msg in channel.sent]
    assert "codex_prepare_progress" in events
    done = next(msg for msg in channel.sent if msg.get("event") == "codex_prepare_done")
    assert done["success"] is True
    assert manager._prepare_running is False
    assert steps == ["Installing Codex CLI…"]
