"""Stateless utilities for rendering unified diffs as HTML."""

from __future__ import annotations

import difflib
import logging
import os
import time
from typing import Dict, List, Optional

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_for_filename
    from pygments.util import ClassNotFound

    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False
    highlight = None  # type: ignore
    HtmlFormatter = None  # type: ignore
    get_lexer_for_filename = None  # type: ignore
    ClassNotFound = Exception  # type: ignore

logger = logging.getLogger(__name__)


def generate_html_diff(
    original_content: str,
    modified_content: str,
    file_path: str,
) -> Optional[Dict[str, str]]:
    """Generate unified HTML diff (minimal + full context) for two strings."""
    # If pygments isn't available we fall back to the simplified renderer
    if not PYGMENTS_AVAILABLE:
        return _generate_simple_diff_html(original_content, modified_content, file_path)

    # Basic safety limits to keep rendering responsive
    max_content_size = 500000  # 500KB
    max_lines = 5000
    original_line_count = original_content.count("\n")
    modified_line_count = modified_content.count("\n")
    if (
        len(original_content) > max_content_size
        or len(modified_content) > max_content_size
        or max(original_line_count, modified_line_count) > max_lines
    ):
        logger.warning("Large file detected for diff generation: %s", file_path)
        return _generate_simple_diff_html(original_content, modified_content, file_path)

    try:
        original_lines = original_content.splitlines(keepends=True)
        modified_lines = modified_content.splitlines(keepends=True)

        start_time = time.time()
        timeout_seconds = 5

        minimal_diff_lines = list(
            difflib.unified_diff(
                original_lines,
                modified_lines,
                fromfile=f"a/{os.path.basename(file_path)}",
                tofile=f"b/{os.path.basename(file_path)}",
                lineterm="",
                n=3,
            )
        )
        if time.time() - start_time > timeout_seconds:
            logger.warning("Diff generation timeout for %s", file_path)
            return None

        if len(original_lines) + len(modified_lines) < 2000:
            context_span = len(original_lines) + len(modified_lines)
            full_diff_lines = list(
                difflib.unified_diff(
                    original_lines,
                    modified_lines,
                    fromfile=f"a/{os.path.basename(file_path)}",
                    tofile=f"b/{os.path.basename(file_path)}",
                    lineterm="",
                    n=context_span,
                )
            )
        else:
            full_diff_lines = minimal_diff_lines

        parsed_minimal = parse_unified_diff_simple(minimal_diff_lines)
        parsed_full = parse_unified_diff_simple(full_diff_lines)

        if time.time() - start_time > timeout_seconds:
            logger.warning("Diff generation timeout for %s", file_path)
            return None

        minimal_html = render_diff_html(parsed_minimal, file_path, "minimal")
        full_html = render_diff_html(parsed_full, file_path, "full")
        return {"minimal": minimal_html, "full": full_html}
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Error generating HTML diff for %s: %s", file_path, exc)
        return None


def parse_unified_diff_simple(diff_lines: List[str]) -> List[Dict]:
    """Parse unified diff lines into structured rows (no intraline highlighting)."""
    parsed: List[Dict] = []
    old_line_num = 0
    new_line_num = 0

    for line in diff_lines:
        if line.startswith("@@"):
            import re

            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_line_num = int(match.group(1)) - 1
                new_line_num = int(match.group(2)) - 1
            parsed.append(
                {
                    "type": "header",
                    "content": line,
                    "old_line_num": "",
                    "new_line_num": "",
                }
            )
        elif line.startswith("---") or line.startswith("+++"):
            parsed.append(
                {
                    "type": "header",
                    "content": line,
                    "old_line_num": "",
                    "new_line_num": "",
                }
            )
        elif line.startswith("-"):
            old_line_num += 1
            parsed.append(
                {
                    "type": "delete",
                    "old_line_num": old_line_num,
                    "new_line_num": "",
                    "content": line,
                }
            )
        elif line.startswith("+"):
            new_line_num += 1
            parsed.append(
                {
                    "type": "add",
                    "old_line_num": "",
                    "new_line_num": new_line_num,
                    "content": line,
                }
            )
        elif line.startswith(" "):
            old_line_num += 1
            new_line_num += 1
            parsed.append(
                {
                    "type": "context",
                    "old_line_num": old_line_num,
                    "new_line_num": new_line_num,
                    "content": line,
                }
            )
    return parsed


