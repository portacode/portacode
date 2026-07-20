from __future__ import annotations

from types import SimpleNamespace

import pytest

from portacode.codex_prepare import CodexPreparationError, _run


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
