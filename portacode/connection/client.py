from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Optional
import json
import base64
import sys

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
        WebSocket URL, e.g. ``wss://device.portacode.com/gateway``
    keypair: KeyPair
        User's public/private keypair used for authentication.
    reconnect_delay: float
        Seconds to wait before attempting to reconnect after an unexpected drop.
    max_retries: int, optional
        Deprecated. The connection manager now retries indefinitely for 
        recoverable errors and exits cleanly for fatal errors to allow 
        service manager restart.
    """

    def __init__(self, gateway_url: str, keypair: KeyPair, reconnect_delay: float = 1.0, max_retries: int = None, debug: bool = False):
        self.gateway_url = gateway_url
        self.keypair = keypair
        self.reconnect_delay = reconnect_delay
        self.debug = debug
        # max_retries is now deprecated but kept for backwards compatibility
        self.max_retries = max_retries

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
        attempt = 0
        while not self._stop_event.is_set():
            try:
                if attempt:
                    delay = min(self.reconnect_delay * 2 ** (attempt - 1), 30)
                    logger.warning("Reconnecting in %.1f s (attempt %d)…", delay, attempt)
                    await asyncio.sleep(delay)
                logger.info("Connecting to gateway at %s", self.gateway_url)
                async with websockets.connect(self.gateway_url) as ws:
                    # Reset attempt counter after successful connection
                    attempt = 0

                    self.websocket = ws
                    self.mux = Multiplexer(self.websocket.send)

                    # Authenticate – abort loop on auth failures
                    await self._authenticate()

                    # ------------------------------------------------------------------
                    # Initialise or re-attach terminal/control management (channel 0)
                    # ------------------------------------------------------------------
                    try:
                        from .terminal import TerminalManager  # local import to avoid heavy deps on startup
                        if getattr(self, "_terminal_manager", None):
                            self._terminal_manager.attach_mux(self.mux)
                        else:
                            self._terminal_manager = TerminalManager(self.mux, debug=self.debug)  # noqa: pylint=attribute-defined-outside-init
                    except Exception as exc:
                        logger.warning("TerminalManager unavailable: %s", exc)

                    # Start main receive loop until closed or stop requested
                    await self._listen()
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                attempt += 1
                logger.warning("Connection error: %s", exc)
                # Remove the max_retries limit - keep trying indefinitely
                # The service manager (systemd) will handle any necessary restarts
            except Exception as exc:
                # For truly fatal errors (like authentication failures), 
                # log and exit cleanly so systemd can restart the service
                logger.exception("Fatal error in connection manager: %s", exc)
                # Exit cleanly to allow systemd restart
                sys.exit(1)

    async def _authenticate(self) -> None:
        """Challenge-response authentication with the gateway using base64 DER public key."""
        assert self.websocket is not None, "WebSocket not ready"
        # Step 1: Send public key as base64 DER
        await self.websocket.send(self.keypair.public_key_der_b64())
        # Step 2: Receive challenge or error
        response = await self.websocket.recv()
        try:
            data = json.loads(response)
            challenge = data["challenge"]
        except Exception:
            # Not a challenge, must be an error
            raise RuntimeError(f"Gateway rejected authentication: {response}")
        # Step 3: Sign challenge and send signature
        signature = self.keypair.sign_challenge(challenge)
        signature_b64 = base64.b64encode(signature).decode()
        await self.websocket.send(json.dumps({"signature": signature_b64}))
        # Step 4: Receive final status
        status = await self.websocket.recv()
        if status != "ok":
            raise RuntimeError(f"Gateway rejected authentication: {status}")
        # Print success message in green and show close instructions
        try:
            import click
            click.echo(click.style("Successfully authenticated with the gateway.", fg="green"))
            if sys.platform == "darwin":
                click.echo(click.style("Press Cmd+C to close the connection.", fg="cyan"))
            else:
                click.echo(click.style("Press Ctrl+C to close the connection.", fg="cyan"))
        except ImportError:
            print("Successfully authenticated with the gateway.")
            if sys.platform == "darwin":
                print("Press Cmd+C to close the connection.")
            else:
                print("Press Ctrl+C to close the connection.")

    async def _listen(self) -> None:
        assert self.websocket is not None, "WebSocket not ready"
        while not self._stop_event.is_set():
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
                if self.mux:
                    await self.mux.on_raw_message(message)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break
        # Exit listen loop, trigger closure
        try:
            await self.websocket.close()
        except Exception:
            pass


async def run_until_interrupt(manager: ConnectionManager) -> None:
    stop_event = asyncio.Event()

    def _stop(*_):
        # TODO: Add cleanup logic here (e.g., close sockets, remove PID files, flush logs)
        stop_event.set()

    # Register SIGTERM handler (works on Unix, ignored on Windows)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass  # Not available on some platforms

    # Register SIGINT handler (Ctrl+C)
    try:
        signal.signal(signal.SIGINT, _stop)
    except (AttributeError, ValueError):
        pass

    await manager.start()
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        # TODO: Add cleanup logic here (e.g., close sockets, remove PID files, flush logs)
        pass
    await manager.stop()
    # TODO: Add any final cleanup logic here (e.g., remove PID files, flush logs) 