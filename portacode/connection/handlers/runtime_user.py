from __future__ import annotations

import os
import pwd
import shlex
import stat
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

DEFAULT_RUNTIME_USER_ENV = "PORTACODE_DEFAULT_RUNTIME_USER"


def get_default_runtime_user(message: Optional[dict] = None) -> str:
    if isinstance(message, dict):
        for key in ("run_as_user", "username"):
            value = str(message.get(key) or "").strip()
            if value:
                return value
    env_user = str(os.environ.get(DEFAULT_RUNTIME_USER_ENV) or "").strip()
    if env_user:
        return env_user
    return _current_username()


def get_runtime_user_home(message: Optional[dict] = None) -> str:
    user = get_default_runtime_user(message)
    if os.name == "nt":
        return str(Path.home())
    try:
        return pwd.getpwnam(user).pw_dir or _fallback_home_for_user(user)
    except KeyError:
        return _fallback_home_for_user(user)


def should_switch_user(user: str) -> bool:
    return bool(user and os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and user != "root")


def wrap_shell_command(
    command: str,
    user: str,
    shell: str = "/bin/sh",
    preserve_env_names: Optional[Sequence[str]] = None,
) -> str:
    if not should_switch_user(user):
        return command
    shell_path = _resolve_shell_for_user(user, shell)
    quoted_user = shlex.quote(user)
    quoted_shell = shlex.quote(shell_path)
    preserved = [
        str(name or "").strip()
        for name in (preserve_env_names or [])
        if str(name or "").strip()
    ]
    preserve_fragment = ""
    if preserved:
        preserve_fragment = f"--preserve-env={','.join(shlex.quote(name) for name in preserved)} "
    return f"sudo -H -i {preserve_fragment}-u {quoted_user} -- {quoted_shell} -lc {shlex.quote(command)}"


def wrap_argv_for_user(argv: Iterable[str], user: str, cwd: Optional[str] = None) -> List[str]:
    argv_list = list(argv)
    if not should_switch_user(user):
        return argv_list
    if not argv_list:
        return ["sudo", "-H", "-i", "-u", user, "--"]
    if not cwd:
        return ["sudo", "-H", "-i", "-u", user, "--", *argv_list]

    shell_path = _resolve_shell_for_user(user, argv_list[0])
    exec_command = " ".join(shlex.quote(arg) for arg in argv_list)
    command = f"cd {shlex.quote(cwd)} && exec {exec_command}"
    return ["sudo", "-H", "-i", "-u", user, "--", shell_path, "-lc", command]


def ensure_parent_dirs(path: str | Path, owner_user: Optional[str] = None) -> None:
    target = Path(path).parent
    missing: List[Path] = []
    current = target
    while current and not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    target.mkdir(parents=True, exist_ok=True)
    if owner_user:
        for created in reversed(missing):
            chown_path_if_possible(created, owner_user)


def write_text_preserve_metadata(path: str | Path, content: str, *, create_user: Optional[str] = None) -> int:
    target = Path(path)
    existed = target.exists()
    original_stat = target.stat() if existed else None
    ensure_parent_dirs(target, owner_user=create_user if not existed else None)

    with open(target, "w", encoding="utf-8") as file_obj:
        file_obj.write(content)

    if original_stat is not None:
        _restore_metadata(target, original_stat)
    elif create_user:
        chown_path_if_possible(target, create_user)

    return len(content.encode("utf-8"))


def mkdir_with_owner(path: str | Path, owner_user: Optional[str] = None) -> None:
    target = Path(path)
    ensure_parent_dirs(target, owner_user=owner_user)
    target.mkdir(parents=False, exist_ok=False)
    if owner_user:
        chown_path_if_possible(target, owner_user)


def chown_path_if_possible(path: str | Path, user: str) -> None:
    if os.name == "nt":
        return
    try:
        pw_entry = pwd.getpwnam(user)
    except KeyError:
        return
    try:
        os.chown(path, pw_entry.pw_uid, pw_entry.pw_gid)
    except PermissionError:
        return
    except OSError:
        return


def _restore_metadata(path: Path, original_stat: os.stat_result) -> None:
    try:
        os.chmod(path, stat.S_IMODE(original_stat.st_mode))
    except OSError:
        pass
    try:
        os.chown(path, original_stat.st_uid, original_stat.st_gid)
    except PermissionError:
        pass
    except OSError:
        pass


def _current_username() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return str(os.environ.get("USER") or "root")


def _fallback_home_for_user(user: str) -> str:
    return "/root" if user == "root" else f"/home/{user}"


def _resolve_shell_for_user(user: str, shell: str) -> str:
    if shell and shell != "/bin/sh":
        return shell
    try:
        candidate = pwd.getpwnam(user).pw_shell or ""
    except KeyError:
        candidate = ""
    candidate = candidate.strip()
    if candidate:
        return candidate
    return shell or "/bin/sh"
