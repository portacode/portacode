"""Handlers for applying unified diffs to project files."""

import asyncio
import logging
import os
from functools import partial
from typing import Any, Dict, List, Optional

from .base import AsyncHandler
from .project_state.manager import get_or_create_project_state_manager
from ...utils.diff_apply import (
    DiffApplyError,
    DiffParseError,
    apply_file_patch,
    parse_unified_diff,
)

logger = logging.getLogger(__name__)


class FileApplyDiffHandler(AsyncHandler):
    """Handler that applies unified diff patches to one or more files."""

    @property
    def command_name(self) -> str:
        return "file_apply_diff"

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

        return {
            "event": "file_apply_diff_response",
            "project_id": project_id,
            "base_path": base_path,
            "results": results,
            "files_changed": success_count,
            "status": overall_status,
            "success": failure_count == 0,
        }
