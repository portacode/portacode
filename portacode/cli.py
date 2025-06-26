from __future__ import annotations

import asyncio
import os
import sys
from multiprocessing import Process
from pathlib import Path
import signal
import json

import click
import pyperclip

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

    pubkey_b64 = keypair.public_key_der_b64()
    click.echo()
    click.echo(click.style("✔ Generated / loaded RSA keypair", fg="green"))

    click.echo()
    click.echo(click.style("Public key (copy & paste to your Portacode account):", bold=True))
    click.echo("-" * 60)
    click.echo(pubkey_b64)
    click.echo("-" * 60)
    try:
        pyperclip.copy(pubkey_b64)
        click.echo(click.style("(Copied to clipboard!)", fg="cyan"))
    except Exception:
        click.echo(click.style("(Could not copy to clipboard. Please copy manually.)", fg="yellow"))
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


# ---------------------------------------------------------------------------
# Debug helpers – NOT intended for production use
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("message", nargs=1)
@click.option("--gateway", "gateway", "-g", help="Gateway websocket URL (overrides env/ default)")
def send_control(message: str, gateway: str | None) -> None:  # noqa: D401 – Click callback
    """Send a raw JSON *control* message on channel 0 and print replies.

    Example::

        portacode send-control '{"cmd": "system_info"}'

    The command opens a short-lived connection, authenticates, sends the
    control message and waits up to 5 seconds for responses which are then
    pretty-printed to stdout.
    """

    try:
        payload = json.loads(message)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Invalid JSON: {exc}") from exc

    target_gateway = gateway or os.getenv(GATEWAY_ENV) or GATEWAY_URL

    async def _run() -> None:
        keypair = get_or_create_keypair()
        mgr = ConnectionManager(target_gateway, keypair)
        await mgr.start()

        # Wait until mux is available & authenticated (rudimentary – 2s timeout)
        for _ in range(20):
            if mgr.mux is not None:
                break
            await asyncio.sleep(0.1)
        if mgr.mux is None:
            click.echo("Failed to initialise connection – aborting.")
            await mgr.stop()
            return

        # Send control frame on channel 0
        ctl = mgr.mux.get_channel(0)
        await ctl.send(payload)

        # Print replies for a short time
        try:
            with click.progressbar(length=50, label="Waiting for replies") as bar:
                for _ in range(50):
                    try:
                        reply = await asyncio.wait_for(ctl.recv(), timeout=0.1)
                        click.echo(click.style("< " + json.dumps(reply, indent=2), fg="cyan"))
                    except asyncio.TimeoutError:
                        pass
                    bar.update(1)
        finally:
            await mgr.stop()

    asyncio.run(_run()) 