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

from . import __version__
from .data import get_pid_file, is_process_running
from .keypair import get_or_create_keypair, fingerprint_public_key
from .connection.client import ConnectionManager, run_until_interrupt

GATEWAY_URL = "wss://device.portacode.com/gateway"
GATEWAY_ENV = "PORTACODE_GATEWAY"


@click.group()
@click.version_option(__version__, "-v", "--version", message="Portacode %(version)s")
def cli() -> None:
    """Portacode command-line interface."""


@cli.command()
@click.option("--gateway", "gateway", "-g", help="Gateway websocket URL (overrides env/ default)")
@click.option("--detach", "detach", "-d", is_flag=True, help="Run connection in background")
@click.option("--debug", "debug", is_flag=True, help="Enable debug logging")
@click.option("--non-interactive", "non_interactive", is_flag=True, envvar="PORTACODE_NON_INTERACTIVE", hidden=True,
              help="Skip interactive prompts (used by background service)")
def connect(gateway: str | None, detach: bool, debug: bool, non_interactive: bool) -> None:  # noqa: D401 – Click callback
    """Connect this machine to Portacode gateway."""

    # Set up debug logging if requested
    if debug:
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        click.echo(click.style("🔍 Debug logging enabled", fg="yellow"))

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
    if not non_interactive:
        # Show key generation status
        if getattr(keypair, '_is_new', False):
            click.echo()
            click.echo(click.style("✔ Generated new RSA keypair", fg="green"))
        else:
            click.echo()
            click.echo(click.style("✔ Loaded existing RSA keypair", fg="green"))
        
        # Show key location
        key_dir = getattr(keypair, '_key_dir', None)
        if key_dir:
            click.echo(click.style(f"  📁 Key files: {key_dir}", fg="bright_black"))

        click.echo()
        click.echo(click.style("🚀 Welcome to Portacode!", fg="bright_blue", bold=True))
        click.echo()
        click.echo(click.style("📱 Next steps:", fg="bright_cyan", bold=True))
        click.echo(click.style("  1. Visit ", fg="white") + click.style("https://remote.portacode.com", fg="bright_blue", underline=True))
        click.echo(click.style("  2. Create your free account or sign in", fg="white"))
        click.echo(click.style("  3. Add this device using the key below", fg="white"))
        click.echo()
        
        click.echo(click.style("🔑 Device Key:", fg="bright_yellow", bold=True))
        
        # Dynamic border based on key length for visual appeal
        max_width = 80  # Maximum width for terminal display
        if len(pubkey_b64) <= max_width - 4:
            # Single line display
            border_width = len(pubkey_b64) + 4
            top = "┌" + "─" * (border_width - 2) + "┐"
            middle = "│ " + pubkey_b64 + " │"
            bottom = "└" + "─" * (border_width - 2) + "┘"
        else:
            # Multi-line display
            line_width = max_width - 4  # Account for border
            lines = []
            for i in range(0, len(pubkey_b64), line_width):
                lines.append(pubkey_b64[i:i+line_width])
            
            top = "┌" + "─" * (max_width - 2) + "┐"
            bottom = "└" + "─" * (max_width - 2) + "┘"
            
            middle_lines = []
            for line in lines:
                padded_line = line.ljust(line_width)
                middle_lines.append("│ " + padded_line + " │")
            middle = "\n".join(middle_lines)
        
        click.echo(click.style(top, fg="bright_black"))
        click.echo(click.style(middle, fg="bright_white"))
        click.echo(click.style(bottom, fg="bright_black"))
        
        # Show clean key for manual copying
        click.echo()
        click.echo(click.style("📋 Copy this key (select all text between the lines):", fg="bright_cyan", bold=True))
        click.echo(click.style("─" * min(len(pubkey_b64), 80), fg="bright_black"))
        click.echo(click.style(pubkey_b64, fg="bright_white", bold=True))
        click.echo(click.style("─" * min(len(pubkey_b64), 80), fg="bright_black"))
        
        # Better clipboard handling with multiple fallback mechanisms
        clipboard_success = False
        clipboard_error = None
        
        try:
            pyperclip.copy(pubkey_b64)
            # Verify the copy actually worked
            if pyperclip.paste() == pubkey_b64:
                clipboard_success = True
            else:
                clipboard_error = "Copy verification failed"
        except Exception as e:
            clipboard_error = str(e)
        
        if clipboard_success:
            click.echo(click.style("✅ Copied to clipboard!", fg="bright_green"))
        else:
            click.echo(click.style("⚠️  Copy manually from above", fg="bright_yellow"))
            if clipboard_error and "could not find a copy/paste mechanism" in clipboard_error:
                click.echo(click.style("   💡 Try: sudo apt-get install xclip", fg="bright_black"))
        
        click.echo(click.style(f"   Fingerprint: {fingerprint}", fg="bright_black"))
        click.echo()
        
        click.echo(click.style("💡 Connection info:", fg="bright_magenta", bold=True))
        click.echo(click.style("   • This session: Connection active until terminal closes", fg="white"))
        click.echo(click.style("   • Permanent: Run ", fg="white") + click.style("portacode service install", fg="bright_cyan") + click.style(" for auto-connect", fg="white"))
        click.echo(click.style("   • More help: ", fg="white") + click.style("portacode service --help", fg="bright_cyan"))
        click.echo()
        
        click.prompt(click.style("Press Enter once device is added", fg="bright_green", bold=True), default="", show_default=False)

    # 3. Start connection manager
    if detach and not non_interactive:
        click.echo("Establishing connection in the background…")
        p = Process(target=_run_connection_forever, args=(target_gateway, keypair, pid_file))
        p.daemon = False  # We want it to live beyond parent process on POSIX; on Windows it's anyway independent
        p.start()
        click.echo(click.style(f"Background process PID: {p.pid}", fg="green"))
        return

    # Foreground mode → run in current event-loop
    if not detach:
        pid_file.write_text(str(os.getpid()))

    async def _main() -> None:
        mgr = ConnectionManager(target_gateway, keypair, debug=debug)
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
            mgr = ConnectionManager(url, keypair, debug=debug)
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
        mgr = ConnectionManager(target_gateway, keypair, debug=debug)
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


