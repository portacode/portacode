from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from portacode.codex_prepare import (
    CodexPreparationError,
    _run,
    resolve_codex_home,
    write_codex_config,
)


def test_run_allows_proxmox_apt_update_exit_100(monkeypatch):
    """Same tolerance as ensure_cloudflared / ensure_pyyaml / proxmox_infra."""
    monkeypatch.setattr(
        "portacode.codex_prepare.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=100,
            stdout="",
            stderr="E: Failed to fetch https://enterprise.proxmox.com/debian/pve ... 401 Unauthorized",
        ),
    )
    _run(["apt-get", "update"], ok_returncodes=(0, 100))


def test_run_still_fails_on_unexpected_exit(monkeypatch):
    monkeypatch.setattr(
        "portacode.codex_prepare.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=100, stdout="", stderr="boom"),
    )
    with pytest.raises(CodexPreparationError, match="Command failed \\(100\\)"):
        _run(["apt-get", "update"])


def test_resolve_codex_home_remaps_root_when_runtime_user_differs(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/root/.codex")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_runtime_user_home",
        lambda message=None: "/home/bishoy",
    )
    assert resolve_codex_home() == Path("/home/bishoy/.codex")


def test_resolve_codex_home_keeps_explicit_non_root(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/custom/codex")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    assert resolve_codex_home() == Path("/custom/codex")


def test_write_codex_config_forces_local_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.chown_path_if_possible",
        lambda *args, **kwargs: None,
    )
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        '[projects."/home/bishoy/souldesign_container"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    path = write_codex_config(home)
    text = path.read_text(encoding="utf-8")
    assert 'model_provider = "portacode_proxy"' in text
    assert "127.0.0.1:61789" in text
    assert "supports_websockets = false" in text
    assert "openai_base_url" in text
    assert '[projects."/home/bishoy/souldesign_container"]' in text
    assert 'trust_level = "trusted"' in text
