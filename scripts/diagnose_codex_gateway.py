#!/usr/bin/env python3
"""Run inside a provisioned Codex container to diagnose gateway reachability.

Usage:
  python3 diagnose_codex_gateway.py
  python3 diagnose_codex_gateway.py --base-url https://codexapi.portacode.com/v1
  OPENAI_API_KEY=pcx_... python3 diagnose_codex_gateway.py

Prints only facts from each probe (no conclusions).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def kv(key: str, value: Any) -> None:
    print(f"{key}: {value}")


def load_env_from_bashrc() -> None:
    """Best-effort: pull OPENAI_API_KEY / PORTACODE_CODEX_BASE_URL from ~/.bashrc exports."""
    bashrc = Path.home() / ".bashrc"
    if not bashrc.is_file():
        return
    for line in bashrc.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export ") :]
        if "=" not in body:
            continue
        key, raw = body.split("=", 1)
        key = key.strip()
        if key not in {"OPENAI_API_KEY", "PORTACODE_CODEX_BASE_URL"}:
            continue
        if key in os.environ and os.environ[key]:
            continue
        val = raw.strip()
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            val = val[1:-1]
        os.environ[key] = val


def resolve_base_url(cli_value: str | None) -> str:
    candidates = [
        cli_value,
        os.environ.get("PORTACODE_CODEX_BASE_URL"),
        os.environ.get("CODEX_BASE_URL"),
    ]
    config = Path.home() / ".codex" / "config.toml"
    if config.is_file():
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("base_url"):
                # base_url = "https://..."
                parts = line.split("=", 1)
                if len(parts) == 2:
                    candidates.append(parts[1].strip().strip('"').strip("'"))
                break
    for item in candidates:
        if item and str(item).strip():
            return str(item).strip().rstrip("/")
    return "https://codexapi.portacode.com/v1"


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 15.0,
) -> None:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4096)
            kv("http_status", resp.status)
            kv("http_headers", dict(resp.headers.items()))
            kv("body_preview", raw[:800].decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        kv("http_status", exc.code)
        kv("http_headers", dict(exc.headers.items()) if exc.headers else {})
        kv("body_preview", raw[:800].decode("utf-8", errors="replace"))
        kv("error_type", "HTTPError")
    except Exception as exc:
        kv("error_type", type(exc).__name__)
        kv("error", repr(exc))
        kv("traceback", traceback.format_exc())


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Codex gateway connectivity from this container.")
    parser.add_argument("--base-url", default=None, help="Responses base URL ending with /v1")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    load_env_from_bashrc()
    base = resolve_base_url(args.base_url)
    token = (os.environ.get("OPENAI_API_KEY") or "").strip()
    parsed = urlparse(base if "://" in base else f"https://{base}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    section("1) Local environment facts")
    kv("cwd", os.getcwd())
    kv("HOME", os.environ.get("HOME"))
    kv("USER", os.environ.get("USER"))
    kv("hostname", socket.gethostname())
    try:
        kv("hostname_fqdn", socket.getfqdn())
    except Exception as exc:
        kv("hostname_fqdn_error", repr(exc))
    kv("python", sys.version.replace("\n", " "))
    kv("OPENAI_API_KEY_set", bool(token))
    kv("OPENAI_API_KEY_len", len(token))
    kv("OPENAI_API_KEY_prefix", token[:12] + "..." if len(token) > 12 else token)
    kv("PORTACODE_CODEX_BASE_URL_env", os.environ.get("PORTACODE_CODEX_BASE_URL"))
    kv("resolved_base_url", base)
    config = Path.home() / ".codex" / "config.toml"
    kv("codex_config_exists", config.is_file())
    if config.is_file():
        kv("codex_config_path", str(config))
        print("--- config.toml ---")
        print(config.read_text(encoding="utf-8", errors="replace"))

    section("1b) /etc/hosts and resolv.conf")
    for path in (Path("/etc/hosts"), Path("/etc/resolv.conf")):
        kv(f"{path}_exists", path.is_file())
        if path.is_file():
            print(f"--- {path} ---")
            print(path.read_text(encoding="utf-8", errors="replace"))

    section("2) DNS resolution")
    kv("host", host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = sorted({item[4][0] for item in infos})
        kv("addresses", addrs)
        loopback = [a for a in addrs if a.startswith("127.")]
        kv("resolves_to_loopback", bool(loopback))
        for addr in addrs:
            try:
                rev = socket.gethostbyaddr(addr)
                kv(f"reverse_{addr}", rev)
            except Exception as exc:
                kv(f"reverse_{addr}_error", repr(exc))
    except Exception as exc:
        kv("dns_error_type", type(exc).__name__)
        kv("dns_error", repr(exc))
        kv("traceback", traceback.format_exc())

    section("3) TCP connect")
    kv("target", f"{host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=args.timeout) as sock:
            kv("tcp_connected", True)
            kv("local_addr", sock.getsockname())
            kv("remote_addr", sock.getpeername())
    except Exception as exc:
        kv("tcp_connected", False)
        kv("error_type", type(exc).__name__)
        kv("error", repr(exc))
        kv("traceback", traceback.format_exc())

    if parsed.scheme == "https":
        section("4) TLS handshake")
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=args.timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    kv("tls_connected", True)
                    kv("tls_version", ssock.version())
                    kv("cipher", ssock.cipher())
                    cert = ssock.getpeercert()
                    kv("peer_cert_subject", cert.get("subject") if cert else None)
                    kv("peer_cert_san", cert.get("subjectAltName") if cert else None)
        except Exception as exc:
            kv("tls_connected", False)
            kv("error_type", type(exc).__name__)
            kv("error", repr(exc))
            kv("traceback", traceback.format_exc())

    health_url = f"{parsed.scheme}://{host}"
    if parsed.port:
        health_url += f":{parsed.port}"
    health_url += "/health"
    # Also try base without /v1 + /health if base ends with /v1
    health_candidates = [health_url]
    if base.endswith("/v1"):
        health_candidates.insert(0, base[: -len("/v1")] + "/health")

    section("5) HTTP GET /health")
    for url in dict.fromkeys(health_candidates):
        print(f"--- GET {url} ---")
        http_request(url, timeout=args.timeout)

    section("6) HTTP GET {base}/models (with Bearer if present)")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http_request(f"{base}/models", headers=headers, timeout=args.timeout)

    section("7) HTTP POST {base}/responses (minimal body, with Bearer if present)")
    payload = {
        "model": "gpt-5.4",
        "input": "Reply with exactly: PING_OK",
        "store": False,
    }
    body = json.dumps(payload).encode("utf-8")
    post_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        post_headers["Authorization"] = f"Bearer {token}"
    http_request(
        f"{base}/responses",
        method="POST",
        headers=post_headers,
        body=body,
        timeout=max(args.timeout, 60.0),
    )

    section("8) Raw outbound IP check (if possible)")
    for url in (
        "https://ifconfig.me/ip",
        "https://api.ipify.org",
    ):
        print(f"--- GET {url} ---")
        http_request(url, timeout=args.timeout)

    section("Done")
    print("Copy/paste this entire output back for analysis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
