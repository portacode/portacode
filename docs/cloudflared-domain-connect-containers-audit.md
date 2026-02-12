# Cloudflared "Connect A Domain" Workflow Audit (Containers + Non-Root Service)

This document audits the current Cloudflared tunnel setup and forwarding-rule workflow in Portacode, focusing on why it works when Portacode runs as `root` but fails out-of-the-box in Proxmox containers created via `create_proxmox_container` (where the Portacode service runs as a non-root user).

## Current Workflow (As Implemented)

### 1) Tunnel Setup (`setup_cloudflare_tunnel`)

Entry point:

- `portacode/connection/handlers/cloudflare_tunnel.py` (`CloudflareTunnelSetupHandler`)

High-level steps:

1. Hard fail if not root.
   - `CloudflareTunnelSetupHandler.execute()` calls `_is_root()` and raises if false.
2. Ensure PyYAML (pip install if missing).
3. Reset prior tunnel state and remove default cert/config under the *current* `$HOME`.
   - Removes `~/.cloudflared/cert.pem` and `~/.cloudflared/config.yml` (via `portacode/tunneling/state.py` helpers).
4. Ensure `cloudflared` is installed.
   - `portacode/tunneling/ensure_cloudflared.py:ensure_cloudflared_installed()` installs via `apt-get` (root-only).
5. Run Cloudflare login and capture the authorization URL.
   - `portacode/tunneling/cloudflared_login.py:run_login(...)` runs `cloudflared tunnel login` in a PTY and watches for `cert.pem`.
6. Determine the authenticated zone/domain from the cert.
   - `portacode/tunneling/get_domain.py:get_authenticated_domain(...)`
7. Ensure the named tunnel exists and install Cloudflared as a service.
   - `portacode/tunneling/service_install.py:ensure_tunnel_and_service(...)`
   - Writes a minimal config and runs:
     - `cloudflared service uninstall`
     - `cloudflared service install`
     - `systemctl enable --now cloudflared`
8. Persist tunnel metadata in Portacode state.
   - `portacode/tunneling/state.py:update_state(...)` writes user-config state under `platformdirs.user_config_dir("portacode")`.

### 2) Forwarding Rules (`configure_cloudflare_forwarding` and related)

Entry points:

- `portacode/connection/handlers/cloudflare_forwarding.py` (`CloudflareForwardingHandler`)
- Container-specific helper:
  - `portacode/connection/handlers/cloudflare_forwarding.py:set_container_forwarding_rules(...)`

High-level steps:

1. Hard fail if not root.
2. Load tunnel state; validate it is configured.
3. Normalize rules and resolve any device-aware destinations like `http://[123]:8000`:
   - If a destination references a Proxmox-managed container, it queries Proxmox + dnsmasq leases to resolve the current container IP and emits an ingress `service: http(s)://<ip>:<port>`.
4. Write `cloudflared` config using `default_config_path()`:
   - `portacode/tunneling/state.py:default_config_path()` chooses:
     - `/etc/cloudflared/config.yml` if running as root
     - `~/.cloudflared/config.yml` otherwise
5. Route DNS for each hostname:
   - `cloudflared tunnel route dns <tunnel_name> <hostname>`
6. Reload cloudflared:
   - `/bin/systemctl reload cloudflared` (restart if reload fails)
7. Persist forwarding rules in Portacode forwarding state:
   - `portacode/tunneling/forwarding_state.py` (not shown here, but used by the handler)

## Why This Breaks In Proxmox Containers (Non-Root Portacode Service)

In Proxmox containers created via `create_proxmox_container`, Portacode is typically installed as a system service that runs as a non-root user (for Alpine/OpenRC this is explicit via `command_user="<user>"`).

Even though the container bootstrap grants that user passwordless sudo (`/etc/sudoers.d/portacode`), the Cloudflared handlers still hard-fail early because they check `os.geteuid() == 0` and refuse to proceed.

In other words:

- The system is currently "sudo-capable but root-gated".
- Several helpers also assume systemd and a root-owned `/etc/cloudflared/config.yml`.

## Top 3 Clean, Straightforward Adjustments To Make This Work In Containers (Non-Root Service)

