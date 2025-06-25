from __future__ import annotations

import asyncio
import os
import sys
from multiprocessing import Process
from pathlib import Path
import signal

import click

from .data import get_pid_file, is_process_running
from .keypair import get_or_create_keypair, fingerprint_public_key
from .connection.client import ConnectionManager, run_until_interrupt

GATEWAY_URL = "wss://portacode.com/gateway"
GATEWAY_ENV = "PORTACODE_GATEWAY"


@click.group()
def cli() -> None:
    """Portacode command-line interface."""


@cli.command()
@click.option("--gateway", "gateway", "-g", help="Gateway websocket URL (overrides env/ default)")
@click.option("--detach", "detach", "-d", is_flag=True, help="Run connection in background")
def connect(gateway: str | None, detach: bool) -> None:  # noqa: D401 – Click callback
    """Connect this machine to Portacode gateway."""

    # 1. Ensure only a single connection per user
    pid_file = get_pid_file()
    if pid_file.exists():
        try:
            other_pid = int(pid_file.read_text())
        except ValueError:
            other_pid = None

        if other_pid and is_process_running(other_pid):
            click.echo(
                click.style(
                    f"Another portacode connection (PID {other_pid}) is active.", fg="yellow"
                )
            )
            if click.confirm("Terminate the existing connection?", default=False):
                _terminate_process(other_pid)
                pid_file.unlink(missing_ok=True)
            else:
                click.echo("Aborting.")
                sys.exit(1)
        else:
            # Stale pidfile
            pid_file.unlink(missing_ok=True)

    # Determine gateway URL
    target_gateway = gateway or os.getenv(GATEWAY_ENV) or GATEWAY_URL

    # 2. Load or create keypair
    keypair = get_or_create_keypair()
    fingerprint = fingerprint_public_key(keypair.public_key_pem)

    click.echo()
    click.echo(click.style("✔ Generated / loaded RSA keypair", fg="green"))

    click.echo()
    click.echo(click.style("Public key (copy & paste to your Portacode account):", bold=True))
    click.echo("-" * 60)
    click.echo(keypair.public_key_pem.decode())
    click.echo("-" * 60)
    click.echo(f"Fingerprint: {fingerprint}")
    click.echo()
    click.prompt("Press <enter> once the key is added", default="", show_default=False)

    # 3. Start connection manager
    if detach:
        click.echo("Establishing connection in the background…")
        p = Process(target=_run_connection_forever, args=(target_gateway, keypair, pid_file))
        p.daemon = False  # We want it to live beyond parent process on POSIX; on Windows it's anyway independent
        p.start()
        click.echo(click.style(f"Background process PID: {p.pid}", fg="green"))
        return

    # Foreground mode → run in current event-loop
    pid_file.write_text(str(os.getpid()))

    async def _main() -> None:
        mgr = ConnectionManager(target_gateway, keypair)
        await run_until_interrupt(mgr)

    try:
        asyncio.run(_main())
    finally:
        pid_file.unlink(missing_ok=True)


def _run_connection_forever(url: str, keypair, pid_file: Path):
    """Entry-point for detached background process."""
    try:
        pid_file.write_text(str(os.getpid()))

        async def _main() -> None:
            mgr = ConnectionManager(url, keypair)
            await run_until_interrupt(mgr)

        asyncio.run(_main())
    finally:
        pid_file.unlink(missing_ok=True)


def _terminate_process(pid: int):
    if sys.platform.startswith("win"):
        import ctypes
        PROCESS_TERMINATE = 1
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, -1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, signal.SIGTERM)  # type: ignore[name-defined]
        except OSError:
            pass 