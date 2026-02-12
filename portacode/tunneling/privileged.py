"""Small helpers for privileged operations (root vs sudo -n).

We need to support:
- running as root (sudo may not be installed)
- running as non-root with passwordless sudo (containers bootstrapped with NOPASSWD)

These helpers intentionally prefer non-interactive sudo (`sudo -n`) so a service
never hangs on a password prompt.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def sudo_prefix(*, non_interactive: bool = True) -> Optional[list[str]]:
    if is_root():
        return []
    if not have("sudo"):
        return None
    return ["sudo", "-n"] if non_interactive else ["sudo"]


def run_checked(cmd: list[str], *, allow_sudo: bool = True) -> subprocess.CompletedProcess[str]:
    prefix: list[str] = []
    if allow_sudo and not is_root():
        sp = sudo_prefix(non_interactive=True)
        if sp is None:
            raise RuntimeError("Root privileges required but sudo is not available.")
        prefix = sp
    proc = subprocess.run([*prefix, *cmd], text=True, capture_output=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or f"Command failed: {' '.join(cmd)}"
        raise RuntimeError(msg)
    return proc


def run(cmd: list[str], *, allow_sudo: bool = True) -> subprocess.CompletedProcess[str]:
    prefix: list[str] = []
    if allow_sudo and not is_root():
        sp = sudo_prefix(non_interactive=True)
        if sp is None:
            return subprocess.run(cmd, text=True, capture_output=True)
        prefix = sp
    return subprocess.run([*prefix, *cmd], text=True, capture_output=True)


def ensure_dir(path: Path, *, mode: int = 0o755) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(mode)
        except OSError:
            pass
        return
    except PermissionError:
        pass

    run_checked(["mkdir", "-p", str(path)])
    # chmod best-effort; some dirs are expected to be root-owned 0755.
    try:
        run_checked(["chmod", format(mode, "o"), str(path)])
    except Exception:
        pass


def write_text(path: Path, content: str, *, mode: int = 0o644) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(mode)
        except OSError:
            pass
        return
    except PermissionError:
        pass

    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        if have("install"):
            run_checked(["install", "-m", format(mode, "o"), str(tmp_path), str(path)])
        else:
            run_checked(["cp", str(tmp_path), str(path)])
            run_checked(["chmod", format(mode, "o"), str(path)])
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def copy_file(src: Path, dest: Path, *, mode: Optional[int] = None) -> None:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dest.write_bytes(data)
        if mode is not None:
            try:
                dest.chmod(mode)
            except OSError:
                pass
        return
    except PermissionError:
        pass

    ensure_dir(dest.parent)
    if have("install"):
        install_args = ["install"]
        if mode is not None:
            install_args += ["-m", format(mode, "o")]
        install_args += [str(src), str(dest)]
        run_checked(install_args)
        return
    run_checked(["cp", str(src), str(dest)])
    if mode is not None:
        run_checked(["chmod", format(mode, "o"), str(dest)])


__all__ = [
    "is_root",
    "have",
    "sudo_prefix",
    "run",
    "run_checked",
    "ensure_dir",
    "write_text",
    "copy_file",
]
