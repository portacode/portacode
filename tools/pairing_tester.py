#!/usr/bin/env python3
"""
Simple helper to exercise the pairing-device WebSocket endpoint from a dev shell.

Example usage:
    python tools/pairing_tester.py --code 9999 --url wss://portacode.com/ws/pairing/device/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import websockets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test pairing-device WebSocket flow.")
    parser.add_argument(
        "--url",
        default=os.getenv("PAIRING_DEVICE_URL", "ws://localhost:8000/ws/pairing/device/"),
        help="Pairing device WebSocket URL (default: ws://localhost:8000/ws/pairing/device/)",
    )
    parser.add_argument(
        "--code",
        required=True,
        help="4-digit pairing code to send",
    )
    parser.add_argument(
        "--device-name",
        default="CLI Pairing Tester",
        help="Device name to include in the request",
    )
    parser.add_argument(
        "--public-key",
        default="fake-public-key-for-testing",
        help="Base64 DER public key to send (testing placeholder by default)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for approval/rejection before exiting",
    )
    parser.add_argument(
        "--project-path",
        dest="project_paths",
        action="append",
        default=[],
        help="Project folder path to pre-register (repeat for multiple paths)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    print(f"Connecting to {args.url} …")
    async with websockets.connect(args.url) as ws:
        ready = await ws.recv()
        print(f"<- {ready}")

        payload = {
            "code": args.code,
            "device_name": args.device_name,
            "public_key": args.public_key,
        }
        if args.project_paths:
            payload["project_paths"] = args.project_paths
        print(f"-> {json.dumps(payload)}")
        await ws.send(json.dumps(payload))

        try:
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                print(f"<- {message}")
        except asyncio.TimeoutError:
            print("Timed out waiting for server response; closing connection.")
        except websockets.ConnectionClosedOK:
            print("Connection closed gracefully.")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted by user.")
    except Exception as exc:  # pragma: no cover - diagnostic helper
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
