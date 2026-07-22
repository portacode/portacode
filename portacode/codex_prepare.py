"""Cross-platform preparation for Codex CLI through the local Portacode proxy."""

from __future__ import annotations

import json
import logging
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
from typing import Callable, Dict, Iterable, Mapping, MutableMapping, Optional

from .codex_loopback_proxy import CODEX_LOOPBACK_HOST, CODEX_LOOPBACK_PORT

LOGGER = logging.getLogger(__name__)

# Do not pin `model` here — let the installed Codex CLI default apply, and let
# the Portacode chat UI override via thread/start when the device supports it.
CODEX_CONFIG = '''model_provider = "portacode_proxy"
approval_policy = "never"
sandbox_mode = "danger-full-access"
cli_auth_credentials_store = "file"

# Force all model traffic through the local Portacode device proxy.
# Newer Codex builds prefer WebSockets to api.openai.com; that bypasses our
# proxy and treats OPENAI_API_KEY=portacode-local as a real OpenAI key (401).
openai_base_url = "http://127.0.0.1:61789/v1"

[model_providers.portacode_proxy]
name = "Portacode Device Proxy"
base_url = "http://127.0.0.1:61789/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
supports_websockets = false
'''

LOCAL_SENTINEL = "portacode-local"
CODEX_ENV_PATH = Path("/etc/portacode/codex.env")
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"


class CodexPreparationError(RuntimeError):
    pass


def _run(command: Iterable[str], *, ok_returncodes: tuple[int, ...] = (0,)) -> None:
    """Run setup tools without letting their progress controls corrupt the PTY.

    ``ok_returncodes`` mirrors other Portacode install helpers (cloudflared,
    PyYAML, Proxmox infra): ``apt-get update`` often exits 100 on Proxmox hosts
    without an enterprise subscription even when Debian mirrors are fine.
    """
    result = subprocess.run(list(command), text=True, capture_output=True)
    if result.returncode not in ok_returncodes:
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
            # Exit 100: Proxmox enterprise.proxmox.com 401 without subscription.
            # Same tolerance as ensure_cloudflared / ensure_pyyaml / proxmox_infra.
            _run([*_sudo_prefix(), "apt-get", "update"], ok_returncodes=(0, 100))
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


def _extract_project_trust_blocks(existing: str) -> str:
    """Keep Codex ``[projects."..."] trust_level`` stanzas when rewriting config."""
    blocks: list[str] = []
    current: list[str] = []
    in_projects = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_projects and current:
                blocks.append("\n".join(current).rstrip())
            current = []
            in_projects = stripped.startswith("[projects.")
            if in_projects:
                current = [line]
            continue
        if in_projects:
            current.append(line)
    if in_projects and current:
        blocks.append("\n".join(current).rstrip())
    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks) + "\n"


