"""Handlers for applying unified diffs to project files."""

import asyncio
import logging
import os
import re
import time
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from .base import AsyncHandler
from .project_state.manager import get_or_create_project_state_manager
from ...utils import diff_renderer
from ...utils.diff_apply import (
    DiffApplyError,
    DiffParseError,
    FilePatch,
    Hunk,
    PatchLine,
    apply_file_patch,
    preview_file_patch,
    parse_unified_diff,
)

logger = logging.getLogger(__name__)
_DEBUG_LOG_PATH = os.path.expanduser("~/portacode_diff_debug.log")


def _debug_log(message: str) -> None:
    """Append debug traces for troubleshooting without affecting runtime."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except Exception:
        # Ignore logging errors entirely.
        pass


def _resolve_preview_path(base_path: str, relative_path: Optional[str]) -> str:
    """Compute an absolute path hint for diff previews."""
    if not relative_path:
        return base_path
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.abspath(os.path.join(base_path, relative_path))


_DIRECTIVE_LINE_PATTERN = re.compile(r"^@@(?P<cmd>[a-z_]+):(?P<body>.+)@@$", re.IGNORECASE)


def _normalize_directive_path(raw: str) -> str:
    """Normalize relative paths referenced by inline directives."""
    if raw is None:
        raise ValueError("Path is required")
    candidate = os.path.normpath(raw.strip())
    if candidate in ("", ".", ".."):
        raise ValueError("Path must reference a file inside the project")
    if os.path.isabs(candidate):
        raise ValueError("Absolute paths are not allowed in inline directives")
    if candidate.startswith(".."):
        raise ValueError("Path cannot traverse outside the project")
    return candidate


def _extract_inline_directives(diff_text: str) -> Tuple[str, List[Dict[str, str]]]:
    """Strip inline @@command directives and return (clean_diff, directives)."""
    if not diff_text:
        return "", []

    directives: List[Dict[str, str]] = []
    remaining_lines: List[str] = []

    for line in diff_text.splitlines(keepends=True):
        stripped = line.strip()
        match = _DIRECTIVE_LINE_PATTERN.match(stripped)
        if not match:
            remaining_lines.append(line)
            continue

        cmd = match.group("cmd").lower()
        body = (match.group("body") or "").strip()
        if not body:
            raise ValueError(f"Inline directive '{cmd}' is missing required arguments")

        if cmd == "delete":
            directives.append(
                {
                    "type": "delete",
                    "path": _normalize_directive_path(body),
                }
            )
        elif cmd in {"move", "rename"}:
            if "->" not in body:
                raise ValueError("move directive must be formatted as 'source -> destination'")
            source_raw, dest_raw = body.split("->", 1)
            source = _normalize_directive_path(source_raw)
            destination = _normalize_directive_path(dest_raw)
            if source == destination:
                raise ValueError("Source and destination paths must be different for move directives")
            directives.append(
                {
                    "type": "move",
                    "source": source,
                    "destination": destination,
                }
            )
        else:
            raise ValueError(f"Unknown inline directive '{cmd}'")

    return "".join(remaining_lines), directives


def _resolve_directive_path(base_path: str, relative_path: str) -> str:
    """Resolve a directive path and enforce that it stays inside the project root."""
    root = os.path.abspath(base_path or os.getcwd())
    target = os.path.abspath(os.path.join(root, relative_path))
    if os.path.commonpath([root, target]) != root:
        raise DiffApplyError(f"Path escapes project root: {relative_path}", file_path=target)
    return target


def _read_all_lines(abs_path: str) -> List[str]:
    """Load file content for directive handling."""
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            data = fh.read()
        return data.splitlines(keepends=True)
    except FileNotFoundError as exc:
        raise DiffApplyError(f"File does not exist: {abs_path}", file_path=abs_path) from exc
    except OSError as exc:
        raise DiffApplyError(f"Unable to read file: {abs_path}", file_path=abs_path) from exc


def _build_delete_patch(relative_path: str, lines: List[str]) -> FilePatch:
    """Construct a FilePatch that removes an entire file."""
    hunk = Hunk(
        old_start=1,
        old_length=len(lines),
        new_start=1,
        new_length=0,
        lines=[PatchLine("-", line) for line in lines],
    )
    return FilePatch(old_path=relative_path, new_path="/dev/null", hunks=[hunk])


def _build_add_patch(relative_path: str, lines: List[str]) -> FilePatch:
    """Construct a FilePatch that creates a file with the provided lines."""
    hunk = Hunk(
        old_start=1,
        old_length=0,
        new_start=1,
        new_length=len(lines),
        lines=[PatchLine("+", line) for line in lines],
    )
    return FilePatch(old_path="/dev/null", new_path=relative_path, hunks=[hunk])


def _build_directive_patches(directives: List[Dict[str, str]], base_path: str) -> List[FilePatch]:
    """Translate inline directives into concrete FilePatch objects."""
    if not directives:
        return []

    patches: List[FilePatch] = []
    base = base_path or os.getcwd()

    for directive in directives:
        if directive["type"] == "delete":
            rel_path = directive["path"]
            abs_path = _resolve_directive_path(base, rel_path)
            lines = _read_all_lines(abs_path)
            patches.append(_build_delete_patch(rel_path, lines))
        elif directive["type"] == "move":
            source_rel = directive["source"]
            dest_rel = directive["destination"]
            source_abs = _resolve_directive_path(base, source_rel)
            dest_abs = _resolve_directive_path(base, dest_rel)
            if os.path.exists(dest_abs):
                raise DiffApplyError(f"Destination already exists: {dest_abs}", file_path=dest_abs)
            lines = _read_all_lines(source_abs)
            patches.append(_build_delete_patch(source_rel, lines))
            patches.append(_build_add_patch(dest_rel, lines))
        else:
            raise DiffApplyError(f"Unsupported directive: {directive['type']}")

    return patches


class FileApplyDiffHandler(AsyncHandler):
    """Handler that applies unified diff patches to one or more files."""

    @property
    def command_name(self) -> str:
        return "file_apply_diff"

    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle the command by executing it and sending the response to the requesting client session."""
        logger.info("handler: Processing command %s with reply_channel=%s",
                   self.command_name, reply_channel)
        _debug_log(
            f"handle start cmd={self.command_name} request_id={message.get('request_id')} "
            f"project_id={message.get('project_id')} base_path={message.get('base_path')} "
            f"diff_chars={len(message.get('diff') or '')}"
        )

        try:
            response = await self.execute(message)
            logger.info("handler: Command %s executed successfully", self.command_name)

            # Automatically copy request_id if present in the incoming message
            if "request_id" in message and "request_id" not in response:
                response["request_id"] = message["request_id"]

            # Get the source client session from the message
            source_client_session = message.get("source_client_session")
            project_id = response.get("project_id")

            logger.info("handler: %s response project_id=%s, source_client_session=%s",
                       self.command_name, project_id, source_client_session)

            # Send response only to the requesting client session
            if source_client_session:
                # Add client_sessions field to target only the requesting session
                response["client_sessions"] = [source_client_session]

                import json
                logger.info("handler: 📤 SENDING EVENT '%s' (via direct control_channel.send)", response.get("event", "unknown"))
                logger.info("handler: 📤 FULL EVENT PAYLOAD: %s", json.dumps(response, indent=2, default=str))

                await self.control_channel.send(response)
            else:
                # Fallback to original behavior if no source_client_session
                await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("handler: Error in command %s: %s", self.command_name, exc)
            _debug_log(
                f"handle error cmd={self.command_name} request_id={message.get('request_id')} error={exc}"
            )
            error_payload = {
                "event": "file_apply_diff_response",
                "project_id": message.get("project_id"),
                "base_path": message.get("base_path") or os.getcwd(),
                "results": [],
                "files_changed": 0,
                "status": "error",
                "success": False,
                "error": str(exc),
            }
            if "request_id" in message:
                error_payload["request_id"] = message["request_id"]

            source_client_session = message.get("source_client_session")
            if source_client_session:
                error_payload["client_sessions"] = [source_client_session]
                await self.control_channel.send(error_payload)
            else:
                await self.send_response(error_payload, reply_channel, message.get("project_id"))
        else:
            _debug_log(
                f"handle complete cmd={self.command_name} request_id={message.get('request_id')} "
                f"status={(response or {}).get('status') if response else 'no-response'}"
            )

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        diff_text = message.get("diff")
        if not diff_text or not diff_text.strip():
            raise ValueError("diff parameter is required")

        project_id = message.get("project_id")
        source_client_session = message.get("source_client_session")
        requested_base_path = message.get("base_path")

        manager = None
        project_root: Optional[str] = None
        if source_client_session:
            try:
                manager = get_or_create_project_state_manager(self.context, self.control_channel)
                project_state = manager.projects.get(source_client_session)
                if project_state:
                    project_root = project_state.project_folder_path
            except Exception:
                logger.exception("file_apply_diff: Unable to determine project root for session %s", source_client_session)

        base_path = requested_base_path or project_root or os.getcwd()
        logger.info("file_apply_diff: Using base path %s", base_path)

        try:
            cleaned_diff, directives = _extract_inline_directives(diff_text)
        except ValueError as exc:
            raise ValueError(f"Invalid inline directive: {exc}") from exc

        file_patches: List[FilePatch] = []
        if cleaned_diff.strip():
            try:
                file_patches = parse_unified_diff(cleaned_diff)
            except DiffParseError as exc:
                raise ValueError(f"Invalid diff content: {exc}") from exc

        try:
            directive_patches = _build_directive_patches(directives, base_path)
            file_patches = directive_patches + file_patches
        except DiffApplyError as exc:
            raise ValueError(str(exc)) from exc

        if not file_patches:
            raise ValueError("No file changes were provided")

        results: List[Dict[str, Any]] = []
        applied_paths: List[str] = []
        loop = asyncio.get_running_loop()
        _debug_log(
            f"execute parsed {len(file_patches)} patches base_path={base_path} "
            f"source_session={source_client_session}"
        )

        for file_patch in file_patches:
            heuristics: List[str] = []
            apply_func = partial(
                apply_file_patch, file_patch, base_path, heuristic_log=heuristics
            )
            try:
                target_path, action, bytes_written = await loop.run_in_executor(None, apply_func)
                applied_paths.append(target_path)
                result_entry = {
                    "path": target_path,
                    "status": "applied",
                    "action": action,
                    "bytes_written": bytes_written,
                }
                if heuristics:
                    result_entry["heuristic_adjustments"] = heuristics
                results.append(result_entry)
                logger.info("file_apply_diff: %s %s (%s bytes)", action, target_path, bytes_written)
            except DiffApplyError as exc:
                logger.warning("file_apply_diff: Failed to apply diff for %s: %s", file_patch.target_path, exc)
                results.append(
                    {
                        "path": file_patch.target_path,
                        "status": "error",
                        "error": str(exc),
                        "line": getattr(exc, "line_number", None),
                    }
                )
            except Exception as exc:
                logger.exception("file_apply_diff: Unexpected error applying patch")
                results.append(
                    {
                        "path": file_patch.target_path,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        if manager and applied_paths:
            for path in applied_paths:
                try:
                    await manager.refresh_project_state_for_file_change(path)
                except Exception:
                    logger.exception("file_apply_diff: Failed to refresh project state for %s", path)

        success_count = sum(1 for result in results if result["status"] == "applied")
        failure_count = len(results) - success_count
        overall_status = "success"
        if success_count and failure_count:
            overall_status = "partial_failure"
        elif failure_count and not success_count:
            overall_status = "failed"

        response = {
            "event": "file_apply_diff_response",
            "project_id": project_id,
            "base_path": base_path,
            "results": results,
            "files_changed": success_count,
            "status": overall_status,
            "success": failure_count == 0,
        }
        _debug_log(
            f"execute done request_id={message.get('request_id')} success={response['success']} "
            f"files_changed={success_count} failures={failure_count}"
        )
        return response


class FilePreviewDiffHandler(AsyncHandler):
    """Handler that validates diffs and returns HTML previews without applying changes."""

    @property
    def command_name(self) -> str:
        return "file_preview_diff"

    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        logger.info(
            "handler: Processing command %s with reply_channel=%s",
            self.command_name,
            reply_channel,
        )
        _debug_log(
            f"handle start cmd={self.command_name} request_id={message.get('request_id')} "
            f"project_id={message.get('project_id')} base_path={message.get('base_path')} "
            f"diff_chars={len(message.get('diff') or '')}"
        )

        try:
            response = await self.execute(message)
            logger.info("handler: Command %s executed successfully", self.command_name)

            if "request_id" in message and "request_id" not in response:
                response["request_id"] = message["request_id"]

            source_client_session = message.get("source_client_session")
            project_id = response.get("project_id")
            logger.info(
                "handler: %s response project_id=%s, source_client_session=%s",
                self.command_name,
                project_id,
                source_client_session,
            )

            if source_client_session:
                response["client_sessions"] = [source_client_session]
                import json

                logger.info(
                    "handler: 📤 SENDING EVENT '%s' (via direct control_channel.send)",
                    response.get("event", "unknown"),
                )
                logger.info(
                    "handler: 📤 FULL EVENT PAYLOAD: %s",
                    json.dumps(response, indent=2, default=str),
                )
                await self.control_channel.send(response)
            else:
                await self.send_response(response, reply_channel, project_id)
        except Exception as exc:
            logger.exception("handler: Error in command %s: %s", self.command_name, exc)
            _debug_log(
                f"handle error cmd={self.command_name} request_id={message.get('request_id')} error={exc}"
            )
            error_payload = {
                "event": "file_preview_diff_response",
                "project_id": message.get("project_id"),
                "base_path": message.get("base_path") or os.getcwd(),
                "previews": [],
                "status": "error",
                "success": False,
                "error": str(exc),
            }
            if "request_id" in message:
                error_payload["request_id"] = message["request_id"]

            source_client_session = message.get("source_client_session")
            if source_client_session:
                error_payload["client_sessions"] = [source_client_session]
                await self.control_channel.send(error_payload)
            else:
                await self.send_response(error_payload, reply_channel, message.get("project_id"))
        else:
            _debug_log(
                f"handle complete cmd={self.command_name} request_id={message.get('request_id')} "
                f"status={(response or {}).get('status') if response else 'no-response'}"
            )

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        diff_text = message.get("diff")
        if not diff_text or not diff_text.strip():
            raise ValueError("diff parameter is required")

        project_id = message.get("project_id")
        source_client_session = message.get("source_client_session")
        requested_base_path = message.get("base_path")

        manager = None
        project_root: Optional[str] = None
        if source_client_session:
            try:
                manager = get_or_create_project_state_manager(self.context, self.control_channel)
                project_state = manager.projects.get(source_client_session)
                if project_state:
                    project_root = project_state.project_folder_path
            except Exception:
                logger.exception(
                    "file_preview_diff: Unable to determine project root for session %s",
                    source_client_session,
                )

        base_path = requested_base_path or project_root or os.getcwd()
        logger.info("file_preview_diff: Using base path %s", base_path)

        try:
            cleaned_diff, directives = _extract_inline_directives(diff_text)
        except ValueError as exc:
            raise ValueError(f"Invalid inline directive: {exc}") from exc

        file_patches: List[FilePatch] = []
        if cleaned_diff.strip():
            try:
                file_patches = parse_unified_diff(cleaned_diff)
            except DiffParseError as exc:
                raise ValueError(f"Invalid diff content: {exc}") from exc

        try:
            directive_patches = _build_directive_patches(directives, base_path)
            file_patches = directive_patches + file_patches
        except DiffApplyError as exc:
            raise ValueError(str(exc)) from exc

        if not file_patches:
            raise ValueError("No file changes were provided")

        previews: List[Dict[str, Any]] = []

        for file_patch in file_patches:
            target_hint = file_patch.target_path or file_patch.new_path or file_patch.old_path
            display_path = _resolve_preview_path(base_path, target_hint)

            heuristics: List[str] = []
            try:
                (
                    preview_path,
                    file_action,
                    original_lines,
                    updated_lines,
                ) = preview_file_patch(
                    file_patch, base_path, heuristic_log=heuristics
                )
            except DiffApplyError as exc:
                logger.exception(
                    "file_preview_diff: Unable to compute preview for %s", display_path
                )
                previews.append(
                    {
                        "path": display_path,
                        "relative_path": target_hint,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            try:
                if file_action == "deleted":
                    preview_action = "deleted"
                elif file_action == "created":
                    preview_action = "added"
                else:
                    preview_action = "modified"

                original_text = "".join(original_lines)
                updated_text = "".join(updated_lines)
                html_versions = diff_renderer.generate_html_diff(
                    original_text, updated_text, display_path
                )
                if not html_versions:
                    fallback = diff_renderer.generate_fallback_diff_html(display_path)
                    html_versions = {"minimal": fallback, "full": fallback}

                preview_entry = {
                    "path": display_path,
                    "relative_path": target_hint,
                    "status": "ready",
                    "html": html_versions["minimal"],
                    "html_versions": html_versions,
                    "has_full": html_versions["minimal"] != html_versions["full"],
                    "action": preview_action,
                }
                if heuristics:
                    preview_entry["heuristic_adjustments"] = heuristics
                previews.append(preview_entry)
            except Exception as exc:
                logger.exception(
                    "file_preview_diff: Failed to render preview for %s", display_path
                )
                previews.append(
                    {
                        "path": display_path,
                        "relative_path": target_hint,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        success_count = sum(1 for result in previews if result["status"] == "ready")
        failure_count = len(previews) - success_count
        overall_status = "success"
        if success_count and failure_count:
            overall_status = "partial_failure"
        elif failure_count and not success_count:
            overall_status = "failed"

        response = {
            "event": "file_preview_diff_response",
            "project_id": project_id,
            "base_path": base_path,
            "previews": previews,
            "status": overall_status,
            "success": failure_count == 0,
        }
        _debug_log(
            f"preview execute done request_id={message.get('request_id')} "
            f"success={response['success']} previews={len(previews)}"
        )
        return response
