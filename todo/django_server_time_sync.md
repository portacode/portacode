# Django-Based Time Sync Plan

## Current Situation
- Browser clients rely on `ntpClock` (`portacode/static/js/utils/ntp-clock.js`) which fetches Cloudflare’s `/cdn-cgi/trace`; failures leave `offset === null`, causing `trace.client_send` to be `null`.
- Django (`server/portacode_django/dashboard/consumers.py`) and devices (`portacode/connection/handlers/base.py`) already use the Python `ntp_clock` helper, so the backend timeline is consistent even when browsers are unsynced.
- Trace propagation currently depends on the client providing `client_send`. When this is missing, subtracting timestamps raises `TypeError`, triggering the production crash described in `todo/issues/websocket_consumer_crash_on_certain_commands.md`.

## Goals
1. Remove the Cloudflare dependency for browsers/devices so that every participant derives time from the Django server.
2. Preserve the existing trace schema (`client_send`, `server_receive`, `device_receive`, etc.) so downstream tooling keeps working.
3. Keep latency low by reusing the existing WebSocket connection instead of introducing an HTTP polling endpoint.

## Option A – Passive Server Timestamps (Minimal Changes)
**Idea:** let the consumer stamp `trace.server_receive` and `trace.server_send` on *every* inbound/outbound message (even if the client didn’t request tracing). Clients/devices then infer their clock offset using those values.

- Pros: zero extra messages, piggybacks on normal traffic, compatible with firewalls.
- Cons: idle clients that don’t receive messages for long periods won’t refresh their offset; accuracy depends on round-trip variance.
- Implementation notes:
  - `receive_json` should `content.setdefault("trace", {})` and insert `server_receive=ntp_clock.now_ms()` before routing to devices.
  - `device_message` already writes `server_receive_response`/`server_send_response` when a trace exists; change it to always create the `trace` dict and log a flag when `ntp_clock` is unsynced.
  - Client/device SDKs watch for these fields, compute `offset = ((server_receive + server_send)/2) - client_receive`, and store a rolling average.
  - Send periodic heartbeat events if needed to keep offsets fresh.

## Option B – Explicit Time-Sync Messages (Deterministic)
**Idea:** add lightweight request/response pairs over the same WebSocket channel so clients/devices intentionally ask for a timestamp.

- Message sketch:
  - Client → Server: `{ "event": "time_sync.request", "sequence": N, "client_send": Date.now() }`
  - Server → Client: `{ "event": "time_sync.response", "sequence": N, "server_receive": now_ms, "server_send": now_ms }`
  - Devices mirror with `device_time_sync.*` events routed through the gateway.
- Pros: deterministic cadence, works even when no other traffic flows, can piggyback additional metadata (e.g., server-offset, sync health).
- Cons: added protocol surface, more handlers to maintain, slightly more bandwidth.
- Implementation notes:
  - Rate-limit sync requests (e.g., every 5 minutes or triggered when the client detects drift).
  - Consider exposing `recommended_interval_ms` so the server can throttle during load.
  - Include a `sync_warning` flag when the server’s own `ntp_clock` reports `is_synced == False`.

## Option C – Hybrid
Use passive timestamps for day-to-day alignment and fall back to explicit `time_sync.request` messages when the client notices that offsets are stale (e.g., no messages for ≥5 minutes or variance exceeds a threshold).

- Pros: combines low overhead with deterministic recovery.
- Cons: slightly more logic across all participants.

## Open Questions
1. **Persistence:** should the derived offset survive page reloads / device restarts (e.g., persisted in localStorage or config file)?
2. **Security:** is exposing absolute server time in every message acceptable, or should we obfuscate it when the session uses service tokens?
3. **Diagnostics:** do we need tooling to surface offset drift (e.g., Grafana panels, admin UI) to verify the switch?
4. **Backwards compatibility:** how will older clients/devices behave once the server starts injecting trace blocks by default?

## Suggested Next Steps (When We Revisit)
1. Harden the server trace handling (already partially done in “phase 1”) so it gracefully handles missing/invalid timestamps.
2. Prototype Option A by auto-stamping timestamps on every consumer hop and adding offset calculation to the dashboard JS (log offsets in the console for validation).
3. Evaluate whether idle sessions retain acceptable accuracy; if not, layer Option B on top.
4. Document the final protocol so device firmware and third-party clients can adopt it consistently.
