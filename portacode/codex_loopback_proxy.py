"""Loopback-only Codex Responses proxy authenticated by the device key.

Runs on a dedicated thread + event loop so long-lived SSE streams and crypto
never starve the Portacode websocket / terminal asyncio loop.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Dict, Optional, Tuple

import httpx

from .codex_usage_limit import note_usage_limit_resets_at
from .keypair import KeyPair

LOGGER = logging.getLogger(__name__)

CODEX_LOOPBACK_HOST = "127.0.0.1"
CODEX_LOOPBACK_PORT = 61789
DEFAULT_GATEWAY_URL = "https://codexapi.portacode.com/v1"
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 256 * 1024
ALLOWED_PATHS = {"/health", "/v1/models", "/v1/responses"}
MAX_CONCURRENT_UPSTREAM = 8

# Streaming read can idle between SSE chunks; connect/write stay bounded.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0)

_TOOL_OUTPUT_TYPES = frozenset(
    {
        "function_call_output",
        "computer_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "shell_call_output",
        "command_execution_output",
    }
)


def _truncate_utf8(value: str, limit: int = MAX_TOOL_OUTPUT_BYTES) -> tuple[str, int]:
    """Keep useful head/tail context while enforcing a UTF-8 byte ceiling."""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, 0
    marker_budget = 160
    content_budget = max(limit - marker_budget, 0)
    head_size = content_budget * 3 // 4
    tail_size = content_budget - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    marker = (
        f"\n\n[Portacode truncated {omitted} bytes of oversized tool output. "
        "Narrow the command or inspect a specific file.]\n\n"
    )
    result = f"{head}{marker}{tail}"
    # The dynamic marker can exceed marker_budget by a few bytes.
    while len(result.encode("utf-8")) > limit and head:
        head = head[:-128]
        result = f"{head}{marker}{tail}"
    return result, omitted


def sanitize_responses_request(body: bytes) -> tuple[bytes, int, int]:
    """Crop only recognized Responses API tool-result fields.

    Unknown JSON shapes pass through untouched so Codex/API upgrades cannot be
    silently corrupted. Returns (body, fields_changed, bytes_omitted).
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, 0, 0
    if not isinstance(payload, dict) or not isinstance(payload.get("input"), list):
        return body, 0, 0

    changed = 0
    omitted_total = 0
    for item in payload["input"]:
        if not isinstance(item, dict) or item.get("type") not in _TOOL_OUTPUT_TYPES:
            continue
        for key in ("output", "content"):
            value = item.get(key)
            if isinstance(value, str):
                cropped, omitted = _truncate_utf8(value)
                if omitted:
                    item[key] = cropped
                    changed += 1
                    omitted_total += omitted
            elif isinstance(value, list):
                for part in value:
                    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                        continue
                    cropped, omitted = _truncate_utf8(part["text"])
                    if omitted:
                        part["text"] = cropped
                        changed += 1
                        omitted_total += omitted
    if not changed:
        return body, 0, 0
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), changed, omitted_total

FORWARD_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "cache-control",
        "x-codex-active-limit",
        "x-codex-primary-reset-at",
        "x-codex-secondary-reset-at",
        "x-codex-primary-used-percent",
        "x-codex-secondary-used-percent",
        "x-codex-primary-window-minutes",
        "x-codex-secondary-window-minutes",
        "x-codex-promo-message",
        "x-codex-credits-available",
        "x-codex-credits-balance",
        "x-portacode-resets-at",
        "x-request-id",
        "cf-ray",
    }
)


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
        self._client: Optional[httpx.AsyncClient] = None
        self._upstream_sem: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._active_requests = 0
        self._active_lock = threading.Lock()

    async def start(self) -> None:
        """Start the proxy thread (idempotent). Awaitable from the agent loop."""
        if self._thread is not None and self._thread.is_alive() and self._server is not None:
            return

        ready: Future = Future()
        stop_event = threading.Event()
        self._stop_event = stop_event

        def _thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._async_start())
                ready.set_result(True)
                while not stop_event.is_set():
                    loop.run_forever()
                    if stop_event.is_set():
                        break
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    LOGGER.exception("Codex loopback proxy thread crashed")
            finally:
                try:
                    loop.run_until_complete(self._async_stop())
                except Exception:
                    LOGGER.exception("Codex loopback proxy cleanup failed")
                try:
                    loop.close()
                except Exception:
                    pass
                self._loop = None

        self._thread = threading.Thread(
            target=_thread_main,
            name="portacode-codex-loopback",
            daemon=True,
        )
        self._thread.start()
        await asyncio.wrap_future(ready)

    async def stop(self) -> None:
        thread = self._thread
        loop = self._loop
        stop_event = self._stop_event
        if thread is None:
            return
        if stop_event is not None:
            stop_event.set()
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        # Prefer to_thread when available; keep a 3.8-safe fallback.
        try:
            await asyncio.to_thread(thread.join, 5.0)
        except AttributeError:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, thread.join, 5.0)
        self._thread = None
        self._stop_event = None

    async def _async_start(self) -> None:
        if self._server is not None:
            return
        self._upstream_sem = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM)
        self._client = httpx.AsyncClient(
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=False,
            http2=False,
        )
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                host=CODEX_LOOPBACK_HOST,
                port=CODEX_LOOPBACK_PORT,
                limit=MAX_REQUEST_BYTES + 16 * 1024,
                reuse_address=True,
            )
        except OSError:
            LOGGER.exception(
                "Failed to bind Codex loopback proxy on %s:%s",
                CODEX_LOOPBACK_HOST,
                CODEX_LOOPBACK_PORT,
            )
            await self._async_stop()
            raise
        LOGGER.info(
            "Codex loopback proxy listening on http://%s:%s/v1 (isolated thread, gateway=%s)",
            CODEX_LOOPBACK_HOST,
            CODEX_LOOPBACK_PORT,
            self.gateway_url,
        )

    async def _async_stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                LOGGER.debug("wait_closed for loopback server failed", exc_info=True)
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                LOGGER.debug("Closing upstream httpx client failed", exc_info=True)

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

    def _bump_active(self, delta: int) -> int:
        with self._active_lock:
            self._active_requests = max(0, self._active_requests + delta)
            return self._active_requests

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        req_id = uuid.uuid4().hex[:10]
        peer = writer.get_extra_info("peername")
        started = time.monotonic()
        active = self._bump_active(1)
        method = path = "-"
        try:
            method, path, headers, body = await self._read_request(reader)
            LOGGER.info(
                "codex-proxy[%s] %s %s bytes=%s peer=%s active=%s",
                req_id,
                method,
                path,
                len(body),
                peer,
                active,
            )
            if method == "GET" and path == "/health":
                payload = {
                    "ok": True,
                    "service": "portacode-codex-loopback",
                    "active_requests": active,
                    "gateway": self.gateway_url,
                }
                await self._write_json(
                    writer, 200, json.dumps(payload, separators=(",", ":")).encode("utf-8")
                )
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
            await self._forward(writer, method, path, headers, body, req_id=req_id)
        except asyncio.TimeoutError:
            LOGGER.error(
                "codex-proxy[%s] timed out after %.1fs method=%s path=%s",
                req_id,
                time.monotonic() - started,
                method,
                path,
            )
            try:
                await self._write_error(writer, 504, "Portacode Codex proxy timed out talking to gateway")
            except Exception:
                pass
        except ValueError as exc:
            LOGGER.warning("codex-proxy[%s] bad request: %s", req_id, exc)
            try:
                await self._write_error(writer, 400, str(exc))
            except Exception:
                pass
        except Exception:
            LOGGER.exception(
                "codex-proxy[%s] request failed after %.1fs method=%s path=%s",
                req_id,
                time.monotonic() - started,
                method,
                path,
            )
            try:
                await self._write_error(writer, 502, "Portacode Codex proxy request failed")
            except Exception:
                pass
        finally:
            remaining = self._bump_active(-1)
            LOGGER.info(
                "codex-proxy[%s] done in %.1fs active=%s",
                req_id,
                time.monotonic() - started,
                remaining,
            )
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except Exception:
                pass

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> Tuple[str, str, Dict[str, str], bytes]:
        request_line = (await asyncio.wait_for(reader.readline(), timeout=30.0)).decode("latin-1").strip()
        parts = request_line.split(" ")
        if len(parts) != 3:
            raise ValueError("Malformed HTTP request")
        method, target, _version = parts
        path = target.split("?", 1)[0]
        headers: Dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
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
        body = (
            await asyncio.wait_for(reader.readexactly(length), timeout=60.0) if length else b""
        )
        return method.upper(), path, headers, body

    def _collect_response_headers(self, response: httpx.Response) -> Dict[str, str]:
        out: Dict[str, str] = {
            "Content-Type": response.headers.get("content-type", "application/json"),
            "Cache-Control": "no-store",
            "Connection": "close",
        }
        for name, value in response.headers.items():
            key = name.lower()
            if key in FORWARD_RESPONSE_HEADERS and key not in {"content-type", "cache-control"}:
                out[name] = value
        return out

    def _note_resets_from_error(self, response: httpx.Response, body: bytes) -> None:
        resets_at = None
        header_val = response.headers.get("x-portacode-resets-at") or response.headers.get(
            "x-codex-primary-reset-at"
        )
        if header_val:
            try:
                resets_at = int(header_val)
            except ValueError:
                resets_at = None
        if resets_at is None and body:
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                for key in ("resets_at", "resetsAt"):
                    if key in err:
                        try:
                            resets_at = int(err[key])
                            break
                        except (TypeError, ValueError):
                            pass
        note_usage_limit_resets_at(resets_at)

    async def _forward(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        request_headers: Dict[str, str],
        body: bytes,
        *,
        req_id: str,
    ) -> None:
        if method == "POST" and path == "/v1/responses" and body:
            body, cropped_fields, omitted_bytes = sanitize_responses_request(body)
            if cropped_fields:
                LOGGER.warning(
                    "codex-proxy[%s] cropped %s oversized tool result field(s), omitting %s bytes",
                    req_id,
                    cropped_fields,
                    omitted_bytes,
                )
        headers = self._signature_headers(method, path, body)
        content_type = request_headers.get("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        gateway_path = path[3:] if path.startswith("/v1") else path
        upstream_url = f"{self.gateway_url}/device{gateway_path}"
        client = self._client
        if client is None:
            raise RuntimeError("Upstream HTTP client is not started")
        sem = self._upstream_sem
        if sem is None:
            raise RuntimeError("Upstream semaphore is not started")

        async with sem:
            LOGGER.info(
                "codex-proxy[%s] upstream %s %s",
                req_id,
                method,
                upstream_url,
            )
            try:
                async with client.stream(
                    method,
                    upstream_url,
                    content=body,
                    headers=headers,
                ) as response:
                    response_headers = self._collect_response_headers(response)
                    status_text = response.reason_phrase or "OK"
                    LOGGER.info(
                        "codex-proxy[%s] upstream status=%s content-type=%s",
                        req_id,
                        response.status_code,
                        response.headers.get("content-type", ""),
                    )

                    if response.status_code >= 400:
                        err_body = await response.aread()
                        self._note_resets_from_error(response, err_body)
                        LOGGER.warning(
                            "codex-proxy[%s] upstream error status=%s body_prefix=%r",
                            req_id,
                            response.status_code,
                            err_body[:300],
                        )
                        response_headers["Content-Length"] = str(len(err_body))
                        writer.write(
                            f"HTTP/1.1 {response.status_code} {status_text}\r\n".encode("ascii")
                        )
                        for name, value in response_headers.items():
                            writer.write(f"{name}: {value}\r\n".encode("latin-1"))
                        writer.write(b"\r\n")
                        writer.write(err_body)
                        await writer.drain()
                        return

                    if response.headers.get("content-length"):
                        response_headers["Content-Length"] = response.headers["content-length"]
                    writer.write(
                        f"HTTP/1.1 {response.status_code} {status_text}\r\n".encode("ascii")
                    )
                    for name, value in response_headers.items():
                        writer.write(f"{name}: {value}\r\n".encode("latin-1"))
                    writer.write(b"\r\n")
                    bytes_out = 0
                    async for chunk in response.aiter_raw():
                        if not chunk:
                            continue
                        bytes_out += len(chunk)
                        writer.write(chunk)
                        await writer.drain()
                    LOGGER.info(
                        "codex-proxy[%s] streamed %s bytes to client",
                        req_id,
                        bytes_out,
                    )
            except httpx.TimeoutException as exc:
                LOGGER.error("codex-proxy[%s] upstream timeout: %s", req_id, exc)
                raise asyncio.TimeoutError(str(exc)) from exc
            except httpx.HTTPError as exc:
                LOGGER.error("codex-proxy[%s] upstream http error: %s", req_id, exc)
                raise

    async def _write_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = ('{"error":{"message":"' + message.replace('"', "'") + '"}}').encode("utf-8")
        await self._write_json(writer, status, body)

    async def _write_json(self, writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = "OK" if 200 <= status < 300 else "Error"
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                "Cache-Control: no-store\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()
