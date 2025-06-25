"""Networking primitives for Portacode.

The :pymod:`portacode.connection` package provides a resilient WebSocket client
with built-in multiplexer/demultiplexer for arbitrary virtual channels.
"""

from .client import ConnectionManager  # noqa: F401 