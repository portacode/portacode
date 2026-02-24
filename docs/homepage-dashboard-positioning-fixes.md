# Portacode Homepage + New-User Dashboard Content Audit

## What the product can already do (from code)

- One-click template pages exist and route into dashboard deployment flow via `?portafile=...` (`server/portacode_django/devops/templates/cms/one_click_template.html:163`, `server/portacode_django/static/js/dashboard-legacy.js:1498`).
- Deployment supports template-driven infra + automation YAML (instructions, expose ports, retries/failure policy, success actions) (`server/portacode_django/static/js/dashboard-legacy.js:1551`, `server/portacode_django/data/README_AUTOMATION_YAML.md:1`).
- Hosted fallback exists (your demo Proxmox) plus user-owned host preference when available (`server/portacode_django/dashboard/views/device_container_views.py:840`).
- Hosted free-tier limits and 1-week expiration are enforced in backend (`server/portacode_django/dashboard/views/device_container_views.py:49`, `server/portacode_django/dashboard/views/device_container_views.py:939`).
- 24-hour expiry warning email + expired-device cleanup workers exist (`server/portacode_django/dashboard/email_utils.py:246`, `server/portacode_django/data/management/commands/notify_expiring_devices.py:47`, `server/portacode_django/data/management/commands/cleanup_expired_devices.py:31`).

## Ordered fixes (highest impact first)

1. Fix the expectation gap in one-click promise.
Current state: dashboard code explicitly says one-click prefill “does not auto-provision yet” (`server/portacode_django/static/js/dashboard-legacy.js:1503`).
Update needed: either enable true one-click auto-start after template click, or adjust all public copy to “one click to prefill and launch” until behavior matches promise.

2. Replace homepage primary CTA from “Get Preview Access” to “Start with One-Click Templates”.
Current state: hero CTA goes to signup (`server/portacode_django/templates/pages/home.html:924`).
Update needed: first CTA should send users directly to `/one-click-deployment-templates/`, with signup as secondary.

3. Reposition homepage headline from mobile-first ideology to outcome-first deployment value.
Current state: homepage title/tagline and mission copy center “Complete Freedom, Ultimate Power” and smartphone lifestyle framing (`server/portacode_django/templates/pages/home.html:4`, `server/portacode_django/templates/pages/home.html:953`).
Update needed: headline/subheadline should lead with “Deploy full stacks in minutes + browser IDE + CI/CD automation + alerts”.

4. Add explicit “2-path onboarding” copy on homepage and dashboard.
Path A: “Deploy from template now.”
Path B: “Connect your own Proxmox node for no-charge self-hosted deployments.”
Current state: this distinction is not clear in hero, nav, or empty state.

5. Rewrite new-user dashboard empty state to stop forcing CLI-first onboarding.
Current state: “No Devices Connected” instructs `pip install portacode` + `portacode connect` only (`server/portacode_django/static/js/components/dashboard-app.js:1232`).
Update needed: first block = one-click deployment templates, second block = connect existing device via CLI/pairing.

6. Rename dashboard framing from “Device Dashboard” to platform framing.
Current state: title/subtitle are device-centric (“Monitor and manage your connected devices”) (`server/portacode_django/templates/pages/dashboard.html:21`).
Update needed: reflect deployments, automation, and operations control plane.

7. Promote one-click templates in global navigation and footer.
Current state: no top-nav shortcut to one-click templates; footer emphasizes docs/login/signup (`server/portacode_django/templates/core/base.html:122`, `server/portacode_django/templates/core/base.html:232`).
Update needed: add prominent nav item: “Templates” or “Deploy”.

8. Clarify hosted limits + lifecycle at decision points with positive framing.
Current state: modal note says “demo cluster” + usage limits link (`server/portacode_django/templates/pages/dashboard.html:299`).
Update needed: “Hosted quickstart (7-day servers, reminder before cleanup)” + immediate “Use your own Proxmox for persistent/free-control-plane usage”.

9. Surface email-based async workflow as a core benefit.
Current state: completion/failure/expiry email infrastructure exists but is not prominent in product messaging (`server/portacode_django/dashboard/email_utils.py:204`, `server/portacode_django/dashboard/email_utils.py:246`).
Update needed: homepage + dashboard copy should promise “Launch and leave; we email you when ready / if action needed.”

10. Upgrade one-click templates index copy from generic to conversion copy.
Current state: “curated deployment templates… full YAML configuration” (`server/portacode_django/devops/templates/cms/one_click_index.html:84`).
Update needed: add value/outcome language (“WordPress, n8n, and more with web IDE + automation + notifications”).

11. Improve one-click template detail page CTA context.
Current state: button is “Deploy” with minimal hint (`server/portacode_django/devops/templates/cms/one_click_template.html:163`).
Update needed: show what happens after click (provisioning, automation, email notification, where it runs, and retention policy).

12. Make first-run dashboard actions explicit and ranked.
Current state: “Add Device” + “Pair Device” buttons are equal priority (`server/portacode_django/templates/pages/dashboard.html:27`).
Update needed: primary action should be “Deploy from Template”; secondary “Connect Existing Device”; tertiary “Manual Device”.

13. Replace abstract mission text with proof-based positioning blocks.
Current state: long philosophical copy in Vision/Mission (`server/portacode_django/templates/pages/home.html:955`).
Update needed: use concrete proof blocks: “What you can deploy”, “What happens automatically”, “What control you keep”.

14. Remove stale/unused milestone framing and align with real trust signals.
Current state: milestone counters/hooks are still in script/CSS but not delivering clear conversion value (`server/portacode_django/templates/pages/home.html:364`, `server/portacode_django/templates/pages/home.html:1143`).
Update needed: replace with trust metrics tied to deployments, completion time, success/failure notifications, and managed cleanup transparency.

15. Normalize terminology across pages to one narrative.
Current state: mixed language (“preview access”, “device dashboard”, “pairing”, “one-click deployment”) creates product identity drift.
Update needed: consistent hierarchy:
- Primary: “Deployment + Automation Control Plane”
- Secondary: “Remote IDE/terminal access”
- Optional advanced: “Pair external devices / self-hosted Proxmox”.

