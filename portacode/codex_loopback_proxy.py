"""Loopback-only Codex Responses proxy authenticated by the device key."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from typing import Dict, Optional, Tuple

import httpx

from .keypair import KeyPair

LOGGER = logging.getLogger(__name__)

CODEX_LOOPBACK_HOST = "127.0.0.1"
CODEX_LOOPBACK_PORT = 61789
DEFAULT_GATEWAY_URL = "https://codexapi.portacode.com/v1"
MAX_REQUEST_BYTES = 10 * 1024 * 1024
ALLOWED_PATHS = {"/health", "/v1/models", "/v1/responses"}


class CodexLoopbackProxy:
    """Expose the narrow OpenAI-compatible surface that Codex CLI needs locally."""

    def __init__(self, keypair: KeyPair, gateway_url: Optional[str] = None) -> None:
        self.keypair = keypair
        self.gateway_url = (
            gateway_url
            or os.getenv("PORTACODE_CODEX_DEVICE_GATEWAY_URL")
            or DEFAULT_GATEWAY_URL
        ).rstrip("/")
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=CODEX_LOOPBACK_HOST,
            port=CODEX_LOOPBACK_PORT,
            limit=MAX_REQUEST_BYTES + 16 * 1024,
        )
        LOGGER.info(
            "Codex loopback proxy listening on http://%s:%s/v1",
            CODEX_LOOPBACK_HOST,
            CODEX_LOOPBACK_PORT,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    def _signature_headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode("ascii")
        signature = base64.b64encode(self.keypair.sign_bytes(canonical)).decode("ascii")
        return {
            "X-Portacode-Device-Key": self.keypair.public_key_der_b64(),
            "X-Portacode-Timestamp": timestamp,
            "X-Portacode-Nonce": nonce,
            "X-Portacode-Signature": signature,
        }

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path, headers, body = await self._read_request(reader)
            if method == "GET" and path == "/health":
                await self._write_json(writer, 200, b'{"ok":true,"service":"portacode-codex-loopback"}')
                return
            if method not in {"GET", "POST"} or path not in ALLOWED_PATHS:
                await self._write_error(writer, 404, "Unsupported local Codex endpoint")
                return
            if method == "GET" and path != "/v1/models":
                await self._write_error(writer, 405, "Method not allowed")
                return
            if method == "POST" and path != "/v1/responses":
                await self._write_error(writer, 405, "Method not allowed")
                return
            await self._forward(writer, method, path, headers, body)
        except ValueError as exc:
            await self._write_error(writer, 400, str(exc))
        except Exception:
            LOGGER.exception("Codex loopback proxy request failed")
            await self._write_error(writer, 502, "Portacode Codex proxy request failed")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> Tuple[str, str, Dict[str, str], bytes]:
        request_line = (await reader.readline()).decode("latin-1").strip()
        parts = request_line.split(" ")
        if len(parts) != 3:
            raise ValueError("Malformed HTTP request")
        method, target, _version = parts
        path = target.split("?", 1)[0]
        headers: Dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            name, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                raise ValueError("Malformed HTTP header")
            headers[name.strip().lower()] = value.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            raise ValueError("Chunked request bodies are not supported")
        length = int(headers.get("content-length", "0"))
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body exceeds the local Codex proxy limit")
        return method.upper(), path, headers, await reader.readexactly(length) if length else b""

    async def _forward(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        request_headers: Dict[str, str],
        body: bytes,
    ) -> None:
        headers = self._signature_headers(method, path, body)
        content_type = request_headers.get("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                method,
                f"{self.gateway_url}/device{path.removeprefix('/v1')}",
                content=body,
                headers=headers,
            ) as response:
                response_headers = {
                    "Content-Type": response.headers.get("content-type", "application/json"),
                    "Cache-Control": "no-store",
                    "Connection": "close",
                }
                if response.headers.get("content-length"):
                    response_headers["Content-Length"] = response.headers["content-length"]
                status_text = response.reason_phrase or "OK"
                writer.write(f"HTTP/1.1 {response.status_code} {status_text}\r\n".encode("ascii"))
                for name, value in response_headers.items():
                    writer.write(f"{name}: {value}\r\n".encode("latin-1"))
                writer.write(b"\r\n")
                async for chunk in response.aiter_raw():
                    writer.write(chunk)
                    await writer.drain()

    async def _write_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = ('{"error":{"message":"' + message.replace('"', "'") + '"}}').encode("utf-8")
        await self._write_json(writer, status, body)

    async def _write_json(self, writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        writer.write(
            (
                f"HTTP/1.1 {status} Error\r\n"
                "Content-Type: application/json\r\n"
                "Cache-Control: no-store\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()
