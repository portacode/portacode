"""Utilities for parsing and applying unified diff patches."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


class DiffParseError(Exception):
    """Raised when a diff cannot be parsed."""


class DiffApplyError(Exception):
    """Raised when a diff cannot be applied to a file."""

    def __init__(self, message: str, *, file_path: Optional[str] = None, line_number: Optional[int] = None):
        super().__init__(message)
        self.file_path = file_path
        self.line_number = line_number


@dataclass
class PatchLine:
    """Represents a single line in a hunk."""

    op: str  # ' ', '+', '-'
    text: str


@dataclass
class Hunk:
    """Represents a parsed hunk."""

    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: List[PatchLine]


@dataclass
class FilePatch:
    """Represents an entire file diff."""

    old_path: Optional[str]
    new_path: Optional[str]
    hunks: List[Hunk]

    @property
    def is_new_file(self) -> bool:
        return self.old_path in (None, "/dev/null")

    @property
    def is_delete(self) -> bool:
        return self.new_path in (None, "/dev/null")

    @property
    def target_path(self) -> Optional[str]:
        if self.is_delete and self.old_path not in (None, "/dev/null"):
            return self.old_path
        if self.new_path not in (None, "/dev/null"):
            return self.new_path
        return self.old_path


_HUNK_HEADER_RE = re.compile(
    r"@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@"
)


def _normalize_diff_path(raw_path: str) -> Optional[str]:
    """Normalize diff path lines (handles prefixes and tabs)."""
    path = raw_path.strip()
    if not path or path == "/dev/null":
        return None
    # Drop git prefixes like a/ and b/
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    # Remove any trailing metadata after tab (e.g., timestamps)
    if "\t" in path:
        path = path.split("\t", 1)[0]
    return path.strip()


def parse_unified_diff(diff_text: str) -> List[FilePatch]:
    """Parse unified diff text into FilePatch objects."""
    if not diff_text or not diff_text.strip():
        raise DiffParseError("Diff content is empty")

    lines = diff_text.splitlines(keepends=True)
    patches: List[FilePatch] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git"):
            i += 1
            continue

        if not line.startswith("--- "):
            i += 1
            continue

        old_path = _normalize_diff_path(line[4:].strip())
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise DiffParseError("Missing +++ line after --- line")
        new_path = _normalize_diff_path(lines[i][4:].strip())
        i += 1

        hunks: List[Hunk] = []
        while i < len(lines) and lines[i].startswith("@@"):
            header = lines[i]
            match = _HUNK_HEADER_RE.match(header)
            if not match:
                raise DiffParseError(f"Invalid hunk header: {header.strip()}")
            old_start = int(match.group("old_start"))
            old_len = int(match.group("old_len") or "1")
            new_start = int(match.group("new_start"))
            new_len = int(match.group("new_len") or "1")
            i += 1

            hunk_lines: List[PatchLine] = []
            while i < len(lines):
                current_line = lines[i]
                prefix = current_line[:1]
                # Stop if we encounter the start of the next file diff
                if current_line.startswith("diff --git ") or current_line.startswith("--- "):
                    break
                if prefix in {" ", "+", "-"}:
                    # Guard against accidental file headers inside a hunk
                    if prefix == "-" and current_line.startswith("--- "):
                        break
                    if prefix == "+" and current_line.startswith("+++ "):
                        break
                    hunk_lines.append(PatchLine(prefix, current_line[1:]))
                    i += 1
                elif current_line.startswith("\\ No newline at end of file"):
                    # Skip metadata line but keep processing
                    i += 1
                else:
                    break

            hunks.append(Hunk(old_start, old_len, new_start, new_len, hunk_lines))

        if not hunks:
            raise DiffParseError("No hunks found for file diff")

        patches.append(FilePatch(old_path, new_path, hunks))

    if not patches:
        raise DiffParseError("No valid file patches found in diff")

    return patches


def _normalize_target_path(path: str, base_path: Optional[str]) -> str:
    """Compute the absolute path for a diff target."""
    if os.path.isabs(path):
        return path
    base = base_path or os.getcwd()
    return os.path.abspath(os.path.join(base, path))


def _load_file_lines(path: str) -> Tuple[List[str], bool]:
    """Load file contents as a list of lines with newline characters preserved."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        return data.splitlines(keepends=True), True
    except FileNotFoundError:
        return [], False


