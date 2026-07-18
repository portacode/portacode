"""Cross-platform preparation for Codex CLI through the local Portacode proxy."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from .codex_loopback_proxy import CODEX_LOOPBACK_HOST, CODEX_LOOPBACK_PORT

CODEX_CONFIG = '''model = "gpt-5.4"
model_provider = "portacode_proxy"
approval_policy = "never"
sandbox_mode = "danger-full-access"
cli_auth_credentials_store = "file"

[model_providers.portacode_proxy]
name = "Portacode Device Proxy"
base_url = "http://127.0.0.1:61789/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
'''

LOCAL_SENTINEL = "portacode-local"


class CodexPreparationError(RuntimeError):
    pass


def _run(command: Iterable[str]) -> None:
    """Run setup tools without letting their progress controls corrupt the PTY."""
    result = subprocess.run(list(command), text=True, capture_output=True)
    if result.returncode:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        detail = f"\n{output[-4000:]}" if output else ""
        raise CodexPreparationError(f"Command failed ({result.returncode}): {' '.join(command)}{detail}")


def _node_major() -> int:
    node = shutil.which("node")
    if not node:
        return 0
    result = subprocess.run([node, "--version"], capture_output=True, text=True)
    match = re.match(r"v?(\d+)", result.stdout.strip())
    return int(match.group(1)) if result.returncode == 0 and match else 0


def _codex_path() -> str | None:
    return shutil.which("codex") or shutil.which("codex.cmd")


def _sudo_prefix() -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if not shutil.which("sudo"):
        raise CodexPreparationError("Administrator privileges are required but sudo is unavailable.")
    return ["sudo"]


def _authorize_sudo_if_needed() -> None:
    """Prompt before captured installer output would otherwise hide sudo's prompt."""
    if platform.system().lower() not in {"linux", "darwin"} or not _sudo_prefix():
        return
    result = subprocess.run(["sudo", "-v"], text=True)
    if result.returncode:
        raise CodexPreparationError("Administrator authorization is required to prepare Codex.")


def _install_node_if_needed() -> None:
    if _node_major() >= 18:
        return
    system = platform.system().lower()
    if system == "linux":
        os_release = Path("/etc/os-release")
        release = os_release.read_text(encoding="utf-8", errors="ignore").lower() if os_release.exists() else ""
        if "alpine" in release:
            _run([*_sudo_prefix(), "apk", "add", "--no-cache", "nodejs", "npm"])
        elif any(name in release for name in ("debian", "ubuntu")):
            _run([*_sudo_prefix(), "apt-get", "update"])
            _run([*_sudo_prefix(), "apt-get", "install", "-y", "ca-certificates", "curl"])
            node_setup = "bash -" if not _sudo_prefix() else "sudo -E bash -"
            _run(["sh", "-c", f"curl -fsSL https://deb.nodesource.com/setup_22.x | {node_setup}"])
            _run([*_sudo_prefix(), "apt-get", "install", "-y", "nodejs"])
        else:
            raise CodexPreparationError("Unsupported Linux distribution. Install Node.js 18+ and run this command again.")
    elif system == "darwin":
        if not shutil.which("brew"):
            raise CodexPreparationError("Homebrew is required to install Node.js on macOS. Install Node.js 18+ first.")
        _run(["brew", "install", "node"])
    elif system == "windows":
        winget = shutil.which("winget")
        if not winget:
            raise CodexPreparationError("winget is required to install Node.js on Windows. Install Node.js 18+ first.")
        _run([
            winget,
            "install",
            "--id",
            "OpenJS.NodeJS.LTS",
            "--exact",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ])
    else:
        raise CodexPreparationError(f"Unsupported operating system: {platform.system()}")
    if _node_major() < 18:
        raise CodexPreparationError("Node.js installation completed but Node.js 18+ is not available in this session.")


def _install_codex() -> None:
    if _codex_path():
        return
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise CodexPreparationError("npm was not found after installing Node.js.")
    command = [npm, "install", "-g", "@openai/codex@latest"]
    if platform.system().lower() in {"linux", "darwin"}:
        command = [*_sudo_prefix(), *command]
    _run(command)
    if not _codex_path():
        raise CodexPreparationError("Codex CLI installation completed but codex is not on PATH.")


def _write_config() -> Path:
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(CODEX_CONFIG, encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def _set_local_sentinel() -> None:
    system = platform.system().lower()
    os.environ["OPENAI_API_KEY"] = LOCAL_SENTINEL
    if system == "linux":
        # Portacode terminals load this directly, including sessions started by
        # a long-running agent that pre-dates the prepare command.
        managed_setup = (
            "install -d -m 755 /etc/portacode && "
            "printf '%s\\n' 'OPENAI_API_KEY=portacode-local' > /etc/portacode/codex.env && "
            "chmod 644 /etc/portacode/codex.env && "
            "printf '%s\\n' '# Managed by portacode prepare codex.' "
            "'if [ -r /etc/portacode/codex.env ]; then' '  set -a' "
            "'  . /etc/portacode/codex.env' '  set +a' 'fi' > /etc/profile.d/portacode_codex.sh && "
            "chmod 644 /etc/profile.d/portacode_codex.sh"
        )
        _run([*_sudo_prefix(), "sh", "-c", managed_setup])
        _run([*_sudo_prefix(), "sh", "-c", 'grep -q "^OPENAI_API_KEY=portacode-local$" /etc/environment || printf "%s\\n" "OPENAI_API_KEY=portacode-local" >> /etc/environment'])
    elif system == "darwin":
        zshenv = Path.home() / ".zshenv"
        line = "export OPENAI_API_KEY=portacode-local\n"
        if not zshenv.exists() or line not in zshenv.read_text(encoding="utf-8", errors="ignore"):
            with zshenv.open("a", encoding="utf-8") as handle:
                handle.write(line)
        subprocess.run(["launchctl", "setenv", "OPENAI_API_KEY", LOCAL_SENTINEL], check=False)
    elif system == "windows":
        _run(["setx", "OPENAI_API_KEY", LOCAL_SENTINEL])


def _verify_loopback_proxy() -> None:
    url = f"http://{CODEX_LOOPBACK_HOST}:{CODEX_LOOPBACK_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        raise CodexPreparationError(
            "Portacode's local Codex proxy is not running. Restart the Portacode service, then retry."
        ) from exc
    if not payload.get("ok"):
        raise CodexPreparationError("Portacode's local Codex proxy returned an unhealthy response.")


def prepare_codex() -> Path:
    """Install Codex and configure it to use the device-authenticated proxy."""
    _authorize_sudo_if_needed()
    _install_node_if_needed()
    _install_codex()
    config_path = _write_config()
    _set_local_sentinel()
    _verify_loopback_proxy()
    return config_path
