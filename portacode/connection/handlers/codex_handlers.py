"""Device-side command handlers for the Codex chat UI."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from portacode.codex_usage_limit import attach_resets_at_to_params
from portacode.connection.codex_app_server import CodexAppServer, CodexAppServerError
from portacode.connection.handlers.base import AsyncHandler

LOGGER = logging.getLogger(__name__)


class _SessionAwareSender:
    """Helper to push events to the browser sessions interested in a project."""

    def __init__(self, control_channel: Any, context: Dict[str, Any]) -> None:
        self.control_channel = control_channel
        self.context = context

    async def send(self, payload: Dict[str, Any], project_id: Optional[str] = None) -> None:
        client_session_manager = self.context.get("client_session_manager")
        if client_session_manager and client_session_manager.has_interested_clients():
            target_sessions = client_session_manager.get_target_sessions(project_id)
            if target_sessions:
                payload = dict(payload)
                payload["client_sessions"] = target_sessions
            reply_channel = client_session_manager.get_reply_channel_for_compatibility()
            if reply_channel:
                payload = dict(payload)
                payload["reply_channel"] = reply_channel

        await self.control_channel.send(payload)


class CodexChatManager:
    """Owns one CodexAppServer bridge and routes its events to Portacode client sessions."""

    def __init__(self, control_channel: Any, context: Dict[str, Any]) -> None:
        self.sender = _SessionAwareSender(control_channel, context)
        self.bridge = CodexAppServer(on_notification=self._on_notification)
        self._thread_project: Dict[str, str] = {}
        self._cwd_project: Dict[str, str] = {}
        # Survives page reloads via codex_status so the IDE can restore UI.
        self._prepare_running = False
        self._prepare_step: Optional[str] = None
        self._prepare_error: Optional[str] = None

    async def _on_notification(self, method: str, params: Dict[str, Any]) -> None:
        # Attach usage-limit reset timestamp captured by the loopback proxy.
        if method == "error" or (
            method == "turn/completed"
            and isinstance(params, dict)
            and (params.get("turn") or {}).get("error")
        ):
            params = attach_resets_at_to_params(params or {})
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("error"), dict):
                turn = dict(turn)
                turn["error"] = attach_resets_at_to_params({"error": turn["error"]})["error"]
                params = dict(params)
                params["turn"] = turn

        project_id = self._resolve_project_id(params)
        event_payload = {
            "event": "codex_event",
            "notification": {"method": method, "params": params},
        }
        if project_id:
            event_payload["project_id"] = project_id
        await self.sender.send(event_payload, project_id=project_id)

    def _resolve_project_id(self, params: Dict[str, Any]) -> Optional[str]:
        # Most notifications contain a threadId. Try that first.
        thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        if thread_id and thread_id in self._thread_project:
            return self._thread_project[thread_id]

        # Some notifications include cwd directly; fall back to the cwd map.
        cwd = params.get("cwd") or (params.get("thread") or {}).get("cwd")
        if cwd and cwd in self._cwd_project:
            return self._cwd_project[cwd]

        return None

    def record_thread(self, thread_id: str, cwd: str, project_id: str) -> None:
        self._thread_project[thread_id] = project_id
        self._cwd_project[cwd] = project_id

    async def ensure_started(self) -> None:
        await self.bridge.start()


# A single manager is stored per device agent context.
def _get_manager(handler: AsyncHandler) -> CodexChatManager:
    context = handler.context
    if "codex_manager" not in context:
        context["codex_manager"] = CodexChatManager(handler.control_channel, context)
    return context["codex_manager"]


def _project_id(message: Dict[str, Any]) -> Optional[str]:
    return message.get("project_id")


def _thread_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize thread/start|resume|read results to a thread object.

    Current app-server returns `{ "thread": { "id": ... } }`. Older fakes /
    builds may return the thread fields at the top level.
    """
    if not isinstance(result, dict):
        return {}
    thread = result.get("thread")
    if isinstance(thread, dict):
        return thread
    return result


def _thread_id_from_result(result: Dict[str, Any]) -> Optional[str]:
    thread = _thread_from_result(result)
    return thread.get("id") or result.get("id") or result.get("threadId")


def _threads_from_list_result(result: Dict[str, Any]) -> tuple[list, Optional[str]]:
    """Normalize thread/list results.

    Current app-server returns `{ "data": [...], "nextCursor": ... }`.
    """
    if not isinstance(result, dict):
        return [], None
    threads = result.get("data")
    if threads is None:
        threads = result.get("threads")
    if not isinstance(threads, list):
        threads = []
    return threads, result.get("nextCursor")


def _items_from_read_result(result: Dict[str, Any]) -> list:
    """Extract conversation items from thread/read (or legacy shapes)."""
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("items"), list):
        return result["items"]
    if isinstance(result.get("messages"), list):
        return result["messages"]

    thread = _thread_from_result(result)
    turns = thread.get("turns") or result.get("turns") or []
    items: list = []
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_items = turn.get("items")
            if isinstance(turn_items, list):
                items.extend(turn_items)
    return items


