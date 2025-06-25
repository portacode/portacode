from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Optional

import websockets
from websockets import WebSocketClientProtocol

from ..keypair import KeyPair
from .multiplex import Multiplexer

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Maintain a persistent connection to the Portacode gateway.

    Parameters
    ----------
    gateway_url: str
        WebSocket URL, e.g. ``wss://portacode.com/gateway``
    keypair: KeyPair
        User's public/private keypair used for authentication.
    reconnect_delay: float
        Seconds to wait before attempting to reconnect after an unexpected drop.
    """

    def __init__(self, gateway_url: str, keypair: KeyPair, reconnect_delay: float = 5.0):
        self.gateway_url = gateway_url
        self.keypair = keypair
        self.reconnect_delay = reconnect_delay

        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

        self.websocket: Optional[WebSocketClientProtocol] = None
        self.mux: Optional[Multiplexer] = None

    async def start(self) -> None:
        """Start the background task that maintains the connection."""
        if self._task is not None:
            raise RuntimeError("Connection already running")
        self._task = asyncio.create_task(self._runner())

    async def stop(self) -> None:
        """Request graceful shutdown."""
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def _runner(self) -> None:
        while not self._stop_event.is_set():
            try:
                logger.info("Connecting to gateway at %s", self.gateway_url)
                async with websockets.connect(self.gateway_url) as ws:
                    self.websocket = ws
                    self.mux = Multiplexer(self.websocket.send)
                    await self._authenticate()
                    await self._listen()
            except (OSError, websockets.WebSocketException) as exc:
                logger.warning("Connection error: %s", exc)
            finally:
                if not self._stop_event.is_set():
                    logger.info("Reconnecting in %.1f seconds…", self.reconnect_delay)
                    await asyncio.sleep(self.reconnect_delay)

    async def _authenticate(self) -> None:
        """Send authentication frame containing the user's public key."""
        assert self.websocket is not None, "WebSocket not ready"
        await self.websocket.send(self.keypair.public_key_pem.decode())
        logger.info("Authentication frame sent; awaiting confirmation…")
        # For the moment we just wait for a confirmation message. This depends on
        # the actual server implementation. We'll assume the server replies with
        # a simple text message "ok".
        response = await self.websocket.recv()
        if response != "ok":  # naive check
            raise RuntimeError(f"Gateway rejected authentication: {response}")
        logger.info("Successfully authenticated with the gateway.")

    async def _listen(self) -> None:
        assert self.websocket is not None, "WebSocket not ready"
        async for message in self.websocket:
            if self.mux:
                await self.mux.on_raw_message(message)


async def run_until_interrupt(manager: ConnectionManager) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, lambda: asyncio.create_task(manager.stop()))

    await manager.start()
    # Wait until the manager stops
    await manager._task 