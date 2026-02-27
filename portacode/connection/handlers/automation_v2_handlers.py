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
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .base import AsyncHandler

logger = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_SECONDS = 7200.0
MAX_STDIO_CHARS = 8000
OUTPUT_FLUSH_INTERVAL_S = 1.0
STATE_FILE_PATH = Path("/tmp/portacode_automation_v2_state.json")
EXPOSED_SERVICES_FILE_PATH = Path("/etc/portacode/exposed_services.json")
EXPOSED_SERVICES_ENV_KEY = "PORTACODE_EXPOSED_SERVICES_JSON"
WAIT_FOR_PLACEHOLDER_RE = re.compile(r"\[exposed:(\d{1,5})\]", flags=re.IGNORECASE)
WAIT_FOR_STEP_INTERVAL_SECONDS = 3.0
WAIT_FOR_REQUEST_TIMEOUT_SECONDS = 5.0
WAIT_FOR_DEFAULT_TIMEOUT_SECONDS = 600.0


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


def _extract_step_wait_for(step: Any) -> Optional[str]:
    if not isinstance(step, dict):
        return None
    normalized = {str(k).lower(): v for k, v in step.items()}
    value = normalized.get("wait_for")
    if value is None:
        return None
    target = str(value).strip()
    return target or None


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


