from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from typing import Optional

import websockets

PAIRING_URL_ENV = "PORTACODE_PAIRING_URL"
DEFAULT_PAIRING_URL = "wss://portacode.com/ws/pairing/device/"


class PairingError(Exception):
    """Raised when pairing fails or is rejected."""


@dataclass
class PairingResult:
    status: str
    payload: dict


async def _pair_device(
    url: str,
    public_key_b64: str,
    pairing_code: str,
    device_name: str,
    project_paths: list[str] | None = None,
    timeout: float = 300.0,
) -> PairingResult:
    async with websockets.connect(url) as ws:
        # Initial ready/event
        try:
            ready = await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise PairingError("Gateway did not acknowledge pairing request") from exc

        try:
            data = json.loads(ready)
            if data.get("event") != "pairing_ready":
                raise PairingError(f"Unexpected response from pairing gateway: {ready}")
        except json.JSONDecodeError:
            raise PairingError(f"Unexpected response from pairing gateway: {ready}") from None

        payload = {
            "code": pairing_code,
            "device_name": device_name,
            "public_key": public_key_b64,
        }
        if project_paths:
            payload["project_paths"] = project_paths
        await ws.send(json.dumps(payload))

        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PairingError("Pairing timed out waiting for approval") from exc

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            if data.get("event") != "pairing_status":
                continue

            status = data.get("status")
            if status == "pending":
                continue
            if status == "approved":
                return PairingResult(status="approved", payload=data)
            reason = data.get("reason") or status or "unknown_error"
            raise PairingError(reason)


def pair_device_with_code(
    keypair,
    pairing_code: str,
    device_name: Optional[str] = None,
    project_paths: list[str] | None = None,
    *,
    timeout: float = 300.0,
) -> PairingResult:
    """Run the pairing workflow synchronously."""
    pairing_url = os.getenv(PAIRING_URL_ENV, DEFAULT_PAIRING_URL)
    normalized_url = pairing_url if pairing_url.startswith("ws") else f"wss://{pairing_url.lstrip('/')}"
    friendly_name = device_name or socket.gethostname() or "Portacode Device"

    public_key_b64 = keypair.public_key_der_b64()
    return asyncio.run(
        _pair_device(
            normalized_url,
            public_key_b64=public_key_b64,
            pairing_code=pairing_code,
            device_name=friendly_name,
            project_paths=project_paths,
            timeout=timeout,
        )
    )
