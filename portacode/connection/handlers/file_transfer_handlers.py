"""Isolated handlers for websocket file upload and download transfers."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import pwd
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from .base import AsyncHandler, SyncHandler
from .chunked_content import ChunkAssembler, create_chunked_response
from .project_state.manager import get_or_create_project_state_manager
from .runtime_user import get_default_runtime_user

logger = logging.getLogger(__name__)


def _schedule_project_state_refresh(handler, changed_path: str) -> None:
    try:
        manager = get_or_create_project_state_manager(handler.context, handler.control_channel)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(manager.refresh_project_state_for_file_change(str(changed_path)))
    except Exception as exc:
        logger.warning("Failed to refresh project state after transfer for %s: %s", changed_path, exc)


def _chown_path_if_possible(path: str | Path, user: Optional[str]) -> None:
    if not user or os.name == "nt":
        return
    try:
        pw_entry = pwd.getpwnam(user)
        os.chown(path, pw_entry.pw_uid, pw_entry.pw_gid)
    except Exception:
        return


def _restore_metadata(path: Path, original_stat: Optional[os.stat_result]) -> None:
    if original_stat is None:
        return
    try:
        os.chmod(path, stat.S_IMODE(original_stat.st_mode))
    except OSError:
        pass
    try:
        os.chown(path, original_stat.st_uid, original_stat.st_gid)
    except OSError:
        pass


def _write_bytes_preserve_metadata(path: str | Path, content: bytes, *, create_user: Optional[str] = None) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    original_stat = target.stat() if existed else None

    with open(target, "wb") as file_obj:
        file_obj.write(content)

    if existed:
        _restore_metadata(target, original_stat)
    else:
        _chown_path_if_possible(target, create_user)

    return len(content)


class FileUploadHandler(SyncHandler):
    """Upload a file to disk, optionally across multiple websocket chunks."""

    def __init__(self, control_channel, context):
        super().__init__(control_channel, context)
        self._chunk_assembler = ChunkAssembler()

    @property
    def command_name(self) -> str:
        return "file_upload"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        path = message.get("path")
        file_name = message.get("file_name") or (Path(path).name if path else None)
        mime_type = message.get("mime_type") or "application/octet-stream"
        chunked = bool(message.get("chunked", False))

        try:
            self._chunk_assembler.cleanup_stale_transfers()
            if not path:
                raise ValueError("path parameter is required")

            content_base64 = message.get("content_base64")
            if chunked:
                content_base64 = self._chunk_assembler.add_chunk(message, "content_base64")
                if content_base64 is None:
                    return {
                        "event": "file_upload_response",
                        "path": path,
                        "file_name": file_name,
                        "mime_type": mime_type,
                        "transfer_id": message.get("transfer_id"),
                        "chunked": True,
                        "chunk_received": True,
                        "success": True,
                        "complete": False,
                    }

            if content_base64 is None:
                raise ValueError("content_base64 parameter is required")

            overwrite = bool(message.get("overwrite", False))
            file_path = Path(path)
            parent_dir = file_path.parent
            if not parent_dir.exists():
                raise ValueError(f"Parent directory does not exist: {parent_dir}")
            if not parent_dir.is_dir():
                raise ValueError(f"Parent path is not a directory: {parent_dir}")
            if file_path.exists() and not overwrite:
                raise ValueError(f"Target already exists: {file_path}")

            try:
                content_bytes = base64.b64decode(content_base64.encode("ascii"), validate=True)
            except Exception as exc:
                raise ValueError(f"Invalid base64 file content: {exc}") from exc

            bytes_written = _write_bytes_preserve_metadata(
                file_path,
                content_bytes,
                create_user=get_default_runtime_user(message),
            )
            _schedule_project_state_refresh(self, str(file_path))

            return {
                "event": "file_upload_response",
                "path": str(file_path),
                "file_name": file_name,
                "mime_type": mime_type,
                "bytes_written": bytes_written,
                "file_size": len(content_bytes),
                "chunked": chunked,
                "success": True,
                "complete": True,
            }
        except Exception as exc:
            return {
                "event": "file_upload_response",
                "path": path,
                "file_name": file_name,
                "mime_type": mime_type,
                "chunked": chunked,
                "success": False,
                "complete": True,
                "error": str(exc),
                "message": str(exc),
            }


class FileDownloadHandler(AsyncHandler):
    """Read a file from disk and send it as base64, chunked when large."""

    @property
    def command_name(self) -> str:
        return "file_download"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        path = message.get("path")
        try:
            if not path:
                raise ValueError("path parameter is required")

            file_path = Path(path)
            if not file_path.exists():
                raise ValueError(f"File not found: {path}")
            if not file_path.is_file():
                raise ValueError(f"Path is not a file: {path}")

            content_bytes = file_path.read_bytes()
            content_base64 = base64.b64encode(content_bytes).decode("ascii")
            base_response = {
                "event": "file_download_response",
                "path": str(file_path),
                "file_name": file_path.name,
                "mime_type": message.get("mime_type") or "application/octet-stream",
                "file_size": len(content_bytes),
                "success": True,
            }
            if "request_id" in message:
                base_response["request_id"] = message["request_id"]

            for response in create_chunked_response(base_response, "content_base64", content_base64):
                await self.send_response(response, project_id=message.get("project_id"))
            return None
        except Exception as exc:
            response = {
                "event": "file_download_response",
                "path": path,
                "success": False,
                "chunked": False,
                "error": str(exc),
                "message": str(exc),
            }
            if "request_id" in message:
                response["request_id"] = message["request_id"]
            return response