def _turn_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    turn = result.get("turn")
    if isinstance(turn, dict):
        return turn
    return result


class CodexAsyncHandler(AsyncHandler):
    """AsyncHandler that replies to `source_client_session` per WEBSOCKET_PROTOCOL.md."""

    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        try:
            response = await self.execute(message)
            if response is None:
                return
            if "request_id" in message and "request_id" not in response:
                response["request_id"] = message["request_id"]

            source_client_session = message.get("source_client_session")
            project_id = response.get("project_id") or _project_id(message)
            if project_id and "project_id" not in response:
                response["project_id"] = project_id

            if source_client_session:
                payload = dict(response)
                payload["client_sessions"] = [source_client_session]
                await self.control_channel.send(payload)
                return

            await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            LOGGER.exception("handler: Error in codex handler %s: %s", self.command_name, exc)
            await self.send_error(
                str(exc),
                reply_channel,
                _project_id(message),
                request_id=message.get("request_id"),
            )


class CodexStatusHandler(CodexAsyncHandler):
    """Report whether Codex is installed and the app-server is healthy."""

    @property
    def command_name(self) -> str:
        return "codex_status"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        installed = bool(CodexAppServer.get_binary_path())
        # Avoid blocking the asyncio loop with a sync subprocess during status.
        version = None
        ready = False
        error_message: Optional[str] = None

        if installed:
            try:
                loop = asyncio.get_running_loop()
                version = await loop.run_in_executor(None, CodexAppServer.version)
                manager = _get_manager(self)
                await manager.ensure_started()
                ready = await manager.bridge.healthy()
            except Exception as exc:
                LOGGER.debug("codex_status: app-server not healthy: %s", exc)
                error_message = str(exc)

        manager = _get_manager(self)
        if ready:
            manager._prepare_error = None
            manager._prepare_step = None

        payload = {
            "event": "codex_status",
            "installed": installed,
            "version": version,
            "ready": ready,
            "prepare_running": bool(manager._prepare_running),
            # Capability flags for the browser UI. Absent on older Portacode
            # CLIs — chat must hide model selection rather than error.
            "features": ["model_select"],
        }
        if error_message:
            payload["error_message"] = error_message
        if manager._prepare_step:
            payload["prepare_step"] = manager._prepare_step
        if manager._prepare_error and not ready:
            payload["prepare_error"] = manager._prepare_error
        project_id = _project_id(message)
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexThreadListHandler(CodexAsyncHandler):
    """List Codex threads for a given project cwd."""

    @property
    def command_name(self) -> str:
        return "codex_thread_list"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        cwd = message.get("cwd")
        if not cwd:
            raise ValueError("cwd is required")

        manager = _get_manager(self)
        await manager.ensure_started()

        # Default sourceKinds is cli+vscode only; include appServer so IDE chats appear.
        # Avoid useStateDbOnly:true — it has returned empty lists in some Codex builds.
        params: Dict[str, Any] = {
            "cwd": cwd,
            "sourceKinds": ["cli", "vscode", "appServer"],
            "limit": message.get("limit") if message.get("limit") is not None else 50,
        }
        cursor = message.get("cursor")
        if cursor is not None:
            params["cursor"] = cursor

        result = await manager.bridge.call("thread/list", params)
        threads, next_cursor = _threads_from_list_result(result)
        payload = {
            "event": "codex_thread_list",
            "threads": threads,
            "nextCursor": next_cursor,
        }
        project_id = _project_id(message)
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexThreadStartHandler(CodexAsyncHandler):
    """Start a new Codex thread in the project directory."""

    @property
    def command_name(self) -> str:
        return "codex_thread_start"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        cwd = message.get("cwd")
        project_id = _project_id(message)
        if not cwd:
            raise ValueError("cwd is required")

        manager = _get_manager(self)
        await manager.ensure_started()

        params = {"cwd": cwd}
        # Optional overrides from the UI (e.g. model picker later).
        if message.get("model"):
            params["model"] = message["model"]
        if message.get("modelProvider"):
            params["modelProvider"] = message["modelProvider"]

        result = await manager.bridge.call("thread/start", params)
        thread = _thread_from_result(result)
        thread_id = _thread_id_from_result(result)
        if not thread_id:
            raise CodexAppServerError("thread/start did not return a thread id")

        manager.record_thread(thread_id, cwd, project_id or cwd)

        payload = {
            "event": "codex_thread_started",
            "threadId": thread_id,
            "thread": thread,
        }
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexThreadResumeHandler(CodexAsyncHandler):
    """Resume a Codex thread and return its history."""

    @property
    def command_name(self) -> str:
        return "codex_thread_resume"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = message.get("threadId")
        cwd = message.get("cwd")
        project_id = _project_id(message)
        if not thread_id:
            raise ValueError("threadId is required")

        manager = _get_manager(self)
        await manager.ensure_started()

        resume_params: Dict[str, Any] = {"threadId": thread_id}
        if cwd:
            resume_params["cwd"] = cwd
        await manager.bridge.call("thread/resume", resume_params)

        read_result = await manager.bridge.call(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        items = _items_from_read_result(read_result)

        if cwd and project_id:
            manager.record_thread(thread_id, cwd, project_id)

        payload = {
            "event": "codex_thread_resumed",
            "threadId": thread_id,
            "items": items,
        }
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexTurnStartHandler(CodexAsyncHandler):
    """Send a user turn to a Codex thread."""

    @property
    def command_name(self) -> str:
        return "codex_turn_start"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = message.get("threadId")
        text = message.get("text")
        if not thread_id:
            raise ValueError("threadId is required")
        if text is None or text == "":
            raise ValueError("text is required")

        manager = _get_manager(self)
        await manager.ensure_started()

        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        # Optional model / reasoning controls (Codex Desktop-style overrides).
        if message.get("model"):
            params["model"] = message["model"]
        if message.get("effort"):
            params["effort"] = message["effort"]
        if message.get("summary"):
            params["summary"] = message["summary"]

        result = await manager.bridge.call("turn/start", params)
        project_id = _project_id(message)

        payload = {
            "event": "codex_turn_started",
            "threadId": thread_id,
            "turn": _turn_from_result(result),
        }
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexTurnInterruptHandler(CodexAsyncHandler):
    """Interrupt a running Codex turn."""

    @property
    def command_name(self) -> str:
        return "codex_turn_interrupt"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = message.get("threadId")
        turn_id = message.get("turnId")
        if not thread_id or not turn_id:
            raise ValueError("threadId and turnId are required")

        manager = _get_manager(self)
        await manager.ensure_started()
        await manager.bridge.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

        project_id = _project_id(message)
        payload = {"event": "codex_turn_interrupted", "threadId": thread_id, "turnId": turn_id}
        if project_id:
            payload["project_id"] = project_id
        return payload


class CodexPrepareHandler(CodexAsyncHandler):
    """Run `portacode prepare codex` on the device in the background.

    Streams progress as `codex_prepare_progress` events and finishes with a
    `codex_prepare_done` event so the chat UI can react (re-check status or
    show manual instructions when sudo is required).
    """

    @property
    def command_name(self) -> str:
        return "codex_prepare"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        project_id = _project_id(message)
        manager = _get_manager(self)
        if manager._prepare_running:
            payload = {
                "event": "codex_prepare_started",
                "already_running": True,
                "step": manager._prepare_step or "Setting up Codex…",
            }
            if project_id:
                payload["project_id"] = project_id
            return payload

        manager._prepare_running = True
        manager._prepare_error = None
        manager._prepare_step = "Starting Codex setup…"
        asyncio.create_task(self._run_prepare(manager, project_id))

        payload = {
            "event": "codex_prepare_started",
            "step": manager._prepare_step,
        }
        if project_id:
            payload["project_id"] = project_id
        return payload

    async def _emit_progress(
        self,
        manager: CodexChatManager,
        project_id: Optional[str],
        step: str,
    ) -> None:
        payload = {"event": "codex_prepare_progress", "step": step}
        if project_id:
            payload["project_id"] = project_id
        await manager.sender.send(payload, project_id=project_id)

    async def _run_prepare(self, manager: CodexChatManager, project_id: Optional[str]) -> None:
        from portacode.codex_prepare import CodexPreparationError, prepare_codex

        loop = asyncio.get_running_loop()

        def on_progress(step: str) -> None:
            manager._prepare_step = step
            future = asyncio.run_coroutine_threadsafe(
                self._emit_progress(manager, project_id, step),
                loop,
            )
            try:
                future.result(timeout=10)
            except Exception:  # pragma: no cover - best-effort UI updates
                LOGGER.debug("Failed to emit codex_prepare_progress", exc_info=True)

        payload: Dict[str, Any] = {
            "event": "codex_prepare_done",
            "success": False,
            "error": "Codex setup did not finish.",
        }
        try:
            await self._emit_progress(manager, project_id, manager._prepare_step or "Starting Codex setup…")
            await loop.run_in_executor(None, lambda: prepare_codex(on_progress=on_progress))
            manager._prepare_step = "Starting Codex…"
            await self._emit_progress(manager, project_id, manager._prepare_step)
            # Drop any app-server started before the sentinel was available so
            # the next chat command respawns with OPENAI_API_KEY injected.
            try:
                await manager.bridge.recycle()
            except Exception:
                LOGGER.exception("Failed to recycle Codex app-server after prepare")
            manager._prepare_error = None
            payload = {"event": "codex_prepare_done", "success": True}
        except CodexPreparationError as exc:
            LOGGER.warning("codex_prepare failed: %s", exc)
            manager._prepare_error = str(exc)
            manager._prepare_step = None
            payload = {"event": "codex_prepare_done", "success": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("codex_prepare crashed")
            manager._prepare_error = str(exc)
            manager._prepare_step = None
            payload = {"event": "codex_prepare_done", "success": False, "error": str(exc)}
        finally:
            manager._prepare_running = False

        if project_id:
            payload["project_id"] = project_id
        await manager.sender.send(payload, project_id=project_id)
