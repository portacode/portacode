"""Retrieve the Cloudflare zone/domain for the current cert."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

TOKEN_BEGIN = "-----BEGIN ARGO TUNNEL TOKEN-----"
TOKEN_END = "-----END ARGO TUNNEL TOKEN-----"


def extract_token_json(cert_path: str) -> dict:
    try:
        with open(cert_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        raise RuntimeError(f"Failed to read cert file: {exc}") from exc

    in_token = False
    b64_parts = []
    for line in lines:
        if line.strip() == TOKEN_BEGIN:
            in_token = True
            continue
        if line.strip() == TOKEN_END:
            in_token = False
            break
        if in_token:
            b64_parts.append(line.strip())

    if not b64_parts:
        raise RuntimeError("No ARGO TUNNEL TOKEN block found in cert.")

    try:
        decoded = base64.b64decode("".join(b64_parts)).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Failed to decode ARGO TUNNEL TOKEN JSON.") from exc


def fetch_zone_name(zone_id: str, api_token: str) -> str:
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to query Cloudflare API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON response from Cloudflare API.") from exc

    if not data.get("success"):
        errors = data.get("errors") or []
        msg = errors[0].get("message") if errors else "Unknown API error."
        raise RuntimeError(f"Cloudflare API error: {msg}")

    result = data.get("result") or {}
    name = result.get("name")
    if not name:
        raise RuntimeError("Zone name missing in Cloudflare API response.")
    return name


def get_authenticated_domain(cert_path: str) -> str:
    token = extract_token_json(cert_path)
    zone_id = token.get("zoneID")
    api_token = token.get("apiToken")
    if not zone_id or not api_token:
        raise RuntimeError("zoneID or apiToken missing from cert token JSON.")
    return fetch_zone_name(zone_id, api_token)


__all__ = ["get_authenticated_domain", "extract_token_json"]
