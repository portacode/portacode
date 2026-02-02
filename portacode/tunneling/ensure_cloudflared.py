"""Ensure cloudflared is installed (Debian-based) and return its version."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

KEYRING_DIR = "/usr/share/keyrings"
KEYRING_PATH = f"{KEYRING_DIR}/cloudflare-main.gpg"
REPO_LIST_PATH = "/etc/apt/sources.list.d/cloudflared.list"
REPO_LINE = (
    "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
    "https://pkg.cloudflare.com/cloudflared any main\n"
)
GPG_URL = "https://pkg.cloudflare.com/cloudflare-main.gpg"

DEB_URLS = {
    "amd64": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb",
    "i386": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386.deb",
    "armhf": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm.deb",
    "arm64": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb",
}


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def apt_update() -> None:
    p = run_capture(["apt-get", "update"])
    if p.returncode == 0:
        return
    if p.returncode == 100:
        msg = (p.stderr or p.stdout or "").strip()
        print("Warning: `apt-get update` returned 100; continuing anyway.", file=sys.stderr)
        if msg:
            print(msg, file=sys.stderr)
        return
    raise subprocess.CalledProcessError(p.returncode, p.args, output=p.stdout, stderr=p.stderr)


def apt_install(pkgs: list[str]) -> None:
    run(["apt-get", "install", "-y"] + pkgs)


def ensure_prereqs() -> None:
    apt_update()
    # curl is the simplest/most reliable for fetching the key and deb
    apt_install(["ca-certificates", "curl"])


def refresh_cloudflare_key() -> None:
    os.makedirs(KEYRING_DIR, mode=0o755, exist_ok=True)
    # Overwrite key every run (key rollovers happen).
    run(["curl", "-fsSL", GPG_URL, "-o", KEYRING_PATH])
    os.chmod(KEYRING_PATH, 0o644)


def ensure_repo_line() -> None:
    # Overwrite repo file to keep it clean.
    with open(REPO_LIST_PATH, "w", encoding="utf-8") as f:
        f.write(REPO_LINE)


def dpkg_arch() -> str:
    p = run_capture(["dpkg", "--print-architecture"])
    if p.returncode != 0:
        raise RuntimeError("dpkg --print-architecture failed")
    return p.stdout.strip()


def install_via_apt_repo() -> None:
    refresh_cloudflare_key()
    ensure_repo_line()
    apt_update()
    apt_install(["cloudflared"])


def install_via_deb_fallback() -> None:
    arch = dpkg_arch()
    url = DEB_URLS.get(arch)
    if not url:
        raise RuntimeError(f"Unsupported architecture: {arch}")

    tmp_deb = f"/tmp/cloudflared-{arch}.deb"
    run(["curl", "-fL", url, "-o", tmp_deb])
    # dpkg may leave deps unresolved; fix with apt-get -f
    run(["dpkg", "-i", tmp_deb])
    apt_update()
    run(["apt-get", "-f", "install", "-y"])
    try:
        os.remove(tmp_deb)
    except OSError:
        pass


def ensure_cloudflared_installed() -> str:
    if have("cloudflared"):
        return subprocess.check_output(["cloudflared", "--version"], text=True).strip()

    if not is_root():
        raise RuntimeError("Run as root (sudo) to install cloudflared.")
    if not have("apt-get"):
        raise RuntimeError("apt-get not found (Debian-based expected).")

    ensure_prereqs()

    try:
        install_via_apt_repo()
    except subprocess.CalledProcessError as exc:
        print(f"Warning: apt-repo install failed ({exc}); trying .deb fallback.", file=sys.stderr)
        install_via_deb_fallback()

    return subprocess.check_output(["cloudflared", "--version"], text=True).strip()


__all__ = ["ensure_cloudflared_installed"]