def _collect_original_line_chunks(hunk: Hunk) -> List[List[str]]:
    """Collect consecutive lines from the original file that can anchor a hunk."""
    chunks: List[List[str]] = []
    current: List[str] = []

    for line in hunk.lines:
        if line.op in {" ", "-"}:
            current.append(line.text)
        elif current:
            chunks.append(list(current))
            current = []
    if current:
        chunks.append(list(current))

    return [chunk for chunk in chunks if chunk]


def _find_unique_chunk_position(original_lines: List[str], chunk: List[str]) -> Optional[int]:
    """Return the sole index where chunk appears, or None if ambiguous."""
    if not chunk or len(chunk) > len(original_lines):
        return None

    matches: List[int] = []
    max_start = len(original_lines) - len(chunk)
    for idx in range(0, max_start + 1):
        if original_lines[idx : idx + len(chunk)] == chunk:
            matches.append(idx)
            if len(matches) > 1:
                break
    if len(matches) == 1:
        return matches[0]
    return None


def _find_hunk_fallback_index(
    original_lines: List[str], hunk: Hunk
) -> Optional[Tuple[int, int]]:
    """Try to find a unique match for hunk context and return (index, context_length)."""
    chunks = _collect_original_line_chunks(hunk)
    if not chunks:
        return None

    chunks.sort(key=len, reverse=True)
    for chunk in chunks:
        pos = _find_unique_chunk_position(original_lines, chunk)
        if pos is not None:
            return pos, len(chunk)
    return None


def _apply_hunk_segment(
    original_lines: List[str],
    start_index: int,
    hunk: Hunk,
    *,
    file_path: str,
) -> Tuple[List[str], int]:
    """Apply a single hunk starting at a specific index."""
    if start_index > len(original_lines):
        raise DiffApplyError(
            "Hunk start position past end of file",
            file_path=file_path,
            line_number=hunk.old_start,
        )

    result: List[str] = []
    src_index = start_index
    current_line_number = start_index + 1

    for line in hunk.lines:
        if line.op == " ":
            if src_index >= len(original_lines):
                raise DiffApplyError(
                    "Unexpected end of file while matching context",
                    file_path=file_path,
                    line_number=current_line_number,
                )
            if original_lines[src_index] != line.text:
                raise DiffApplyError(
                    f"Context mismatch. Expected {original_lines[src_index]!r} but got {line.text!r}",
                    file_path=file_path,
                    line_number=current_line_number,
                )
            result.append(line.text)
            src_index += 1
            current_line_number += 1
        elif line.op == "-":
            if src_index >= len(original_lines):
                raise DiffApplyError(
                    "Unexpected end of file while removing line",
                    file_path=file_path,
                    line_number=current_line_number,
                )
            if original_lines[src_index] != line.text:
                raise DiffApplyError(
                    f"Deletion mismatch. Expected {original_lines[src_index]!r} but got {line.text!r}",
                    file_path=file_path,
                    line_number=current_line_number,
                )
            src_index += 1
            current_line_number += 1
        elif line.op == "+":
            result.append(line.text)
        else:
            raise DiffApplyError(
                f"Unsupported hunk operation {line.op!r}",
                file_path=file_path,
                line_number=current_line_number,
            )

    return result, src_index


