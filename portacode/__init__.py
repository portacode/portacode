"""Portacode SDK & CLI.

This package exposes a top-level `cli` entry-point for interacting with the
Portacode gateway and also provides programmatic helpers for managing user
configuration and network connections.
"""

# NOTE: Use postponed evaluation of annotations to ensure type hints
# like `list[str]` are treated as strings on Python < 3.9. This allows
# the SDK/CLI to run on e.g. Ubuntu 20.04 (Python 3.8) without raising
# `TypeError: 'type' object is not subscriptable` at import time.
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("portacode")  # type: ignore[arg-type]
except PackageNotFoundError:  # pragma: no cover
    # Package is not installed – most likely running from source tree.
    __version__ = "0.0.0.dev0"

# Keep explicit type annotation while remaining compatible with Python < 3.9
# Thanks to the `annotations` future import above, the annotation is stored
# as a string and only evaluated by type checkers / at runtime on 3.9+.
__all__: list[str] = ["__version__"] 