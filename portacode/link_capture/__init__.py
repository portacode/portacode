"""Helpers for the link capture wrapper scripts."""

import shutil
import tempfile
from pathlib import Path
from typing import Optional

try:
    # Use the stdlib helpers when they are available (Python ≥3.9).
    from importlib.resources import as_file, files
except ImportError:  # pragma: no cover
    # Fall back to the backport for older Python 3.x runtimes (>=3.6).
    from importlib_resources import as_file, files

_LINK_CAPTURE_TEMP_DIR: Optional[Path] = None


def prepare_link_capture_bin() -> Optional[Path]:
    """Extract the packaged link capture wrappers into a temporary dir and return it."""
    global _LINK_CAPTURE_TEMP_DIR
    if _LINK_CAPTURE_TEMP_DIR:
        return _LINK_CAPTURE_TEMP_DIR

    bin_source = files(__package__) / "bin"
    if not bin_source.is_dir():
        return None

    temp_dir = Path(tempfile.mkdtemp(prefix="portacode-link-capture-"))
    for entry in bin_source.iterdir():
        if not entry.is_file():
            continue
        with as_file(entry) as file_path:
            dest = temp_dir / entry.name
            shutil.copyfile(file_path, dest)
            dest.chmod(dest.stat().st_mode | 0o111)

    _LINK_CAPTURE_TEMP_DIR = temp_dir
    return _LINK_CAPTURE_TEMP_DIR
