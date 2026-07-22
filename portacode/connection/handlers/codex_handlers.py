"""Device-side command handlers for the Codex chat UI."""

from __future__ import annotations

import asyncio
import copy
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

from portacode.codex_usage_limit import attach_resets_at_to_params
from portacode.connection.codex_app_server import CodexAppServer, CodexAppServerError
from portacode.connection.handlers.base import AsyncHandler

LOGGER = logging.getLogger(__name__)
MAX_UI_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_LIVE_TEXT_BYTES = 512 * 1024
MAX_LIVE_ITEMS_PER_THREAD = 64
MAX_LIVE_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_TRACKED_LIVE_THREADS = 64

_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}


def _is_image_attachment(path: str, mime_type: Optional[str] = None, kind: Optional[str] = None) -> bool:
    if kind in {"image", "localImage"}:
        return True
    if mime_type and str(mime_type).lower().startswith("image/"):
        return True
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return True
    guessed, _ = mimetypes.guess_type(path)
    return bool(guessed and guessed.startswith("image/"))


def _build_turn_input(
    text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build Codex turn/start input items from text + staged device paths."""
    attachments = attachments or []
    text = (text or "").strip()
    input_items: List[Dict[str, Any]] = []
    path_mentions: List[str] = []

    for raw in attachments:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip()
        if not path:
            continue
        name = str(raw.get("name") or Path(path).name)
        mime = raw.get("mime_type") or raw.get("mimeType")
        kind = raw.get("kind")
        if _is_image_attachment(path, mime_type=mime, kind=kind):
            input_items.append({"type": "localImage", "path": path})
        else:
            path_mentions.append(f"{name}: {path}")

    body = text
    if path_mentions:
        mention_block = "Attached files:\n" + "\n".join(path_mentions)
        body = f"{body}\n\n{mention_block}".strip() if body else mention_block

    if body:
        input_items.insert(0, {"type": "text", "text": body})
    elif not input_items:
        raise ValueError("text or attachments are required")
    elif not any(item.get("type") == "text" for item in input_items):
        # Images-only turn still needs a short text cue for some Codex builds.
        input_items.insert(0, {"type": "text", "text": "Please review the attached image(s)."})

    return input_items


class _SessionAwareSender:
    """Helper to push events to the browser sessions interested in a project."""

    def __init__(self, control_channel: Any, context: Dict[str, Any]) -> None:
        self.control_channel = control_channel
        self.context = context

    async def send(
        self,
        payload: Dict[str, Any],
        project_id: Optional[str] = None,
        explicit_sessions: Optional[List[str]] = None,
    ) -> None:
        target_sessions = set(explicit_sessions or [])
        client_session_manager = self.context.get("client_session_manager")
        if client_session_manager and client_session_manager.has_interested_clients():
            target_sessions.update(client_session_manager.get_target_sessions(project_id))
            reply_channel = client_session_manager.get_reply_channel_for_compatibility()
            if reply_channel:
                payload = dict(payload)
                payload["reply_channel"] = reply_channel
        if target_sessions:
            payload = dict(payload)
            payload["client_sessions"] = sorted(target_sessions)

        await self.control_channel.send(payload)


class CodexChatManager:
    """Owns one CodexAppServer bridge and routes its events to Portacode client sessions."""

    def __init__(self, control_channel: Any, context: Dict[str, Any]) -> None:
        self.sender = _SessionAwareSender(control_channel, context)
        self.bridge = CodexAppServer(
            on_notification=self._on_notification,
            on_unexpected_exit=self._on_bridge_exit,
        )
        self._thread_project: Dict[str, str] = {}
        self._cwd_project: Dict[str, str] = {}
        self._thread_active_turn: Dict[str, str] = {}
        self._thread_sessions: Dict[str, set[str]] = {}
        # Materialized in-progress items live with the device agent, not a
        # browser websocket. New/reconnected tabs receive these on resume.
        self._thread_live_items: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Survives page reloads via codex_status so the IDE can restore UI.
        self._prepare_running = False
        self._prepare_step: Optional[str] = None
        self._prepare_error: Optional[str] = None

    async def _on_bridge_exit(self, returncode: Optional[int]) -> None:
        """Turn an app-server crash into terminal events for waiting browsers."""
        active = list(self._thread_active_turn.items())
        self._thread_active_turn.clear()
        for thread_id, turn_id in active:
            project_id = self._thread_project.get(thread_id)
            if not project_id:
                continue
            suffix = f" (exit code {returncode})" if returncode is not None else ""
            params = {
                "threadId": thread_id,
                "turnId": turn_id,
                "error": {
                    "message": f"The local Codex app-server stopped unexpectedly{suffix}.",
                    "additionalDetails": "Portacode is restarting it. Retry the message after it reconnects.",
                },
            }
            await self.sender.send(
                {
                    "event": "codex_event",
                    "notification": {"method": "error", "params": params},
                    "project_id": project_id,
                },
                project_id=project_id,
                explicit_sessions=list(self._thread_sessions.get(thread_id, set())),
            )

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

        self._remember_thread_from_params(params)
        thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = params.get("turnId") or turn.get("id")
        if thread_id and method == "turn/started" and turn_id:
            self._thread_active_turn[str(thread_id)] = str(turn_id)
            self._thread_live_items[str(thread_id)] = {}
            while len(self._thread_live_items) > MAX_TRACKED_LIVE_THREADS:
                oldest = next(iter(self._thread_live_items))
                if oldest == str(thread_id) and len(self._thread_live_items) > 1:
                    oldest = next(key for key in self._thread_live_items if key != str(thread_id))
                self._thread_live_items.pop(oldest, None)
        elif thread_id and method == "turn/completed":
            self._thread_active_turn.pop(str(thread_id), None)
        if thread_id:
            params = self._materialize_and_bound_notification(str(thread_id), method, params)
        project_id = self._resolve_project_id(params)
        if not project_id:
            # Unresolved project_id would fan out to every open project tab.
            LOGGER.debug(
                "Dropping Codex notification %s — no project mapping for thread/cwd",
                method,
            )
            return
        event_payload = {
            "event": "codex_event",
            "notification": {"method": method, "params": params},
            "project_id": project_id,
        }
        await self.sender.send(
            event_payload,
            project_id=project_id,
            explicit_sessions=list(self._thread_sessions.get(str(thread_id), set())) if thread_id else None,
        )

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= limit:
            return value
        marker = "\n[Portacode truncated oversized live output.]\n"
        budget = max(limit - len(marker.encode("utf-8")), 0)
        return raw[:budget].decode("utf-8", errors="ignore") + marker

    def _materialize_and_bound_notification(
        self, thread_id: str, method: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Bound UI command output and retain a reconnectable live snapshot."""
        params = copy.deepcopy(params)
        raw = params.get("item") if isinstance(params.get("item"), dict) else params
        item_id = raw.get("id") or params.get("itemId") or params.get("id")
        if not item_id or not method.startswith("item/"):
            return params

        item_id = str(item_id)
        items = self._thread_live_items.setdefault(thread_id, {})
        item = items.setdefault(item_id, {"id": item_id})
        incoming_type = raw.get("type")
        if incoming_type:
            item["type"] = incoming_type
        elif "commandExecution" in method:
            item["type"] = "commandExecution"
            item.setdefault("role", "tool")
        elif "reasoning" in method:
            item["type"] = "reasoning"
            item.setdefault("role", "reasoning")
        elif "agentMessage" in method or "message/delta" in method:
            item["type"] = "agentMessage"
            item.setdefault("role", "assistant")
        for key in ("role", "command", "status"):
            if raw.get(key) is not None:
                item[key] = copy.deepcopy(raw[key])

        is_command = "commandExecution" in method or item.get("type") == "commandExecution"
        text_key = None
        incoming = ""
        if method == "item/commandExecution/outputDelta":
            text_key, incoming = "output", raw.get("delta") or raw.get("output") or ""
        elif method in {"item/agentMessage/delta", "item/message/delta"}:
            text_key, incoming = "text", raw.get("delta") or raw.get("text") or ""
        elif method in {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
            text_key, incoming = "reasoning", raw.get("delta") or raw.get("text") or ""
        elif method in {"item/started", "item/completed"}:
            if is_command:
                text_key = "output"
                incoming = raw.get("output") or (raw.get("commandExecution") or {}).get("output") or raw.get("aggregatedOutput") or ""
            else:
                incoming = raw.get("text") or ""
                text_key = "text" if incoming else None

        if text_key and isinstance(incoming, str):
            limit = MAX_UI_COMMAND_OUTPUT_BYTES if text_key == "output" else MAX_LIVE_TEXT_BYTES
            if method in {"item/started", "item/completed"}:
                combined = incoming
            else:
                combined = str(item.get(text_key) or "") + incoming
            bounded = self._bounded_text(combined, limit)
            item[text_key] = bounded
            # Forward only the portion that fits for deltas; completed items
            # carry a bounded aggregate so the browser cannot restore the blob.
            if method.endswith("/outputDelta") or method.endswith("/delta") or method.endswith("textDelta") or method.endswith("summaryTextDelta"):
                previous = combined[:-len(incoming)] if incoming else combined
                allowed = bounded[len(previous):] if bounded.startswith(previous) else ""
                if "delta" in raw:
                    raw["delta"] = allowed
                elif text_key in raw:
                    raw[text_key] = allowed
            elif text_key == "output":
                raw["output"] = bounded
                raw.pop("aggregatedOutput", None)
                if isinstance(raw.get("commandExecution"), dict):
                    raw["commandExecution"]["output"] = bounded
        while len(items) > MAX_LIVE_ITEMS_PER_THREAD:
            oldest = next(iter(items))
            if oldest == item_id and len(items) > 1:
                oldest = next(key for key in items if key != item_id)
            items.pop(oldest, None)
        while sum(len(str(value).encode("utf-8")) for value in items.values()) > MAX_LIVE_SNAPSHOT_BYTES and len(items) > 1:
            oldest = next(iter(items))
            if oldest == item_id:
                oldest = next(key for key in items if key != item_id)
            items.pop(oldest, None)
        return params

    def live_items(self, thread_id: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(list(self._thread_live_items.get(str(thread_id), {}).values()))

    def _remember_thread_from_params(self, params: Dict[str, Any]) -> None:
        """Opportunistically map threadId → project when cwd is already known."""
        if not isinstance(params, dict):
            return
        thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = params.get("threadId") or thread.get("id")
        cwd = params.get("cwd") or thread.get("cwd")
        if thread_id and cwd and cwd in self._cwd_project:
            self._thread_project[str(thread_id)] = self._cwd_project[cwd]

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

    def record_thread(self, thread_id: str, cwd: Optional[str], project_id: str) -> None:
        if thread_id and project_id:
            self._thread_project[str(thread_id)] = project_id
        if cwd and project_id:
            self._cwd_project[cwd] = project_id

    def subscribe_thread(self, thread_id: str, client_session: Optional[str]) -> None:
        """Route future thread events to a tab even before session discovery catches up."""
        if not thread_id or not client_session:
            return
        sessions = self._thread_sessions.setdefault(str(thread_id), set())
        sessions.add(str(client_session))
        # Avoid an unbounded collection of stale channel names after many reconnects.
        while len(sessions) > 32:
            sessions.pop()

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


def _turns_from_page(page: Any) -> list:
    """Normalize thread/turns/list or initialTurnsPage payloads to a turn list."""
    if isinstance(page, list):
        return page
    if not isinstance(page, dict):
        return []
    for key in ("data", "turns", "items"):
        value = page.get(key)
        if isinstance(value, list):
            return value
    return []


def _items_from_turns(turns: list) -> list:
    items: list = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_items = turn.get("items")
        if isinstance(turn_items, list):
            items.extend(turn_items)
    return items


def _active_turn_from_turns(turns: list) -> Optional[Dict[str, Any]]:
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        status = str(turn.get("status") or "").lower()
        if status in {"inprogress", "in_progress", "active", "running"}:
            return turn
    return None


def _active_turn_from_read_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the in-progress turn from thread/read, if any."""
    if not isinstance(result, dict):
        return None
    thread = _thread_from_result(result)
    turns = thread.get("turns") or result.get("turns") or []
    if not isinstance(turns, list):
        return None
    return _active_turn_from_turns(turns)


async def _resume_thread_history(
    manager: "CodexChatManager",
    *,
    thread_id: str,
    cwd: Optional[str],
    already_active: bool = False,
) -> tuple[Dict[str, Any], list, Optional[Dict[str, Any]]]:
    """Resume a thread without hydrating the full rollout in one giant payload.

    Attachment-heavy threads (zip/html/images) hang Codex when resume/read
    returns every turn inline. Prefer excludeTurns + paginated turns/list.
    """
    resume_params: Dict[str, Any] = {"threadId": thread_id, "excludeTurns": True}
    if cwd:
        resume_params["cwd"] = cwd

    resume_result: Dict[str, Any] = {"thread": {"id": thread_id}}
    try:
        if not already_active:
            resume_result = await manager.bridge.call(
                "thread/resume", resume_params, timeout=120.0
            )
    except CodexAppServerError as exc:
        # Older Codex builds may not know excludeTurns — fall back carefully.
        message = str(exc).lower()
        if "excludeturns" in message or "unknown" in message or "invalid" in message:
            legacy = {"threadId": thread_id}
            if cwd:
                legacy["cwd"] = cwd
            resume_result = await manager.bridge.call(
                "thread/resume", legacy, timeout=180.0
            )
            thread = _thread_from_result(resume_result)
            turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
            if turns:
                return resume_result, _items_from_turns(turns), _active_turn_from_turns(turns)
        else:
            raise

    # Newer servers may embed a first page on resume.
    page = resume_result.get("initialTurnsPage") if isinstance(resume_result, dict) else None
    turns = _turns_from_page(page)
    if turns:
        return resume_result, _items_from_turns(turns), _active_turn_from_turns(turns)

    try:
        turns_page = await manager.bridge.call(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 80,
                "sortDirection": "asc",
            },
            timeout=120.0,
        )
        turns = _turns_from_page(turns_page)
        return resume_result, _items_from_turns(turns), _active_turn_from_turns(turns)
    except (CodexAppServerError, asyncio.TimeoutError):
        LOGGER.debug("thread/turns/list unavailable; trying lightweight thread/read", exc_info=True)

    # Avoid includeTurns:true — attachment-heavy rollouts hang Codex app-server.
    try:
        read_result = await manager.bridge.call(
            "thread/read",
            {"threadId": thread_id, "excludeTurns": True},
            timeout=30.0,
        )
        page = read_result.get("initialTurnsPage") if isinstance(read_result, dict) else None
        turns = _turns_from_page(page)
        if turns:
            return resume_result, _items_from_turns(turns), _active_turn_from_turns(turns)
        thread = _thread_from_result(read_result)
        inline = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        if inline:
            return resume_result, _items_from_turns(inline), _active_turn_from_turns(inline)
    except (CodexAppServerError, asyncio.TimeoutError):
        LOGGER.warning(
            "Could not load history for thread %s after resume; returning empty items",
            thread_id,
            exc_info=True,
        )
    return resume_result, [], None


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

        attach_dir = str(Path.home() / ".codex" / "tmp" / "portacode-attach")
        try:
            Path(attach_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            LOGGER.debug("Could not ensure Codex attach dir %s", attach_dir, exc_info=True)

        payload = {
            "event": "codex_status",
            "installed": installed,
            "version": version,
            "ready": ready,
            "prepare_running": bool(manager._prepare_running),
            # Capability flags for the browser UI. Absent on older Portacode
            # CLIs — chat must hide new controls rather than error.
            "features": ["model_select", "attach_files"],
            "attach_dir": attach_dir,
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
        manager.subscribe_thread(thread_id, message.get("source_client_session"))

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

        # thread/resume can emit notifications before its RPC response. Route
        # those notifications to the requesting project from the outset.
        if project_id:
            manager.record_thread(thread_id, cwd, project_id)
        manager.subscribe_thread(thread_id, message.get("source_client_session"))

        resume_result, items, active_turn = await _resume_thread_history(
            manager,
            thread_id=thread_id,
            cwd=cwd,
            already_active=str(thread_id) in manager._thread_active_turn,
        )
        thread = _thread_from_result(resume_result)
        if not active_turn:
            known_turn_id = manager._thread_active_turn.get(str(thread_id))
            if known_turn_id:
                active_turn = {"id": known_turn_id, "status": "inProgress"}

        payload = {
            "event": "codex_thread_resumed",
            "threadId": thread_id,
            "thread": thread,
            "items": items,
            "liveItems": manager.live_items(thread_id),
        }
        if active_turn:
            payload["activeTurn"] = active_turn
            active_turn_id = active_turn.get("id") or active_turn.get("turnId")
            if active_turn_id:
                manager._thread_active_turn[str(thread_id)] = str(active_turn_id)
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
        attachments = message.get("attachments")
        if not thread_id:
            raise ValueError("threadId is required")
        if not isinstance(attachments, list):
            attachments = []
        if (text is None or text == "") and not attachments:
            raise ValueError("text or attachments are required")

        manager = _get_manager(self)
        await manager.ensure_started()

        project_id = _project_id(message)
        cwd = message.get("cwd")
        # Codex may emit turn/started, retry, or terminal error notifications
        # before the turn/start RPC response. Establish routing before the call
        # so the browser cannot miss the event that clears its streaming state.
        if project_id:
            manager.record_thread(thread_id, cwd, project_id)
        manager.subscribe_thread(thread_id, message.get("source_client_session"))

        params = {
            "threadId": thread_id,
            "input": _build_turn_input(text or "", attachments),
        }
        # Optional model / reasoning controls (Codex Desktop-style overrides).
        if message.get("model"):
            params["model"] = message["model"]
        if message.get("effort"):
            params["effort"] = message["effort"]
        if message.get("summary"):
            params["summary"] = message["summary"]

        result = await manager.bridge.call("turn/start", params)
        turn = _turn_from_result(result)
        turn_id = turn.get("id") or turn.get("turnId")
        if turn_id:
            manager._thread_active_turn[str(thread_id)] = str(turn_id)

        payload = {
            "event": "codex_turn_started",
            "threadId": thread_id,
            "turn": turn,
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
        if not thread_id:
            raise ValueError("threadId is required")

        manager = _get_manager(self)
        turn_id = turn_id or manager._thread_active_turn.get(str(thread_id))
        if not turn_id:
            raise ValueError("No active turn was found for this thread")
        await manager.ensure_started()
        await manager.bridge.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        manager._thread_active_turn.pop(str(thread_id), None)

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
