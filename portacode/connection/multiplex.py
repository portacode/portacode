from __future__ import annotations

import asyncio
import json
import logging
from asyncio import Queue
from typing import Any, Dict, Union

__all__ = ["Multiplexer", "Channel"]

logger = logging.getLogger(__name__)


class Channel:
    """Represents a virtual duplex channel over a single WebSocket connection."""

    def __init__(self, channel_id: Union[int, str], multiplexer: "Multiplexer"):
        self.id = channel_id
        self._mux = multiplexer
        self._incoming: "Queue[Any]" = asyncio.Queue()

    async def send(self, payload: Any) -> None:
        await self._mux._send_on_channel(self.id, payload)

    async def recv(self) -> Any:
        return await self._incoming.get()

    # Internal API
    async def _deliver(self, payload: Any) -> None:
        await self._incoming.put(payload)


class Multiplexer:
    """Very small message-based multiplexer.

    Messages exchanged over the WebSocket are JSON objects with two keys:

    * ``channel`` – integer or string channel id.
    * ``payload``  – arbitrary JSON-serialisable object.
    """

    def __init__(self, send_func):
        self._send_func = send_func  # async function (str) -> None
        self._channels: Dict[Union[int, str], Channel] = {}

    def get_channel(self, channel_id: Union[int, str]) -> Channel:
        if channel_id not in self._channels:
            self._channels[channel_id] = Channel(channel_id, self)
        return self._channels[channel_id]

    async def _send_on_channel(self, channel_id: Union[int, str], payload: Any) -> None:
        frame = json.dumps({"channel": channel_id, "payload": payload})
        await self._send_func(frame)

    async def on_raw_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            channel_id = data["channel"]  # Can be int or str now
            payload = data.get("payload")
        except (ValueError, KeyError) as exc:
            logger.warning("Discarding malformed frame: %s", exc)
            return

        channel = self.get_channel(channel_id)
        await channel._deliver(payload) 