def _apply_hunks(
    original_lines: List[str],
    hunks: List[Hunk],
    *,
    file_path: str,
    heuristic_log: Optional[List[str]] = None,
) -> List[str]:
    """Apply hunks to the provided original lines."""
    result: List[str] = []
    src_index = 0  # zero-based

    for hunk in hunks:
        desired_index = max(hunk.old_start - 1, 0)
        fallback = _find_hunk_fallback_index(original_lines, hunk)
        attempts: List[int] = [desired_index]
        fallback_context_len = 0
        fallback_index: Optional[int] = None
        if fallback:
            fallback_index, fallback_context_len = fallback
            if fallback_index != desired_index and fallback_index >= src_index:
                attempts.append(fallback_index)

        last_error: Optional[DiffApplyError] = None
        for attempt_index in attempts:
            if attempt_index < src_index:
                continue

            prefix_length = len(result)
            prefix_src = src_index
            if attempt_index > src_index:
                result.extend(original_lines[src_index:attempt_index])
                src_index = attempt_index

            try:
                applied_lines, new_src_index = _apply_hunk_segment(
                    original_lines,
                    attempt_index,
                    hunk,
                    file_path=file_path,
                )
                result.extend(applied_lines)
                src_index = new_src_index

                if (
                    heuristic_log is not None
                    and fallback_index is not None
                    and attempt_index == fallback_index
                ):
                    heuristic_log.append(
                        f"Hunk for {file_path} was shifted from line {desired_index + 1} to {fallback_index + 1} "
                        f"using a unique {fallback_context_len}-line context match."
                    )
                break
            except DiffApplyError as exc:
                last_error = exc
                del result[prefix_length:]
                src_index = prefix_src
                continue
        else:
            if last_error:
                raise last_error
            raise DiffApplyError(
                "Unable to apply hunk",
                file_path=file_path,
                line_number=hunk.old_start,
            )

    # Append remaining content
    if src_index < len(original_lines):
        result.extend(original_lines[src_index:])

    return result


def apply_file_patch(
    file_patch: FilePatch,
    base_path: Optional[str],
    heuristic_log: Optional[List[str]] = None,
) -> Tuple[str, str, int]:
    """Apply a parsed FilePatch to disk.

    Returns:
        Tuple[target_path, action, bytes_written]
    """
    target_rel = file_patch.target_path
    if not target_rel:
        raise DiffApplyError("Unable to determine target path for diff")

    target_path = _normalize_target_path(target_rel, base_path)
    original_lines, file_exists = _load_file_lines(target_path)

    if file_patch.is_new_file and file_exists:
        # For new files we expect the file to not exist, but if it does we treat it as modification
        pass
    elif not file_patch.is_new_file and not file_exists and not file_patch.is_delete:
        raise DiffApplyError(f"File does not exist: {target_path}", file_path=target_path)

    if file_patch.is_delete:
        if not file_exists:
            raise DiffApplyError(f"File does not exist: {target_path}", file_path=target_path)
        # Applying hunks ensures they match before deletion
        updated_lines = _apply_hunks(original_lines, file_patch.hunks, file_path=target_path)
        if os.path.exists(target_path):
            os.remove(target_path)
        return target_path, "deleted", 0

    updated_lines = _apply_hunks(
        original_lines,
        file_patch.hunks,
        file_path=target_path,
        heuristic_log=heuristic_log,
    )

    dir_name = os.path.dirname(target_path)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write("".join(updated_lines))

    bytes_written = sum(len(chunk.encode("utf-8")) for chunk in updated_lines)
    action = "created" if not file_exists else "modified"
    return target_path, action, bytes_written


def preview_file_patch(
    file_patch: FilePatch,
    base_path: Optional[str],
    heuristic_log: Optional[List[str]] = None,
) -> Tuple[str, str, List[str], List[str]]:
    """Compute the before/after contents for a FilePatch without writing to disk.

    Returns:
        Tuple[target_path, action, original_lines, updated_lines]
    """
    target_rel = file_patch.target_path
    if not target_rel:
        raise DiffApplyError("Unable to determine target path for diff preview")

    target_path = _normalize_target_path(target_rel, base_path)
    original_lines, file_exists = _load_file_lines(target_path)

    if file_patch.is_new_file and file_exists:
        # Treat as modification to allow previewing changes atop an existing file
        pass
    elif not file_patch.is_new_file and not file_exists and not file_patch.is_delete:
        raise DiffApplyError(f"File does not exist: {target_path}", file_path=target_path)

    if file_patch.is_delete:
        # Validate the hunks but the resulting file will be removed entirely
        _apply_hunks(
            original_lines,
            file_patch.hunks,
            file_path=target_path,
            heuristic_log=heuristic_log,
        )
        updated_lines = []
        action = "deleted"
    else:
        updated_lines = _apply_hunks(
            original_lines,
            file_patch.hunks,
            file_path=target_path,
            heuristic_log=heuristic_log,
        )
        action = "created" if not file_exists else "modified"

    return target_path, action, original_lines, updated_lines
