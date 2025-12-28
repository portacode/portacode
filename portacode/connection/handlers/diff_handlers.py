"""Handlers for applying unified diffs to project files."""

import asyncio
import difflib
import logging
import os
import time
from functools import partial
from typing import Any, Dict, List, Optional

from .base import AsyncHandler
from .project_state.manager import get_or_create_project_state_manager
from .project_state.git_manager import GitManager
from ...utils.diff_apply import (
    DiffApplyError,
    DiffParseError,
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
            file_patches = parse_unified_diff(diff_text)
        except DiffParseError as exc:
            raise ValueError(f"Invalid diff content: {exc}") from exc

        results: List[Dict[str, Any]] = []
        applied_paths: List[str] = []
        loop = asyncio.get_running_loop()
        _debug_log(
            f"execute parsed {len(file_patches)} patches base_path={base_path} "
            f"source_session={source_client_session}"
        )

        for file_patch in file_patches:
            apply_func = partial(apply_file_patch, file_patch, base_path)
            try:
                target_path, action, bytes_written = await loop.run_in_executor(None, apply_func)
                applied_paths.append(target_path)
                results.append(
                    {
                        "path": target_path,
                        "status": "applied",
                        "action": action,
                        "bytes_written": bytes_written,
                    }
                )
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
            file_patches = parse_unified_diff(diff_text)
        except DiffParseError as exc:
            raise ValueError(f"Invalid diff content: {exc}") from exc

        previews: List[Dict[str, Any]] = []
        git_manager = GitManager(base_path)

        for file_patch in file_patches:
            target_hint = file_patch.target_path or file_patch.new_path or file_patch.old_path
            display_path = _resolve_preview_path(base_path, target_hint)

            try:
                (
                    preview_path,
                    file_action,
                    original_lines,
                    updated_lines,
                ) = preview_file_patch(file_patch, base_path)
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
                label = target_hint or os.path.basename(preview_path) or "file"
                minimal_diff_lines = list(
                    difflib.unified_diff(
                        original_lines,
                        updated_lines,
                        fromfile=f"a/{label}",
                        tofile=f"b/{label}",
                        lineterm="",
                        n=3,
                    )
                )

                total_lines = len(original_lines) + len(updated_lines)
                if total_lines <= 2000:
                    context_span = max(len(original_lines), len(updated_lines), 3)
                    full_diff_lines = list(
                        difflib.unified_diff(
                            original_lines,
                            updated_lines,
                            fromfile=f"a/{label}",
                            tofile=f"b/{label}",
                            lineterm="",
                            n=context_span,
                        )
                    )
                else:
                    full_diff_lines = minimal_diff_lines

                parsed_minimal = git_manager._parse_unified_diff_simple(minimal_diff_lines)
                minimal_html = git_manager._generate_diff_html(
                    parsed_minimal, display_path, "minimal"
                )

                if full_diff_lines != minimal_diff_lines:
                    parsed_full = git_manager._parse_unified_diff_simple(full_diff_lines)
                    full_html = git_manager._generate_diff_html(
                        parsed_full, display_path, "full"
                    )
                else:
                    full_html = minimal_html

                previews.append(
                    {
                        "path": display_path,
                        "relative_path": target_hint,
                        "status": "ready",
                        "html": minimal_html,
                        "html_versions": {
                            "minimal": minimal_html,
                            "full": full_html,
                        },
                        "has_full": full_html != minimal_html,
                        "action": file_action,
                    }
                )
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