def write_codex_config(codex_home: Optional[Path] = None) -> Path:
    """Write the managed Codex config into the shared CODEX_HOME.

    Always refreshes the Portacode proxy provider so runtime-user homes
    (e.g. ``/home/bishoy/.codex``) cannot keep a stale default that sends
    traffic to ``api.openai.com`` with the local sentinel key.
    Preserves existing ``[projects.*]`` trust entries.
    """
    home = Path(codex_home) if codex_home is not None else resolve_codex_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    existing = ""
    try:
        if config_path.is_file():
            existing = config_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    body = CODEX_CONFIG.rstrip() + "\n" + _extract_project_trust_blocks(existing)
    config_path.write_text(body, encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    try:
        from portacode.connection.handlers.runtime_user import (
            chown_path_if_possible,
            get_default_runtime_user,
        )

        owner = get_default_runtime_user()
        chown_path_if_possible(home, owner)
        chown_path_if_possible(config_path, owner)
    except Exception:
        pass
    return config_path


def _write_config() -> Path:
    """Backward-compatible alias used by ``prepare_codex``."""
    return write_codex_config()


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


def _runtime_user_home() -> Path:
    try:
        from portacode.connection.handlers.runtime_user import get_runtime_user_home

        return Path(get_runtime_user_home())
    except Exception:
        return Path.home()


def resolve_codex_home() -> Path:
    """Return the Codex state directory shared by app-server and interactive CLI.

    Web chat and ``codex resume`` must use the same store. When the Portacode
    agent runs as root it previously defaulted to ``/root/.codex`` while IDE
    terminals (as the workspace user) used ``$HOME/.codex`` — so the CLI could
    not see web-created chats.
    """
    preferred = _runtime_user_home() / ".codex"
    explicit = (os.environ.get("CODEX_HOME") or "").strip()
    if not explicit:
        return preferred

    path = Path(explicit).expanduser()
    # Remap stale root homes so CLI (runtime user) and app-server stay aligned.
    try:
        from portacode.connection.handlers.runtime_user import get_default_runtime_user

        runtime_user = get_default_runtime_user()
    except Exception:
        runtime_user = ""
    if (
        hasattr(os, "geteuid")
        and os.geteuid() == 0
        and runtime_user
        and runtime_user != "root"
        and (str(path) == "/root/.codex" or str(path).startswith("/root/.codex/"))
    ):
        return preferred
    return path


def _persist_codex_home_env(codex_home: Path) -> None:
    """Ensure interactive shells (profile.d) pick up the shared CODEX_HOME."""
    if platform.system().lower() != "linux":
        return
    path = Path(CODEX_ENV_PATH)
    desired = f"CODEX_HOME={codex_home}"
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return
    lines = existing.splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith("CODEX_HOME="):
            if not found:
                out.append(desired)
                found = True
            continue
        out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append(desired)
    body = "\n".join(out).rstrip() + "\n"
    if body == existing:
        return
    script = (
        f"install -d -m 755 /etc/portacode && "
        f"printf '%s' {shlex.quote(body)} > {shlex.quote(str(path))} && "
        f"chmod 644 {shlex.quote(str(path))}"
    )
    try:
        _run([*_sudo_prefix(), "sh", "-c", script])
    except Exception:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            path.chmod(0o644)
        except OSError:
            pass


def ensure_codex_home() -> Path:
    """Create CODEX_HOME and seed it from /root/.codex sessions when needed."""
    import shutil

    codex_home = resolve_codex_home()
    codex_home.mkdir(parents=True, exist_ok=True)
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    root_codex = Path("/root/.codex")
    root_sessions = root_codex / "sessions"
    seeded_paths: list[Path] = []
    if (
        hasattr(os, "geteuid")
        and os.geteuid() == 0
        and root_sessions.is_dir()
        and codex_home.resolve() != root_codex.resolve()
    ):
        for src in root_sessions.rglob("*.jsonl"):
            try:
                rel = src.relative_to(root_sessions)
            except ValueError:
                continue
            dest = sessions_dir / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                seeded_paths.append(dest)
                seeded_paths.append(dest.parent)
            except OSError:
                continue
        for name in ("config.toml", "auth.json"):
            src = root_codex / name
            dest = codex_home / name
            if src.is_file() and not dest.exists():
                try:
                    shutil.copy2(src, dest)
                    seeded_paths.append(dest)
                except OSError:
                    pass
        # Chown seeded files (copy2 preserves root ownership). Avoid a full-tree
        # walk on every spawn — that raced the initialize handshake.
        try:
            from portacode.connection.handlers.runtime_user import (
                chown_path_if_possible,
                get_default_runtime_user,
            )

            owner = get_default_runtime_user()
            chown_path_if_possible(codex_home, owner)
            chown_path_if_possible(sessions_dir, owner)
            for path in seeded_paths:
                chown_path_if_possible(path, owner)
            for extra in (codex_home / "tmp", codex_home / ".tmp"):
                if extra.exists():
                    chown_path_if_possible(extra, owner)
        except Exception:
            pass
    try:
        write_codex_config(codex_home)
    except Exception:
        LOGGER.debug("Could not refresh managed Codex config.toml", exc_info=True)
    try:
        _persist_codex_home_env(codex_home)
    except Exception:
        pass
    return codex_home


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
    # Keep interactive CLI and app-server on the same session store.
    if not (env.get("CODEX_HOME") or "").strip():
        env["CODEX_HOME"] = str(resolve_codex_home())


def build_codex_subprocess_env(
    base: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
) -> Dict[str, str]:
    """Environment for Codex CLI / app-server subprocesses."""
    env = dict(base or os.environ)
    apply_codex_env_to_mapping(env, path=path)
    try:
        env["CODEX_HOME"] = str(ensure_codex_home())
    except Exception:
        env.setdefault("CODEX_HOME", str(resolve_codex_home()))
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
        codex_home = str(resolve_codex_home())
        managed_setup = (
            "install -d -m 755 /etc/portacode && "
            f"printf '%s\\n' "
            f"'{OPENAI_API_KEY_ENV}={LOCAL_SENTINEL}' "
            f"'CODEX_HOME={codex_home}' "
            f"> {CODEX_ENV_PATH} && "
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


def prepare_codex(on_progress: Optional[Callable[[str], None]] = None) -> Path:
    """Install Codex and configure it to use the device-authenticated proxy.

    ``on_progress`` receives short human-readable step labels so UIs can show
    what the automatic setup is doing.
    """
    def progress(message: str) -> None:
        if on_progress:
            on_progress(message)

    progress("Checking administrator access…")
    _authorize_sudo_if_needed()
    progress("Installing Node.js if needed…")
    _install_node_if_needed()
    progress("Installing Codex CLI…")
    _install_codex()
    progress("Writing Codex configuration…")
    config_path = _write_config()
    progress("Configuring local API access…")
    _set_local_sentinel()
    progress("Verifying Portacode proxy…")
    _verify_loopback_proxy()
    return config_path