These are ordered by "smallest conceptual change" and "least surprising behavior".

### 1) Replace "Must Be Root" Gates With "Privileged When Needed" Execution

Problem:

- `CloudflareTunnelSetupHandler` and `CloudflareForwardingHandler` error immediately when not root, even though the service user may have `sudo` rights.

Adjustment:

- Remove the hard `_is_root()` requirement from:
  - `portacode/connection/handlers/cloudflare_tunnel.py`
  - `portacode/connection/handlers/cloudflare_forwarding.py`
- Introduce a single helper used by these workflows:
  - `run_privileged([...])`:
    - If `geteuid()==0`, run directly.
    - Else run via `sudo -n ...` (non-interactive) and fail with a clear message if sudo is not available.

Important details to get right:

- Only elevate the operations that require it:
  - Package installation (`apt-get`, `apk`, etc.)
  - Writing to `/etc/...`
  - Service management (`systemctl`, `rc-service`)
- Keep "user-owned" Cloudflare artifacts (cert, credentials, config) in the service user's home directory unless you explicitly decide on a system-owned location.

Why this is clean:

- It keeps the Portacode service unprivileged by default.
- It allows the same codepath to work on hosts where Portacode runs non-root but has sudo (which is exactly the Proxmox container model you have today).

### 2) Make Cloudflared Service Management Init-System Aware (systemd vs OpenRC vs "none")

Problem:

- `portacode/tunneling/service_install.py` currently assumes:
  - `cloudflared service install` exists and is appropriate
  - `systemctl enable --now cloudflared` works
- This does not hold in minimal containers (especially Alpine/OpenRC) and in any environment without systemd.

Adjustment:

- Add an init abstraction similar to `portacode/service.py:get_manager()` but for cloudflared, with at least:
  - systemd (system service)
  - OpenRC (init.d service)
  - "no init" fallback: run cloudflared under Portacode's existing service supervision

Concrete behaviors:

- systemd present:
  - install/enable/restart `cloudflared` via `sudo` when needed
- OpenRC present:
  - install an init script and `rc-update add cloudflared default; rc-service cloudflared restart`
- no init system:
  - do not "install a service" at all; run `cloudflared tunnel run ...` as a subprocess managed by the Portacode service

Why this is clean:

- It removes a major source of "works on host, fails in container" behavior.
- It matches your existing approach for Portacode itself (`systemd` vs `OpenRC` logic already exists in `portacode/service.py`).

### 3) Make Cloudflared State/Paths Independent Of "Who Ran The Last sudo"

Problem:

- `default_cloudflared_dir()` and `default_config_path()` use `Path.home()` and `geteuid()`.
- If any part of the workflow is performed under `sudo` (or by root), you risk:
  - writing `cert.pem` / credentials / config under `/root/.cloudflared`
  - but running the daemon under a non-root user that expects `~/.cloudflared`
- Separately, `service_install.py:_cleanup_system_config()` unconditionally tries to remove `/etc/cloudflared/config.yml`, which is not safe for non-root mode.

Adjustment:

- Introduce an explicit "cloudflared base dir" concept owned by the Portacode service identity, and always pass it to `cloudflared` explicitly:
  - `--config <path>`
  - `--origincert <path>` (for login / domain selection flows)
  - and ensure the credentials file path is consistent

Practical options:

- Preferred (container-friendly): always keep cloudflared artifacts under the Portacode service user's home:
  - `~svcuser/.cloudflared/...`
  - Use `sudo` only for OS-level tasks (packages, init config).
- Alternative: standardize on a system location like `/etc/cloudflared` or `/var/lib/cloudflared`:
  - Requires careful permissions and is more invasive, but avoids per-user HOME issues.

Why this is clean:

- It makes the workflow deterministic across:
  - interactive PTY sessions
  - background services
  - occasional `sudo` usage
- It prevents "login succeeded but tunnel can't find cert/creds/config" class of failures.

## Quick Summary Of The Root Causes

- The handlers hard-require root even when the process has sudo capability.
- Service installation assumes systemd unconditionally.
- Cloudflared file locations are derived from the current process identity (`Path.home()` + `geteuid()`), which is fragile once you mix root + non-root execution.