async def _to_thread_compat(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        to_thread = getattr(asyncio, "to_thread", None)
        if callable(to_thread):
            return await to_thread(func, *args, **kwargs)
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    if kwargs:
        def _callable() -> Any:
            return func(*args, **kwargs)
        return await loop.run_in_executor(None, _callable)
    return await loop.run_in_executor(None, func, *args)


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
                wait_for_target = _extract_step_wait_for(step)
                if not command_text and not wait_for_target:
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
                timeout_fallback = WAIT_FOR_DEFAULT_TIMEOUT_SECONDS if wait_for_target else default_timeout
                timeout_seconds = _extract_step_timeout(step, timeout_fallback)
                task_id_payload: Any = int(task_id) if str(task_id).isdigit() else str(task_id)

            if wait_for_target:
                command_label = f"wait_for {wait_for_target}"
                logger.info(
                    "automation_v2 wait_for start task=%s step=%s target=%s timeout_s=%.1f",
                    task_id,
                    step_index,
                    wait_for_target,
                    timeout_seconds,
                )
                duration_start = time.monotonic()
                wait_result = await self._run_wait_for_step(
                    target=wait_for_target,
                    timeout_seconds=timeout_seconds,
                    automation_task_id=task_id_payload,
                    automation_step_index=step_index,
                )
                duration_s = round(time.monotonic() - duration_start, 3)
                stdout_text = _trim_text(wait_result.get("stdout") or "")
                stderr_text = _trim_text(wait_result.get("stderr") or "")
                returncode = wait_result.get("returncode")
                timed_out = bool(wait_result.get("timed_out"))
                resolved_url = wait_result.get("resolved_url")

                if self._event_sender is not None:
                    try:
                        await self._event_sender(
                            {
                                "event": "terminal_exec_result",
                                "command": command_label,
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
                        "command": command_label,
                        "status": "failed" if timed_out or returncode != 0 else "success",
                        "returncode": returncode,
                        "stdout": stdout_text,
                        "stderr": stderr_text,
                        "duration_s": duration_s,
                        "completed_at": time.time(),
                        "wait_for_target": wait_for_target,
                        "resolved_url": resolved_url,
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
                        logger.warning(
                            "automation_v2 wait_for failed task=%s step=%s target=%s resolved_url=%s stderr=%s",
                            task_id,
                            step_index,
                            wait_for_target,
                            resolved_url,
                            stderr_text,
                        )
                        state["status"] = "failed"
                        state["current_step_status"] = "failed"
                        state["completed_at"] = time.time()
                        state["last_error"] = step_result.get("error") or stderr_text or "wait_for failed"
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
                logger.info(
                    "automation_v2 wait_for success task=%s step=%s target=%s resolved_url=%s duration_s=%.3f",
                    task_id,
                    step_index,
                    wait_for_target,
                    resolved_url,
                    duration_s,
                )
                continue

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

    def _read_exposed_services(self) -> list[dict[str, Any]]:
        payload: Any = None
        env_raw = os.getenv(EXPOSED_SERVICES_ENV_KEY, "").strip()
        if env_raw:
            try:
                payload = json.loads(env_raw)
            except Exception:
                payload = None

        if payload is None and EXPOSED_SERVICES_FILE_PATH.exists():
            try:
                payload = json.loads(EXPOSED_SERVICES_FILE_PATH.read_text(encoding="utf-8"))
            except Exception:
                payload = None

        if isinstance(payload, dict):
            services = payload.get("exposed_services")
            if isinstance(services, list):
                return [item for item in services if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def _resolve_wait_for_url(self, target: str) -> str:
        value = str(target or "").strip()
        if not value:
            raise ValueError("wait_for target cannot be empty")
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("wait_for target must start with http:// or https://")

        services = self._read_exposed_services()
        if not WAIT_FOR_PLACEHOLDER_RE.search(value):
            return value

        hostname_by_port: dict[int, str] = {}
        for service in services:
            try:
                port = int(service.get("port"))
            except (TypeError, ValueError):
                continue
            hostname = str(service.get("hostname") or "").strip().lower().rstrip(".")
            if not hostname:
                raw_url = str(service.get("url") or "").strip()
                if raw_url:
                    parsed = urllib.parse.urlparse(raw_url)
                    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
            if hostname:
                hostname_by_port[port] = hostname

        def _replace(match: re.Match[str]) -> str:
            port = int(match.group(1))
            if port < 1 or port > 65535:
                raise ValueError("wait_for exposed port must be between 1 and 65535")
            hostname = hostname_by_port.get(port)
            if not hostname:
                raise ValueError(f"Unable to resolve [exposed:{port}] from exposed services data")
            return hostname

        return WAIT_FOR_PLACEHOLDER_RE.sub(_replace, value)

    def _probe_http_url(self, url: str) -> tuple[bool, int | None, str]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "portacode-automation-v2/1.0"})
            if url.lower().startswith("https://"):
                context = ssl._create_unverified_context()
                response = urllib.request.urlopen(request, timeout=WAIT_FOR_REQUEST_TIMEOUT_SECONDS, context=context)
            else:
                response = urllib.request.urlopen(request, timeout=WAIT_FOR_REQUEST_TIMEOUT_SECONDS)
            with response as resp:
                status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            return 200 <= status < 300, status, ""
        except Exception as exc:
            return False, None, str(exc)

    async def _run_wait_for_step(
        self,
        *,
        target: str,
        timeout_seconds: float,
        automation_task_id: Any,
        automation_step_index: int,
    ) -> dict[str, Any]:
        logger.info(
            "automation_v2 wait_for polling begin task=%s step=%s target=%s timeout_s=%.1f",
            automation_task_id,
            automation_step_index,
            target,
            timeout_seconds,
        )
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        attempts = 0
        last_error = ""
        resolved_url: str | None = None
        while time.monotonic() < deadline:
            attempts += 1
            try:
                resolved_url = self._resolve_wait_for_url(target)
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "automation_v2 wait_for resolve failed task=%s step=%s attempt=%s target=%s error=%s",
                    automation_task_id,
                    automation_step_index,
                    attempts,
                    target,
                    last_error,
                )
                if self._event_sender is not None:
                    try:
                        await self._event_sender(
                            {
                                "event": "terminal_exec_output",
                                "command": f"wait_for {target}",
                                "stdout": (
                                    f"[attempt {attempts}] resolving target {target}\n"
                                ),
                                "stderr": (
                                    f"[attempt {attempts}] resolve_failed error={last_error}\n"
                                ),
                                "automation_task_id": automation_task_id,
                                "automation_step_index": automation_step_index,
                            }
                        )
                    except Exception:
                        logger.exception("automation_v2: failed to emit terminal_exec_output")
                await asyncio.sleep(WAIT_FOR_STEP_INTERVAL_SECONDS)
                continue

            logger.info(
                "automation_v2 wait_for probe task=%s step=%s attempt=%s url=%s",
                automation_task_id,
                automation_step_index,
                attempts,
                resolved_url,
            )
            ok, status, error = await _to_thread_compat(self._probe_http_url, resolved_url)
            if ok:
                message = (
                    f"wait_for success url={resolved_url} status={status} attempts={attempts}"
                    if status is not None
                    else f"wait_for success url={resolved_url} attempts={attempts}"
                )
                logger.info(
                    "automation_v2 wait_for probe succeeded task=%s step=%s attempt=%s url=%s status=%s",
                    automation_task_id,
                    automation_step_index,
                    attempts,
                    resolved_url,
                    status,
                )
                if self._event_sender is not None:
                    try:
                        await self._event_sender(
                            {
                                "event": "terminal_exec_output",
                                "command": f"wait_for {target}",
                                "stdout": (
                                    f"[attempt {attempts}] GET {resolved_url} -> {status}\n"
                                    f"{message}\n"
                                ),
                                "automation_task_id": automation_task_id,
                                "automation_step_index": automation_step_index,
                            }
                        )
                    except Exception:
                        logger.exception("automation_v2: failed to emit terminal_exec_output")
                return {
                    "returncode": 0,
                    "stdout": message,
                    "stderr": "",
                    "timed_out": False,
                    "resolved_url": resolved_url,
                }
            last_error = error or (f"http_status={status}" if status is not None else "request failed")
            logger.info(
                "automation_v2 wait_for probe not-ready task=%s step=%s attempt=%s url=%s status=%s error=%s",
                automation_task_id,
                automation_step_index,
                attempts,
                resolved_url,
                status,
                last_error,
            )
            if self._event_sender is not None:
                try:
                    await self._event_sender(
                        {
                            "event": "terminal_exec_output",
                            "command": f"wait_for {target}",
                            "stdout": (
                                f"[attempt {attempts}] GET {resolved_url} -> "
                                f"{status if status is not None else 'error'}\n"
                            ),
                            "stderr": (
                                f"[attempt {attempts}] not_ready error={last_error}\n"
                            ),
                            "automation_task_id": automation_task_id,
                            "automation_step_index": automation_step_index,
                        }
                    )
                except Exception:
                    logger.exception("automation_v2: failed to emit terminal_exec_output")
            await asyncio.sleep(WAIT_FOR_STEP_INTERVAL_SECONDS)

        logger.warning(
            "automation_v2 wait_for timeout task=%s step=%s target=%s resolved_url=%s attempts=%s last_error=%s",
            automation_task_id,
            automation_step_index,
            target,
            resolved_url,
            attempts,
            last_error,
        )
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"wait_for timeout target={target} resolved_url={resolved_url or ''} last_error={last_error}",
            "timed_out": True,
            "resolved_url": resolved_url,
        }


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
