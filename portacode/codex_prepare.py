"""Cross-platform preparation for Codex CLI through the local Portacode proxy."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping, Optional

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
CODEX_ENV_PATH = Path("/etc/portacode/codex.env")
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"


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
    """Use passwordless sudo when available before prompting interactively."""
    if platform.system().lower() not in {"linux", "darwin"} or not _sudo_prefix():
        return
    non_interactive = subprocess.run(
        ["sudo", "-n", "true"], text=True, capture_output=True
    )
    if non_interactive.returncode == 0:
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


def read_codex_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """Parse KEY=VALUE lines from the managed Codex env file."""
    path = path or CODEX_ENV_PATH
    values: Dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator and key:
                values[key] = value
    except OSError:
        pass
    return values


def apply_codex_env_to_mapping(
    env: MutableMapping[str, str],
    path: Optional[Path] = None,
) -> None:
    """Ensure OPENAI_API_KEY (and other managed vars) are present for child processes.

    Shell profiles / IDE terminals already source ``/etc/portacode/codex.env``.
    The long-running Portacode systemd service does not, so callers that spawn
    Codex (or interactive shells) must merge this file explicitly.
    """
    file_values = read_codex_env_file(path)
    for key, value in file_values.items():
        if value:
            env[key] = value
    # Always guarantee the local proxy sentinel unless a non-empty value exists.
    if not (env.get(OPENAI_API_KEY_ENV) or "").strip():
        env[OPENAI_API_KEY_ENV] = file_values.get(OPENAI_API_KEY_ENV) or LOCAL_SENTINEL


def build_codex_subprocess_env(
    base: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
) -> Dict[str, str]:
    """Environment for Codex CLI / app-server subprocesses."""
    env = dict(base or os.environ)
    apply_codex_env_to_mapping(env, path=path)
    return env


def _install_systemd_codex_environment_file() -> None:
    """Make Portacode's systemd unit load ``/etc/portacode/codex.env`` on start.

    systemd does not source ``/etc/profile.d`` or ``/etc/environment`` for
    ``Type=simple`` services the way login shells do. A drop-in EnvironmentFile
    is the reliable propagation path for the agent process itself.
    """
    dropin_body = (
        "# Managed by portacode prepare codex.\n"
        "[Service]\n"
        f"EnvironmentFile=-{CODEX_ENV_PATH}\n"
    )
    system_unit = Path("/etc/systemd/system/portacode.service")
    user_unit = Path.home() / ".config/systemd/user/portacode.service"

    if system_unit.exists():
        dropin_dir = Path("/etc/systemd/system/portacode.service.d")
        dropin_path = dropin_dir / "codex.conf"
        script = (
            f"install -d -m 755 {shlex.quote(str(dropin_dir))} && "
            f"printf '%s' {shlex.quote(dropin_body)} > {shlex.quote(str(dropin_path))} && "
            f"chmod 644 {shlex.quote(str(dropin_path))}"
        )
        _run([*_sudo_prefix(), "sh", "-c", script])
        subprocess.run([*_sudo_prefix(), "systemctl", "daemon-reload"], check=False)
        return

    if user_unit.exists():
        dropin_dir = Path.home() / ".config/systemd/user/portacode.service.d"
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin_path = dropin_dir / "codex.conf"
        dropin_path.write_text(dropin_body, encoding="utf-8")
        dropin_path.chmod(0o644)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def _set_local_sentinel() -> None:
    system = platform.system().lower()
    os.environ[OPENAI_API_KEY_ENV] = LOCAL_SENTINEL
    if system == "linux":
        # Portacode terminals load this directly, including sessions started by
        # a long-running agent that pre-dates the prepare command.
        managed_setup = (
            "install -d -m 755 /etc/portacode && "
            f"printf '%s\\n' '{OPENAI_API_KEY_ENV}={LOCAL_SENTINEL}' > {CODEX_ENV_PATH} && "
            f"chmod 644 {CODEX_ENV_PATH} && "
            "printf '%s\\n' '# Managed by portacode prepare codex.' "
            f"'if [ -r {CODEX_ENV_PATH} ]; then' '  set -a' "
            f"'  . {CODEX_ENV_PATH}' '  set +a' 'fi' > /etc/profile.d/portacode_codex.sh && "
            "chmod 644 /etc/profile.d/portacode_codex.sh"
        )
        _run([*_sudo_prefix(), "sh", "-c", managed_setup])
        _run([
            *_sudo_prefix(),
            "sh",
            "-c",
            f'grep -q "^{OPENAI_API_KEY_ENV}={LOCAL_SENTINEL}$" /etc/environment || '
            f'printf "%s\\n" "{OPENAI_API_KEY_ENV}={LOCAL_SENTINEL}" >> /etc/environment',
        ])
        _install_systemd_codex_environment_file()
    elif system == "darwin":
        zshenv = Path.home() / ".zshenv"
        line = f"export {OPENAI_API_KEY_ENV}={LOCAL_SENTINEL}\n"
        if not zshenv.exists() or line not in zshenv.read_text(encoding="utf-8", errors="ignore"):
            with zshenv.open("a", encoding="utf-8") as handle:
                handle.write(line)
        subprocess.run(["launchctl", "setenv", OPENAI_API_KEY_ENV, LOCAL_SENTINEL], check=False)
    elif system == "windows":
        _run(["setx", OPENAI_API_KEY_ENV, LOCAL_SENTINEL])


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
