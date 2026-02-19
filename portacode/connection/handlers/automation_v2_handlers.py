"""Device-side Automation v2 handlers.

This module adds additive commands for long-running automation execution:
- automation_v2_start
- automation_v2_state
- automation_v2_cancel

The runtime keeps in-memory state and mirrors it to a JSON file so reconnecting
workers can query and resume orchestration without restarting from step 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .base import AsyncHandler

logger = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_SECONDS = 7200.0
MAX_STDIO_CHARS = 8000
OUTPUT_FLUSH_INTERVAL_S = 1.0
STATE_FILE_PATH = Path("/tmp/portacode_automation_v2_state.json")


def _trim_text(value: Any, max_chars: int = MAX_STDIO_CHARS) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    suffix = f"\n...[truncated to {max_chars} chars]"
    return text[: max_chars - len(suffix)] + suffix


def _extract_step_command(step: Any) -> Optional[str]:
    if not isinstance(step, dict):
        return None
    normalized = {str(k).lower(): v for k, v in step.items()}
    command = normalized.get("command") or normalized.get("cmd") or normalized.get("run")
    if command is None:
        return None
    command_text = str(command).strip()
    return command_text or None


def _extract_step_timeout(step: Any, fallback: float) -> float:
    if not isinstance(step, dict):
        return fallback
    value = step.get("timeout")
    if value is None:
        return fallback
    try:
        timeout_value = float(value)
    except (TypeError, ValueError):
        return fallback
    if timeout_value <= 0:
        return fallback
    return timeout_value


class _AutomationRuntimeV2:
    """Single-task automation runtime with persisted state."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Any] = {
            "active_task_id": None,
            "tasks": {},
            "updated_at": None,
        }
        self._runner_task: Optional[asyncio.Task] = None
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._current_process_task_id: Optional[str] = None
        self._change_condition = asyncio.Condition()
        self._event_sender: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._load_state()

    def set_event_sender(self, sender: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> None:
        self._event_sender = sender

    def _load_state(self) -> None:
        try:
            if not self._state_path.exists():
                return
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                tasks = payload.get("tasks")
                if not isinstance(tasks, dict):
                    tasks = {}
                self._state = {
                    "active_task_id": payload.get("active_task_id"),
                    "tasks": tasks,
                    "updated_at": payload.get("updated_at"),
                }
        except Exception:
            logger.exception("automation_v2: failed to load persisted state")

    def _persist_state(self) -> None:
        self._state["updated_at"] = time.time()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        payload = json.dumps(self._state, ensure_ascii=True, separators=(",", ":"))
        with open(tmp_path, "w", encoding="utf-8") as tmpf:
            tmpf.write(payload)
            tmpf.flush()
            os.fsync(tmpf.fileno())
        os.replace(tmp_path, self._state_path)

    async def _notify_change(self, state: Optional[Dict[str, Any]] = None) -> None:
        async with self._change_condition:
            self._change_condition.notify_all()

    async def start(
        self,
        task_id: str,
        instructions: Any,
        default_timeout_seconds: float,
    ) -> Dict[str, Any]:
        if not isinstance(instructions, list):
            raise ValueError("instructions must be a list")

        task_key = str(task_id).strip()
        if not task_key:
            raise ValueError("task_id is required")

        async with self._lock:
            active_task_id = self._state.get("active_task_id")
            if active_task_id and active_task_id != task_key:
                active_state = self._state.get("tasks", {}).get(active_task_id, {})
                active_status = active_state.get("status")
                if active_status in {"running", "pending"}:
                    raise RuntimeError(
                        f"Another automation task is active on device: {active_task_id}"
                    )

            tasks = self._state.setdefault("tasks", {})
            existing = tasks.get(task_key)
            if isinstance(existing, dict) and existing.get("status") in {"running", "pending", "success", "failed", "cancelled"}:
                self._state["active_task_id"] = task_key if existing.get("status") in {"running", "pending"} else None
                existing["state_seq"] = int(existing.get("state_seq") or 0) + 1
                self._persist_state()
                await self._notify_change(dict(existing))
                if existing.get("status") in {"running", "pending"} and (not self._runner_task or self._runner_task.done()):
                    self._runner_task = asyncio.create_task(self._run_task(task_key))
                return existing

            task_state: Dict[str, Any] = {
                "task_id": task_key,
                "status": "pending",
                "instructions": instructions,
                "default_timeout_seconds": float(default_timeout_seconds),
                "current_step_index": 0,
                "current_step_status": "pending",
                "steps": [],
                "created_at": time.time(),
                "started_at": None,
                "completed_at": None,
                "last_error": None,
                "cancel_requested": False,
                "state_seq": 1,
            }
            tasks[task_key] = task_state
            self._state["active_task_id"] = task_key
            self._persist_state()
            await self._notify_change(dict(task_state))

            if not self._runner_task or self._runner_task.done():
                self._runner_task = asyncio.create_task(self._run_task(task_key))
            return task_state

    async def get_state(self, task_id: str) -> Dict[str, Any]:
        task_key = str(task_id).strip()
        async with self._lock:
            state = self._state.get("tasks", {}).get(task_key)
            if isinstance(state, dict):
                return dict(state)
            return {
                "task_id": task_key,
                "status": "unknown",
                "current_step_index": 0,
                "current_step_status": "pending",
                "steps": [],
                "last_error": "task not found",
            }

    async def cancel(self, task_id: str) -> Dict[str, Any]:
        task_key = str(task_id).strip()
        async with self._lock:
            state = self._state.get("tasks", {}).get(task_key)
            if not isinstance(state, dict):
                return {
                    "task_id": task_key,
                    "status": "unknown",
                    "message": "task not found",
                }
            state["cancel_requested"] = True
            if state.get("status") in {"pending", "running"}:
                state["status"] = "cancelled"
                state["completed_at"] = time.time()
                state["current_step_status"] = "failed"
            state["state_seq"] = int(state.get("state_seq") or 0) + 1
            self._persist_state()
            await self._notify_change(dict(state))
            process = self._current_process
            process_task_id = self._current_process_task_id

        if process is not None and process.returncode is None and process_task_id == task_key:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception("automation_v2: failed to terminate process for task=%s", task_key)

        return await self.get_state(task_key)

    async def wait_for_change(self, task_id: str, since_seq: int | None = None) -> Dict[str, Any]:
        task_key = str(task_id).strip()
        target_seq = int(since_seq or 0)

        while True:
            async with self._lock:
                state = self._state.get("tasks", {}).get(task_key)
                if not isinstance(state, dict):
                    return {
                        "task_id": task_key,
                        "status": "unknown",
                        "current_step_index": 0,
                        "current_step_status": "pending",
                        "steps": [],
                        "last_error": "task not found",
                        "state_seq": 0,
                    }
                current_seq = int(state.get("state_seq") or 0)
                terminal = str(state.get("status") or "").lower() in {"success", "failed", "cancelled"}
                if current_seq > target_seq or terminal:
                    return dict(state)

            async with self._change_condition:
                await self._change_condition.wait()

    async def _run_task(self, task_id: str) -> None:
        while True:
            async with self._lock:
                state = self._state.get("tasks", {}).get(task_id)
                if not isinstance(state, dict):
                    return
                if state.get("cancel_requested"):
                    state["status"] = "cancelled"
                    state["completed_at"] = state.get("completed_at") or time.time()
                    state["current_step_status"] = "failed"
                    state["state_seq"] = int(state.get("state_seq") or 0) + 1
                    self._state["active_task_id"] = None
                    self._persist_state()
                    await self._notify_change(dict(state))
                    return
                if state.get("status") in {"success", "failed", "cancelled"}:
                    self._state["active_task_id"] = None
                    self._persist_state()
                    await self._notify_change(dict(state))
                    return
                instructions = state.get("instructions") or []
                if not isinstance(instructions, list):
                    state["status"] = "failed"
                    state["last_error"] = "invalid instructions payload"
                    state["completed_at"] = time.time()
                    state["current_step_status"] = "failed"
                    self._state["active_task_id"] = None
                    self._persist_state()
                    return
                step_index = int(state.get("current_step_index") or 0)
                if step_index >= len(instructions):
                    state["status"] = "success"
                    state["current_step_status"] = "success"
                    state["completed_at"] = time.time()
                    state["state_seq"] = int(state.get("state_seq") or 0) + 1
                    self._state["active_task_id"] = None
                    self._persist_state()
                    await self._notify_change(dict(state))
                    return
                step = instructions[step_index]
                command_text = _extract_step_command(step)
                if not command_text:
                    state["current_step_index"] = step_index + 1
                    state["current_step_status"] = "success"
                    self._persist_state()
                    continue

                if not state.get("started_at"):
                    state["started_at"] = time.time()
                state["status"] = "running"
                state["current_step_status"] = "running"
                state["state_seq"] = int(state.get("state_seq") or 0) + 1
                self._persist_state()
                await self._notify_change(dict(state))
                default_timeout = float(state.get("default_timeout_seconds") or DEFAULT_STEP_TIMEOUT_SECONDS)
                timeout_seconds = _extract_step_timeout(step, default_timeout)

            start = time.monotonic()
            process = await asyncio.create_subprocess_shell(
                command_text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._current_process = process
            self._current_process_task_id = task_id

            pending_stdout: list[str] = []
            pending_stderr: list[str] = []
            final_stdout: list[str] = []
            final_stderr: list[str] = []
            pending_lock = asyncio.Lock()
            stop_event = asyncio.Event()
            task_id_payload: Any = int(task_id) if str(task_id).isdigit() else str(task_id)

            async def _append_pending(bucket: list[str], text: str) -> None:
                async with pending_lock:
                    bucket.append(text)

            async def _read_stream(stream, pending: list[str], archive: list[str]) -> None:
                while True:
                    chunk = await stream.read(1024)
                    if not chunk:
                        break
                    text = chunk.decode(errors="replace")
                    await _append_pending(pending, text)
                    archive.append(text)

            async def _flush_pending() -> None:
                async with pending_lock:
                    stdout_chunk = "".join(pending_stdout)
                    stderr_chunk = "".join(pending_stderr)
                    pending_stdout.clear()
                    pending_stderr.clear()
                if not stdout_chunk and not stderr_chunk:
                    return
                sender = self._event_sender
                if sender is None:
                    return
                payload: Dict[str, Any] = {
                    "event": "terminal_exec_output",
                    "command": command_text,
                    "automation_task_id": task_id_payload,
                    "automation_step_index": step_index,
                }
                if stdout_chunk:
                    payload["stdout"] = stdout_chunk
                if stderr_chunk:
                    payload["stderr"] = stderr_chunk
                try:
                    await sender(payload)
                except Exception:
                    logger.exception("automation_v2: failed to emit terminal_exec_output")

            async def _streaming_flusher() -> None:
                try:
                    while True:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=OUTPUT_FLUSH_INTERVAL_S)
                            break
                        except asyncio.TimeoutError:
                            await _flush_pending()
                    await _flush_pending()
                except asyncio.CancelledError:
                    await _flush_pending()
                    raise

            stdout_task = asyncio.create_task(_read_stream(process.stdout, pending_stdout, final_stdout))
            stderr_task = asyncio.create_task(_read_stream(process.stderr, pending_stderr, final_stderr))
            flusher_task = asyncio.create_task(_streaming_flusher())

            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                process.kill()
                await process.wait()
            finally:
                stop_event.set()
                await asyncio.gather(stdout_task, stderr_task, flusher_task, return_exceptions=True)
                self._current_process = None
                self._current_process_task_id = None

            duration_s = round(time.monotonic() - start, 3)
            stdout_text = _trim_text("".join(final_stdout))
            stderr_text = _trim_text("".join(final_stderr))
            returncode = process.returncode

            if self._event_sender is not None:
                try:
                    await self._event_sender(
                        {
                            "event": "terminal_exec_result",
                            "command": command_text,
                            "returncode": returncode,
                            "stdout": stdout_text,
                            "stderr": stderr_text,
                            "duration_s": duration_s,
                            "automation_task_id": task_id_payload,
                            "automation_step_index": step_index,
                        }
                    )
                except Exception:
                    logger.exception("automation_v2: failed to emit terminal_exec_result")

            async with self._lock:
                state = self._state.get("tasks", {}).get(task_id)
                if not isinstance(state, dict):
                    return
                steps = state.setdefault("steps", [])
                while len(steps) <= step_index:
                    steps.append({"status": "pending"})

                step_result = {
                    "index": step_index,
                    "command": command_text,
                    "status": "failed" if timed_out or returncode != 0 else "success",
                    "returncode": returncode,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "duration_s": duration_s,
                    "completed_at": time.time(),
                }
                if timed_out:
                    step_result["error"] = f"step timed out after {timeout_seconds}s"
                steps[step_index] = step_result

                if state.get("cancel_requested"):
                    state["status"] = "cancelled"
                    state["current_step_status"] = "failed"
                    state["completed_at"] = time.time()
                    state["state_seq"] = int(state.get("state_seq") or 0) + 1
                    self._state["active_task_id"] = None
                    self._persist_state()
                    await self._notify_change(dict(state))
                    return

                if timed_out or returncode != 0:
                    state["status"] = "failed"
                    state["current_step_status"] = "failed"
                    state["completed_at"] = time.time()
                    state["last_error"] = step_result.get("error") or stderr_text or "command failed"
                    state["state_seq"] = int(state.get("state_seq") or 0) + 1
                    self._state["active_task_id"] = None
                    self._persist_state()
                    await self._notify_change(dict(state))
                    return

                state["current_step_index"] = step_index + 1
                state["current_step_status"] = "pending"
                state["state_seq"] = int(state.get("state_seq") or 0) + 1
                self._persist_state()
                await self._notify_change(dict(state))


_RUNTIME = _AutomationRuntimeV2(STATE_FILE_PATH)


class AutomationV2StartHandler(AsyncHandler):
    def __init__(self, control_channel, context):
        super().__init__(control_channel, context)
        # Rebind runtime emitter to the currently active connection at handler registration.
        _RUNTIME.set_event_sender(self._emit_state_event)

    async def _emit_state_event(self, payload: Dict[str, Any]) -> None:
        enhanced_payload = dict(payload)
        manager = self.context.get("client_session_manager")
        if manager:
            sessions = list(manager.get_sessions().keys())
            if sessions:
                enhanced_payload["client_sessions"] = sessions
        enhanced_payload["bypass_session_gate"] = True
        await self.control_channel.send(enhanced_payload)

    @property
    def command_name(self) -> str:
        return "automation_v2_start"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        _RUNTIME.set_event_sender(self._emit_state_event)
        task_id = message.get("task_id")
        instructions = message.get("instructions")
        timeout_param = message.get("step_timeout_seconds", DEFAULT_STEP_TIMEOUT_SECONDS)
        try:
            timeout_value = float(timeout_param)
            if timeout_value <= 0:
                timeout_value = DEFAULT_STEP_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            timeout_value = DEFAULT_STEP_TIMEOUT_SECONDS

        state = await _RUNTIME.start(str(task_id), instructions, timeout_value)
        return {
            "event": "automation_v2_started",
            "task_id": str(task_id),
            "state": state,
        }


class AutomationV2StateHandler(AsyncHandler):
    def __init__(self, control_channel, context):
        super().__init__(control_channel, context)
        # Rebind runtime emitter to the currently active connection at handler registration.
        _RUNTIME.set_event_sender(self._emit_state_event)

    async def _emit_state_event(self, payload: Dict[str, Any]) -> None:
        enhanced_payload = dict(payload)
        manager = self.context.get("client_session_manager")
        if manager:
            sessions = list(manager.get_sessions().keys())
            if sessions:
                enhanced_payload["client_sessions"] = sessions
        enhanced_payload["bypass_session_gate"] = True
        await self.control_channel.send(enhanced_payload)

    @property
    def command_name(self) -> str:
        return "automation_v2_state"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        _RUNTIME.set_event_sender(self._emit_state_event)
        task_id = message.get("task_id")
        if task_id is None:
            raise ValueError("task_id is required")
        state = await _RUNTIME.get_state(str(task_id))
        return {
            "event": "automation_v2_state",
            "task_id": str(task_id),
            "state": state,
        }


class AutomationV2CancelHandler(AsyncHandler):
    def __init__(self, control_channel, context):
        super().__init__(control_channel, context)
        # Rebind runtime emitter to the currently active connection at handler registration.
        _RUNTIME.set_event_sender(self._emit_state_event)

    async def _emit_state_event(self, payload: Dict[str, Any]) -> None:
        enhanced_payload = dict(payload)
        manager = self.context.get("client_session_manager")
        if manager:
            sessions = list(manager.get_sessions().keys())
            if sessions:
                enhanced_payload["client_sessions"] = sessions
        enhanced_payload["bypass_session_gate"] = True
        await self.control_channel.send(enhanced_payload)

    @property
    def command_name(self) -> str:
        return "automation_v2_cancel"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        _RUNTIME.set_event_sender(self._emit_state_event)
        task_id = message.get("task_id")
        if task_id is None:
            raise ValueError("task_id is required")
        state = await _RUNTIME.cancel(str(task_id))
        return {
            "event": "automation_v2_cancelled",
            "task_id": str(task_id),
            "state": state,
        }
