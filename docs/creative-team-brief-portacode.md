# Portacode Creative Brief (Product + Messaging)

## 1. What Portacode is today

Portacode is no longer just a mobile-first remote IDE tool.  
It is now a deployment + automation control plane with integrated remote access.

Core reality today:
- Users can deploy ready-made stacks from `one-click-deployment-templates/`.
- Users can manage those deployments from browser/mobile dashboard + terminal + IDE.
- Users can run YAML-defined automation tasks (steps, retries, expose ports, post-success actions).
- Users can still pair external devices with the CLI for remote access workflows.

## 2. The strongest value proposition

“Launch working infrastructure quickly, then operate it from one place.”

In practical terms:
- Pick template -> deploy environment
- Open IDE/terminal in browser/mobile
- Run automation tasks
- Get email notifications for key lifecycle events

## 3. Current onboarding paths (important for content)

### Path A (should be primary in content)
One-click deployment templates:
- URL: `/one-click-deployment-templates/`
- Template page “Deploy” sends user to dashboard with `?portafile=...`
- Current behavior: pre-fills deployment modal and config from YAML.

### Path B (secondary)
Bring existing machine/device:
- Install CLI: `pip install portacode`
- Connect: `portacode connect`
- Optional pairing code flow in dashboard.

## 4. Hosted quickstart model (must be communicated clearly)

When users deploy on Portacode-managed infra:
- Free-tier caps are enforced:
  - CPU cap: `2`
  - RAM cap: `8 GiB`
  - Disk cap: `30 GiB`
- Managed deployments get expiration timestamp of `1 week`.
- Expiry reminder email pipeline exists (default `24h` notice window).
- Expired instances are auto-cleaned by background worker.

This should be framed positively:
- “Fast hosted quickstart”
- “Transparent limits”
- “Bring your own Proxmox node for persistent ownership/control”

## 5. What Portacode can become (north-star positioning)

A lightweight CI/CD + operations platform for teams that want:
- Fast day-0 provisioning
- Day-2 automation/ops workflows
- Mobile/browser control without losing power-user capabilities
- Less tool switching between deploy, automate, monitor, and access

Suggested future-facing positioning line:
- “From one-click infra launch to ongoing CI/CD-style operations in one control plane.”

## 6. Key product capabilities to show visually

Design/content should emphasize:
- Template gallery (clear outcomes: WordPress, n8n, etc.)
- Deploy flow (fast start, guided setup)
- Browser/mobile dashboard (device/service visibility)
- Embedded terminal/IDE usage
- Automation YAML execution + step-by-step progress
- Port exposure/public route handling
- Lifecycle trust: limits, reminders, cleanup transparency

## 7. Current content gaps to avoid repeating

- Over-indexing on “mobile lifestyle” instead of deployment outcomes.
- CLI-first onboarding for all users (high friction for low-intent users).
- Hiding one-click templates behind signup-first CTAs.
- Ambiguous wording on what “one-click” does vs what still needs user confirmation.

## 8. Messaging guardrails

Always include:
- “One-click templates” as first discovery path
- “Hosted quickstart + limits” transparency
- “Bring your own Proxmox node” ownership option

Avoid:
- Promise of fully hands-off auto-provisioning unless product behavior matches.
- Vague slogans without concrete workflow steps.
- Positioning that makes Portacode sound only like remote terminal pairing software.

## 9. Link map for content/design team

Primary product links:
- Home: `https://portacode.com/`
- Templates index: `https://portacode.com/one-click-deployment-templates/`
- Dashboard: `https://portacode.com/dashboard/`
- Usage limits: `https://portacode.com/usage-limits/`

Supporting links:
- CLI package/docs entry: `https://pypi.org/project/portacode/`
- GitHub repo: `https://github.com/portacode/portacode`

## 10. Content framework to produce (recommended)

For homepage and onboarding content:
1. Hero:
   - “Deploy in minutes” headline
   - Primary CTA -> templates
2. How it works (3 steps):
   - Pick template
   - Deploy + configure
   - Operate via IDE/automation
3. Trust section:
   - Hosted limits + reminder + cleanup policy
   - BYO Proxmox option
4. Secondary path:
   - CLI pairing for existing devices
5. Proof section:
   - Screenshots + concrete outcomes

---

If you want, I can create a second file with ready-to-use copy blocks (hero variants, CTA sets, and 3-step sections) in different tones for ad creatives, landing pages, and in-app empty states.
