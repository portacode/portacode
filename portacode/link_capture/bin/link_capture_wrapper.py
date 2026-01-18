#!/usr/bin/env python3
"""Simple link capture wrapper that never executes a native browser."""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

LINK_CHANNEL_ENV = "PORTACODE_LINK_CHANNEL"
TERMINAL_ID_ENV = "PORTACODE_TERMINAL_ID"


def _find_link_argument(args):
    for arg in args:
        if not isinstance(arg, str):
            continue
        parsed = urlparse(arg)
        if parsed.scheme and parsed.netloc:
            return arg
        if arg.startswith("file://"):
            return arg
    return None


def _write_capture_event(cmd_name, args, link):
    channel = os.environ.get(LINK_CHANNEL_ENV)
    terminal_id = os.environ.get(TERMINAL_ID_ENV)
    if not channel or not link:
        return
    payload = {
        "terminal_id": terminal_id,
        "command": cmd_name,
        "args": args,
        "url": link,
        "timestamp": time.time(),
    }
    directory = Path(channel)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    temp_file = directory / f".{uuid.uuid4().hex}.tmp"
    term_label = terminal_id or "unknown"
    base_name = f"{int(time.time() * 1000)}-{term_label}"
    final_file = directory / f"{base_name}.json"
    suffix = 0
    while final_file.exists():
        suffix += 1
        final_file = directory / f"{base_name}-{suffix}.json"
    try:
        temp_file.write_text(json.dumps(payload), encoding="utf-8")
        temp_file.replace(final_file)
    except Exception:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("link_capture: missing target command name\n")
        sys.exit(1)
    cmd_name = sys.argv[1]
    cmd_args = sys.argv[2:]
    link = _find_link_argument(cmd_args)
    if link:
        _write_capture_event(cmd_name, cmd_args, link)
    # Never run a real browser; capture and exit successfully.
    sys.exit(0)


if __name__ == "__main__":
    main()
