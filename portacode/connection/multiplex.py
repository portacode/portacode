from __future__ import annotations

import asyncio
import json
import logging
import time
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
        # Start timing the serialization and sending
        start_time = time.time()
        
        try:
            # Serialize the frame
            serialization_start = time.time()
            frame = json.dumps({"channel": channel_id, "payload": payload})
            serialization_time = time.time() - serialization_start
            
            # Calculate message size
            frame_size_bytes = len(frame.encode('utf-8'))
            frame_size_kb = frame_size_bytes / 1024
            
            # Log warnings for large messages
            if frame_size_kb > 500:  # Warn for messages > 500KB
                logger.warning("🚨 LARGE WEBSOCKET MESSAGE: %.1f KB on channel %s (event: %s)", 
                              frame_size_kb, channel_id, payload.get('event', 'unknown'))
                
                # Log additional details for very large messages
                if frame_size_kb > 1000:  # > 1MB
                    logger.warning("🚨 VERY LARGE MESSAGE: %.1f KB - This may cause connection drops!", frame_size_kb)
                    
                    # Try to identify what's making the message large
                    if isinstance(payload, dict):
                        large_fields = []
                        for key, value in payload.items():
                            if isinstance(value, (str, list, dict)):
                                field_size = len(json.dumps(value).encode('utf-8')) / 1024
                                if field_size > 100:  # Fields > 100KB
                                    large_fields.append(f"{key}: {field_size:.1f}KB")
                        if large_fields:
                            logger.warning("🚨 Large fields detected: %s", ", ".join(large_fields))
            
            elif frame_size_kb > 100:  # Info for messages > 100KB
                logger.info("📦 Large websocket message: %.1f KB on channel %s (event: %s)", 
                           frame_size_kb, channel_id, payload.get('event', 'unknown'))
            
            # Send the frame
            send_start = time.time()
            await self._send_func(frame)
            send_time = time.time() - send_start
            
            total_time = time.time() - start_time
            
            # Log performance metrics for large messages or slow operations
            if frame_size_kb > 50 or total_time > 0.1:  # Log for messages > 50KB or operations > 100ms
                logger.info("📊 WebSocket send performance: %.1f KB in %.3fs (serialize: %.3fs, send: %.3fs) - channel %s", 
                           frame_size_kb, total_time, serialization_time, send_time, channel_id)
            
            # Log detailed timing for very large messages
            if frame_size_kb > 200:
                logger.info("🔍 Detailed timing - Channel: %s, Event: %s, Size: %.1f KB, Total: %.3fs", 
                           channel_id, payload.get('event', 'unknown'), frame_size_kb, total_time)
                
        except Exception as e:
            total_time = time.time() - start_time
            logger.error("❌ Failed to send websocket message on channel %s after %.3fs: %s", 
                        channel_id, total_time, e)
            raise

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