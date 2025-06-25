"""Portacode SDK & CLI.

This package exposes a top-level `cli` entry-point for interacting with the
Portacode gateway and also provides programmatic helpers for managing user
configuration and network connections.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("portacode")  # type: ignore[arg-type]
except PackageNotFoundError:  # pragma: no cover
    # Package is not installed – most likely running from source tree.
    __version__ = "0.0.0.dev0"

__all__: list[str] = ["__version__"] 