def render_diff_html(parsed_diff: List[Dict], file_path: str, view_mode: str) -> str:
    """Convert parsed diff entries into styled HTML."""
    if len(parsed_diff) > 1000:
        logger.warning("Diff too large, truncating: %s (%s lines)", file_path, len(parsed_diff))
        parsed_diff = parsed_diff[:1000]

    lexer = _get_pygments_lexer(file_path)
    highlighted_cache = {}
    if lexer and PYGMENTS_AVAILABLE:
        unique_lines = {
            line_info["content"][1:].rstrip("\n")
            for line_info in parsed_diff
            if line_info.get("content") and line_info["content"][0] in "+- "
        }
        unique_lines = {line for line in unique_lines if line.strip()}
        if unique_lines:
            try:
                combined = "\n".join(unique_lines)
                highlighted = highlight(
                    combined, lexer, HtmlFormatter(nowrap=True, noclasses=False, style="monokai")
                )
                split_highlighted = highlighted.split("\n")
                unique_list = list(unique_lines)
                for idx, content in enumerate(unique_list):
                    if idx < len(split_highlighted):
                        highlighted_cache[content] = split_highlighted[idx]
            except Exception as exc:
                logger.debug("Error in batch syntax highlighting: %s", exc)
                highlighted_cache = {}

    html_parts: List[str] = []
    html_parts.append(f'<div class="unified-diff-container" data-view-mode="{view_mode}">')

    line_additions = sum(1 for line in parsed_diff if line["type"] == "add")
    line_deletions = sum(1 for line in parsed_diff if line["type"] == "delete")

    html_parts.append(
        f"""
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="additions">+{line_additions}</span>
                    <span class="deletions">-{line_deletions}</span>
                    <span class="file-path">{os.path.basename(file_path)}</span>
                </div>
                <div class="diff-stats-right">
                    <button class="diff-toggle-btn" data-current-mode="{view_mode}">
                        <i class="fas fa-eye"></i>
                        <span class="toggle-text"></span>
                    </button>
                </div>
            </div>
        """
    )

    html_parts.append('<div class="diff-content">')
    html_parts.append('<table class="diff-table">')

    for line_info in parsed_diff:
        if line_info["type"] == "header":
            continue

        line_type = line_info["type"]
        old_line_num = line_info.get("old_line_num", "")
        new_line_num = line_info.get("new_line_num", "")
        content = line_info.get("content", "")

        final_content = _escape_html(content)
        if content and content[0] in "+- ":
            prefix = content[0] if content[0] in "+-" else " "
            clean_content = content[1:].rstrip("\n")
            if clean_content.strip():
                cached = highlighted_cache.get(clean_content)
                if cached:
                    final_content = prefix + cached
                elif lexer and PYGMENTS_AVAILABLE:
                    try:
                        highlighted_line = highlight(
                            clean_content, lexer, HtmlFormatter(nowrap=True, noclasses=False, style="monokai")
                        )
                        final_content = prefix + highlighted_line
                    except Exception as exc:
                        logger.debug("Error applying syntax highlighting: %s", exc)
                        final_content = _escape_html(content)

        row_class = f"diff-line diff-{line_type}"
        html_parts.append(
            f"""
                <tr class="{row_class}">
                    <td class="line-num old-line-num">{old_line_num}</td>
                    <td class="line-num new-line-num">{new_line_num}</td>
                    <td class="line-content">{final_content}</td>
                </tr>
            """
        )

    html_parts.append("</table>")
    html_parts.append("</div>")
    html_parts.append("</div>")
    return "".join(html_parts)


def render_simple_diff_html(parsed_diff: List[Dict], file_path: str) -> str:
    """Generate simplified diff HTML tables (no syntax highlighting)."""
    html_parts = []
    html_parts.append('<div class="unified-diff-container" data-view-mode="minimal">')

    line_additions = sum(1 for line in parsed_diff if line["type"] == "add")
    line_deletions = sum(1 for line in parsed_diff if line["type"] == "delete")
    html_parts.append(
        f"""
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="additions">+{line_additions}</span>
                    <span class="deletions">-{line_deletions}</span>
                    <span class="file-path">{os.path.basename(file_path)} (Large file - simplified view)</span>
                </div>
            </div>
        """
    )

    html_parts.append('<div class="diff-content">')
    html_parts.append('<table class="diff-table">')

    for line_info in parsed_diff:
        if line_info["type"] == "header":
            continue
        row_class = f'diff-line diff-{line_info["type"]}'
        html_parts.append(
            f"""
                <tr class="{row_class}">
                    <td class="line-num old-line-num">{line_info.get("old_line_num", "")}</td>
                    <td class="line-num new-line-num">{line_info.get("new_line_num", "")}</td>
                    <td class="line-content">{_escape_html(line_info.get("content", ""))}</td>
                </tr>
            """
        )

    html_parts.append("</table>")
    html_parts.append("</div>")
    html_parts.append("</div>")
    return "".join(html_parts)


def generate_fallback_diff_html(file_path: str) -> str:
    """Fallback view when diff can't be rendered."""
    return f"""
        <div class="unified-diff-container" data-view-mode="minimal">
            <div class="diff-stats">
                <div class="diff-stats-left">
                    <span class="file-path">{os.path.basename(file_path)} (Diff unavailable)</span>
                </div>
            </div>
            <div class="diff-content">
                <div style="padding: 2rem; text-align: center; color: var(--text-secondary);">
                    <i class="fas fa-exclamation-triangle" style="font-size: 2rem; margin-bottom: 1rem;"></i>
                    <p>Diff view unavailable for this file</p>
                    <p style="font-size: 0.9rem;">File may be too large or binary</p>
                </div>
            </div>
        </div>
    """


def _generate_simple_diff_html(original_content: str, modified_content: str, file_path: str) -> Dict[str, str]:
    """Render simplified HTML diff for large files or when syntax highlighting is unavailable."""
    diff_lines = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            modified_content.splitlines(keepends=True),
            fromfile=f"a/{os.path.basename(file_path)}",
            tofile=f"b/{os.path.basename(file_path)}",
            lineterm="",
            n=3,
        )
    )
    parsed = parse_unified_diff_simple(diff_lines)
    if len(parsed) > 500:
        parsed = parsed[:500]
        logger.info("Truncated large diff to 500 lines for %s", file_path)
    html = render_simple_diff_html(parsed, file_path)
    return {"minimal": html, "full": html}


def _get_pygments_lexer(file_path: str):
    if not PYGMENTS_AVAILABLE or not get_lexer_for_filename:  # type: ignore
        return None
    try:
        return get_lexer_for_filename(file_path)  # type: ignore
    except ClassNotFound:
        logger.debug("No Pygments lexer found for file: %s", file_path)
        return None
    except Exception as exc:
        logger.debug("Error getting Pygments lexer for %s: %s", file_path, exc)
        return None


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )

