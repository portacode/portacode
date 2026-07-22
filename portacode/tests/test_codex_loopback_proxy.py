"""Tests for the isolated Codex loopback proxy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from portacode.codex_loopback_proxy import (
    MAX_TOOL_OUTPUT_BYTES,
    CodexLoopbackProxy,
    sanitize_responses_request,
)


def test_sanitize_responses_request_crops_only_tool_results():
    huge = "x" * (MAX_TOOL_OUTPUT_BYTES + 50_000)
    body = json.dumps({
        "input": [
            {"type": "function_call_output", "call_id": "call-1", "output": huge},
            {"type": "message", "role": "user", "content": huge},
        ]
    }).encode()

    sanitized, changed, omitted = sanitize_responses_request(body)
    payload = json.loads(sanitized)

    assert changed == 1
    assert omitted > 0
    assert len(payload["input"][0]["output"].encode()) <= MAX_TOOL_OUTPUT_BYTES
    assert "Portacode truncated" in payload["input"][0]["output"]
    assert payload["input"][0]["call_id"] == "call-1"
    assert payload["input"][1]["content"] == huge


def test_sanitize_responses_request_leaves_unknown_shapes_byte_identical():
    body = b'{"input":[{"type":"future_output","output":"abc"}]}'
    assert sanitize_responses_request(body) == (body, 0, 0)
from portacode.keypair import KeyPair


def _write_keypair(tmp_path: Path) -> KeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_path = tmp_path / "id_portacode"
    pub_path = tmp_path / "id_portacode.pub"
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
    )
    return KeyPair(priv_path, pub_path)


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None, reason="OK"):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {"content-type": "text/event-stream"})
        self.reason_phrase = reason
        self._chunks = chunks or [b"data: hello\n\n"]

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_loopback_proxy_health_on_isolated_thread(tmp_path, monkeypatch):
    keypair = _write_keypair(tmp_path)
    # Bind an ephemeral port to avoid colliding with a live agent.
    import portacode.codex_loopback_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "CODEX_LOOPBACK_PORT", 0)

    proxy = CodexLoopbackProxy(keypair, gateway_url="https://example.test/v1")
    # start_server with port 0 needs us to discover the port — patch after start.
    await proxy.start()
    assert proxy._server is not None
    sockets = proxy._server.sockets or []
    assert sockets
    port = sockets[0].getsockname()[1]

    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "portacode-codex-loopback"
    assert "active_requests" in body

    await proxy.stop()
    assert proxy._thread is None or not proxy._thread.is_alive()


@pytest.mark.asyncio
async def test_loopback_proxy_forwards_and_logs_errors(tmp_path, monkeypatch):
    keypair = _write_keypair(tmp_path)
    import portacode.codex_loopback_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "CODEX_LOOPBACK_PORT", 0)

    proxy = CodexLoopbackProxy(keypair, gateway_url="https://example.test/v1")
    await proxy.start()
    port = proxy._server.sockets[0].getsockname()[1]

    fake = _FakeStreamResponse(
        status_code=429,
        headers={"content-type": "application/json", "x-portacode-resets-at": "1700000000"},
        chunks=[b'{"error":{"message":"quota"}}'],
        reason="Too Many Requests",
    )

    class _Client:
        def stream(self, method, url, content=None, headers=None):
            assert method == "POST"
            assert url.endswith("/device/responses")
            assert "X-Portacode-Signature" in headers
            return fake

        async def aclose(self):
            return None

    proxy._client = _Client()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://127.0.0.1:{port}/v1/responses",
            content=b'{"model":"gpt-5","input":"hi"}',
            headers={"content-type": "application/json"},
            timeout=5.0,
        )
    assert response.status_code == 429
    assert response.json()["error"]["message"] == "quota"
    await proxy.stop()


@pytest.mark.asyncio
async def test_keypair_caches_private_key(tmp_path):
    keypair = _write_keypair(tmp_path)
    first = keypair.sign_bytes(b"abc")
    # Corrupt the on-disk key after first load — cached key should still work.
    keypair.private_key_path.write_bytes(b"not-a-key")
    second = keypair.sign_bytes(b"abc")
    assert first == second
    assert keypair.public_key_der_b64()