# ---------------------------------------------------------------------------
# Service sub-commands (install/uninstall/start/stop/status)
# ---------------------------------------------------------------------------


@cli.group()
def service() -> None:  # noqa: D401 – Click callback
    """Manage background *service* that auto-runs ``portacode connect`` on login."""


@service.command("install")
def service_install() -> None:  # noqa: D401
    """Install + enable the background service and start it now (Linux: system service only)."""
    from .service import get_manager

    mgr = get_manager(system_mode=True)
    click.echo(f"Installing Portacode system service…")
    if os.geteuid() != 0:
        click.echo(click.style("[sudo] You may be prompted for your password to install the system service.", fg="yellow"))
        click.echo(click.style("💡 For persistent connection, install system-wide: sudo pip install portacode --system", fg="bright_black"))
    try:
        mgr.install()
        st = mgr.status()
        if st in {"active", "running"}:
            click.echo(click.style("✔ Service installed and running", fg="green"))
        else:
            click.echo(click.style(f"⚠ Service installed but status: {st}", fg="yellow"))
            if hasattr(mgr, "log_path"):
                click.echo(f"Inspect log: {mgr.log_path}")
    except Exception as exc:
        click.echo(click.style(f"Failed: {exc}", fg="red"))
        if "No module named" in str(exc) or "command not found" in str(exc):
            click.echo(click.style("💡 Try installing system-wide: sudo pip install portacode --system", fg="bright_cyan"))


@service.command("uninstall")
def service_uninstall() -> None:  # noqa: D401
    """Stop + remove the background service (Linux: system service only)."""
    from .service import get_manager

    mgr = get_manager(system_mode=True)
    click.echo(f"Uninstalling Portacode system service…")
    try:
        mgr.uninstall()
        click.echo(click.style("✔ Service removed", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Failed: {exc}", fg="red"))


@service.command("start")
def service_start() -> None:  # noqa: D401
    """Start the service if installed (Linux: system service only)."""
    from .service import get_manager

    mgr = get_manager(system_mode=True)
    try:
        mgr.start()
        st = mgr.status()
        if st in {"active", "running"}:
            click.echo(click.style("Service started and running", fg="green"))
        else:
            click.echo(click.style(f"Service start issued but current status: {st}", fg="yellow"))
            if hasattr(mgr, "log_path"):
                click.echo(f"Inspect log: {mgr.log_path}")
    except Exception as exc:
        click.echo(click.style(f"Failed: {exc}", fg="red"))


@service.command("stop")
def service_stop() -> None:  # noqa: D401
    """Stop the service if running (Linux: system service only)."""
    from .service import get_manager

    mgr = get_manager(system_mode=True)
    try:
        mgr.stop()
        click.echo(click.style("Service stopped", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Failed: {exc}", fg="red"))


@service.command("status")
@click.option("--verbose", "verbose", "-v", is_flag=True, help="Show detailed service status info")
def service_status(verbose: bool) -> None:  # noqa: D401
    """Show current status (running/loaded). Pass -v for system output (Linux: system service only)."""
    from .service import get_manager

    mgr = get_manager(system_mode=True)
    try:
        st = mgr.status()
        color = "green" if st in {"active", "running"} else "red" if st in {"failed", "inactive"} else "yellow"
        click.echo(click.style(f"Service status: {st}", fg=color))
        if verbose and hasattr(mgr, "status_verbose"):
            click.echo("\n--- system output ---")
            click.echo(mgr.status_verbose())
    except Exception as exc:
        click.echo(click.style(f"Failed: {exc}", fg="red")) 