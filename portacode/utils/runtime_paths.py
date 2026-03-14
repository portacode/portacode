"""Helpers for normalizing runtime-provided filesystem paths on the device."""

from __future__ import annotations

import os
import pwd
import re


_ENV_VAR_PATTERN = re.compile(r"\$(\w+)|\$\{([^}]+)\}")


def _resolve_runtime_user() -> pwd.struct_passwd | None:
    runtime_user = (os.environ.get("PORTACODE_DEFAULT_RUNTIME_USER") or "").strip()
    if not runtime_user:
        return None
    try:
        return pwd.getpwnam(runtime_user)
    except KeyError:
        home = "/root" if runtime_user == "root" else f"/home/{runtime_user}"
        return pwd.struct_passwd((runtime_user, "x", 0, 0, "", home, ""))


def _build_runtime_env() -> dict[str, str]:
    env = dict(os.environ)
    runtime_user = _resolve_runtime_user()
    if runtime_user is not None:
        env["HOME"] = runtime_user.pw_dir
        env["USER"] = runtime_user.pw_name
        env["LOGNAME"] = runtime_user.pw_name
    return env


def _expand_runtime_vars(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or ""
        return env.get(name, match.group(0))

    return _ENV_VAR_PATTERN.sub(replace, value)


def expand_runtime_path(path: str) -> str:
    """Expand shell-style user and env markers into an absolute device path."""
    expanded = path
    runtime_env = _build_runtime_env()
    runtime_home = runtime_env.get("HOME")
    if runtime_home and (expanded == "~" or expanded.startswith("~/")):
        expanded = runtime_home + expanded[1:]
    expanded = _expand_runtime_vars(expanded, runtime_env)
    expanded = os.path.expanduser(expanded)
    return os.path.abspath(expanded)
