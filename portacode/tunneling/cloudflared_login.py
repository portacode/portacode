"""Run cloudflared tunnel login in a PTY and capture the login URL."""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class LoginResult:
    login_url: Optional[str]
    exit_code: int
    cert_detected: bool
    timed_out: bool


def run_login(
    cert_path: str,
    timeout: Optional[int],
    on_url: Optional[Callable[[str], None]] = None,
) -> LoginResult:
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "login"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
        text=False,
    )
    os.close(slave_fd)

    captured_url: Optional[str] = None
    url_sent = False
    start = time.monotonic()
    cert_detected = False
    timed_out = False

    while True:
        rlist, _, _ = select.select([master_fd], [], [], 0.1)
        if rlist:
            data = os.read(master_fd, 4096)
            if not data:
                break
            chunk = data.decode(errors="replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if captured_url is None:
                match = URL_RE.search(chunk)
                if match:
                    captured_url = match.group(0).strip()
        if captured_url and not url_sent:
            if on_url:
                on_url(captured_url)
            url_sent = True
        if os.path.exists(cert_path):
            try:
                if os.path.getsize(cert_path) > 0:
                    cert_detected = True
                    if proc.poll() is None:
                        proc.terminate()
                    break
            except OSError:
                pass
        if timeout is not None and (time.monotonic() - start) > timeout:
            timed_out = True
            if proc.poll() is None:
                proc.terminate()
            break
        if proc.poll() is not None and not rlist:
            break

    try:
        os.close(master_fd)
    except OSError:
        pass

    return LoginResult(
        login_url=captured_url,
        exit_code=proc.wait(),
        cert_detected=cert_detected,
        timed_out=timed_out,
    )


__all__ = ["LoginResult", "run_login"]
