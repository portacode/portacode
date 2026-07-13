# Device Health Alerting — Implementation Plan

Status: draft for review
Owner: TBD
Related research agent runs: [Codebase architecture](09a64206-a23c-4952-acf1-84c200c28711), [unicom/queue patterns](43eead86-e1c1-4c1e-b969-17e24d68d567)

## 1. Why, and what "good" looks like

Portacode's agent already collects CPU, RAM, and disk usage every 10s and knows connect/disconnect events in real time. None of this is used proactively today — it's a snapshot that overwrites itself (`Device.system_info`). This plan turns that gold mine into a real alerting product feature.

Market research (Datadog, Zabbix, Netdata, PRTG, Uptime Kuma, PagerDuty/Opsgenie, homelab communities — see §2) converges on the same lessons, which double as our design constraints:

1. **Sustained-duration thresholds, not instant spikes** — the #1 cause of alert fatigue is alerting on transient blips. CPU/RAM need a "how long" component; disk is the one exception (it doesn't self-resolve, so a high static threshold firing instantly is correct).
2. **Actionable or don't send it** — every alert should let the recipient *do something* in one click (snooze, disable, adjust threshold), not just inform.
3. **Root-cause beats raw numbers** — "disk is at 92%" is much less useful than "disk is at 92% because `docker/nginx/logs` has grown 2.1GB/week for 6 months." Nobody in this market does this well out of the box; it's a genuine differentiator for Portacode because we already have a live agent on the box.
4. **Full history, not just current state** — every tool that people trust logs when an alert fired, when it resolved, and why, so it can be reviewed later and tuned.
5. **Respect user trust in what leaves their machine** — several existing Portacode users may run sensitive workloads; diagnostic detail (file/folder names, future command output) needs a privacy-conscious default, not just a "fewer alerts" one.

## 2. Market scan summary

| Tool | Strength we should copy | Weakness / complaint we should avoid |
|---|---|---|
| Datadog | Rich context in alert payload (variables, tags), Monitor Quality dashboard to find noisy alerts | Alert storms from tightly-coupled monitors; expensive; steep configuration curve |
| Zabbix / Prometheus+Grafana | Free, deeply configurable, industry-standard thresholds | Heavy to run (1–8GB RAM), requires real expertise to configure well — "easy to configure" is explicitly *not* their reputation |
| Netdata | Zero-config, per-second granularity, built-in anomaly detection | No first-class actionable notifications; alerting is a secondary feature |
| Uptime Kuma | Dead simple config, good default alert set, many channels | Uptime-only (no resource metrics), no root-cause detail, no tolerance/duration logic beyond simple retries |
| PagerDuty / Opsgenie | One-click Ack/Snooze/Mute directly in the notification (email/push/chat) — exactly the "quick action buttons" the user asked for | Enterprise pricing/complexity, overkill for a single device |
| General homelab/SRE consensus | Sustained-duration windows (CPU/RAM 10–15 min, disk instant at ~90-95%), recovery notifications, inhibition/grouping to avoid duplicate noise | — |

**Recommended default thresholds (matches both market convention and the user's own spec):**

| Metric | Warning | Critical / instant |
|---|---|---|
| Disk usage | ≥ 90% sustained 10 min | ≥ 95% instant (no sustain window — it won't resolve itself) |
| CPU usage | ≥ 85% sustained 15 min | ≥ 95% sustained 5 min |
| RAM usage | ≥ 85% sustained 15 min | ≥ 95% sustained 5 min |
| Connectivity | offline > 5 min (configurable) | — (no separate critical tier at launch) |

All of the above ship pre-enabled as **system default rules** applied to every device, editable/disable-able per device, exactly mirroring the "storage > 90% for 10 min, or 95% instantly" example in the brief.

## 3. Architecture decisions (confirmed)

These came out of a discussion with the user and are now settled; they drive every phase below.

### 3.1 Where data lives — hybrid, split by sensitivity and by what needs central evaluation

- **Numeric health metrics + alert rules + alert history**: centrally, in the existing **Postgres** database. Rationale: disconnect-based alerts inherently *require* server-side state (an offline device cannot report its own disconnection), so the server is already the unavoidable source of truth for evaluation. Piggybacking rules/metrics on the same DB avoids new infrastructure and this data is small (a handful of numbers per device per interval).
- **Root-cause diagnostic detail (disk breakdown, and later: scheduled-command output)**: generated and cached **on the device** by the agent. Rationale (from user's own concern, which is valid): file/folder paths and command output can be sensitive or bulky, and users on the centralized SaaS may not want that leaving their machine by default.
  - Only a **small, bounded summary** ("`/var/lib/docker/containers/<id>/logs` grew 2.1GB over 7 days") gets attached to the specific `AlertEvent` when it fires and is persisted centrally — that's the minimum needed to make the notification useful and to keep permanent history.
  - The **full breakdown** (every folder/file considered) is never bulk-synced or stored centrally. It's fetched **live on demand** through the same WebSocket command channel already used for `system_info`/terminals, when the user opens the "why did this happen" view and the device is online — the same pattern Portacode already uses for terminals, so no new transport is needed.
  - This becomes a documented, user-facing **privacy setting** later ("Diagnostic detail: keep on device (default) / include in cloud history") — schema supports it from day one (`AlertEvent.detail_summary` is intentionally a short text/JSON field, never a dump).
  - Self-hosted deployments can be advised that this distinction is moot for them (it's their own Postgres either way), but the mechanism stays identical, so we don't fork behavior by deployment mode.

### 3.2 Background processing — reuse the existing Postgres `select_for_update(skip_locked=True)` pattern, no new infra

No Celery/Redis today; the codebase already has a proven convention (`AutomationTask`, `cleanup_expired_devices`, `notify_expiring_devices`) for both queue-style claiming and continuous "run-forever" sweeps. We copy it exactly rather than introducing new infrastructure:

- **Rule evaluation**: a continuous sweep management command (same shape as `disk_usage_monitor_runner` / `notify_expiring_devices --run-forever`), not a per-item queue — it periodically walks active rules + latest metrics and decides state transitions.
- **Notification delivery**: a claim-based queue table (`AlertNotification`, modeled field-for-field on `AutomationTask`'s `status` / `worker_uuid` / `claimed_at` pattern) so sending (SMTP/Telegram API calls) gets retries, backoff, and concurrency for free, and doubles as the delivery history ledger.
- This identical pattern is what we'll extend later for scheduled recurring diagnostic commands (§8), so the investment compounds.

### 3.3 Channels — built on `unicom`, not reinvented

- `unicom` already supports Email, Telegram, WhatsApp, WebChat as first-class channels with a polymorphic `channel.send_message()` and reply/callback plumbing. Portacode-side code should be **channel-agnostic**: an alert rule references an abstract "notification target" (a `unicom.Account` + platform), and delivery goes through `unicom`'s existing `send_message` router — exactly how `unicrm.send_red_alert` already does it for staff alerts.
- **Critical guardrail (explicitly required by the user)**: a channel must never be selectable/relied upon unless the user has actually authenticated it. Before showing Telegram/WhatsApp/etc. as an option, or before dispatching to one, we check `Account.objects.filter(member__user=user, platform=X, blocked=False, channel__active=True).exists()`. If a previously-linked channel becomes unavailable (unlinked, blocked), the rule must be surfaced as "degraded" in the UI (e.g. "Telegram alerts are configured but Telegram isn't connected — connect it or alerts for this channel won't be delivered") rather than silently failing.
- WebChat is excluded from alert delivery (no notification/offline delivery support). Future native mobile push will most likely be added as a new `unicom` platform (decision deferred, but the abstraction here doesn't care — it's just another platform string).
- Email ships first (phase 1) since it needs zero additional user setup; Telegram is the natural phase-2 addition given it's nearly free to wire up.

### 3.4 Quick actions in notifications — reuse `unicom`'s existing primitives, don't reinvent

- **Email**: signed, expiring tokens via `django.core.signing` (`signing.dumps(payload, salt=...)`), exactly the pattern already used for unsubscribe links and demo-claim links. No login required to act — this is the accepted industry pattern (PagerDuty/Opsgenie/unsubscribe links all work this way).
- **Telegram**: `unicom`'s existing `CallbackExecution` + inline-keyboard + signal (`telegram_callback_received`) system — already built for exactly this ("tool_call" confirm/cancel buttons already do this for terminal commands).
- **Portacode-side abstraction**: a single channel-agnostic action executor (e.g. `execute_alert_action(rule_id, event_id, action, actor)` supporting `disable_rule`, `snooze(duration)`, `adjust_threshold(new_value)`, `acknowledge`) that both the email-link view and the Telegram callback handler call into. Adding a new channel later means only building the channel-specific *trigger* (a link vs. a button), never touching the action logic itself.

## 4. Data model (introduced across phases, shown together for coherence)

Rules and their actions are modeled as a **template/instance split**, not a flat table — this is what makes them a pluggable, shareable library rather than hardcoded rows (see §4.1 for the reasoning).

```
AlertRuleTemplate                -- the pluggable, shareable "kind of rule" (the library entry)
  id, slug, name, description
  visibility          enum: system | public | private
  owner               FK User, null for system/public
  forked_from         FK self, null   -- lineage when a user copies a public/system template into their own
  kind                enum: metric_threshold | connectivity | command_result (phase 4) | composite (later)
  metric              enum: cpu | ram | disk | connectivity | ...        (used when kind=metric_threshold/connectivity)
  default_comparator, default_threshold_value, default_sustain_seconds, default_severity
  compatibility_expression   small sandboxed expression string (§4.2) — empty = universal, matches every device
  evaluation_stages   array of enum (§4.3): which onboarding/lifecycle stage(s) this template should be
                       (re-)considered at, e.g. [on_pairing_registered] for the universal system defaults,
                       [on_first_system_info] for anything OS/capability-filtered
  default_application_mode  enum: auto_apply | recommend (§4.3) — what happens on a compatibility match
  recommendation_reason     short author-provided explanation text shown on "recommended" cards
  is_reviewed         bool  (moderation flag, relevant once/if public submissions open up — §9)
  source_one_click_template FK devops template, null — set when this template was generated from a
                       one-click template's `alerts:` section (§4.3), for traceability
  created_at, updated_at

AlertActionTemplate               -- pluggable action bundled to a rule template
  id, rule_template (FK)
  key                 e.g. disable_rule | snooze | adjust_threshold | acknowledge | run_command (phase 4)
  label, requires_confirmation
  params_schema       JSON (e.g. snooze duration options; command template + args in phase 4)
  compatibility_expression   same sandboxed expression language, null = inherit rule template's expression
                       (e.g. a "restart container" action can be *more* restrictive than its parent
                       disk-alert rule, which is universal)

AlertRule                          -- an activation of a template: per-device, or a user's default profile entry
  id, template (FK AlertRuleTemplate), user (FK), device (FK, null = user-level default profile entry)
  comparator, threshold_value, sustain_seconds, severity     -- override; null = inherit template default
  enabled, snoozed_until
  notify_channels     JSON list of {platform, account_id} — validated against authenticated Accounts at save + send time
  created_at, updated_at

AlertRuleRecommendation            -- durable "we suggested this, don't ask again" record (§4.3)
  id, device (FK), template (FK AlertRuleTemplate), stage (enum, which evaluation_stage triggered it)
  status              enum: pending | applied | dismissed
  matched_at, resolved_at
  reason              snapshot of recommendation_reason at match time (kept even if template text changes later)

AlertEvent                        -- the permanent history ledger
  id, rule (FK AlertRule), device (FK)
  state               enum: triggered | resolved | acknowledged
  triggered_at, resolved_at, acknowledged_at
  metric_value_at_trigger
  detail_summary      short text/JSON — root-cause hint, bounded size, generated on-device (§3.1)
  created_at

AlertNotification                 -- delivery queue + history, mirrors AutomationTask's claim pattern
  id, event (FK), platform, account (FK to unicom.Account)
  status              enum: pending | sending | sent | failed
  worker_uuid, claimed_at, sent_at, error
  retry_count

DeviceMetricSample                 -- new: turns the "snapshot only" gap into real history
  id, device (FK), sampled_at
  cpu_percent, ram_percent, disk_percent
  (rollup/retention job keeps this bounded — see §5.4)
```

This directly satisfies the brief's requirement to "keep track of full history" — `AlertEvent` is never overwritten, and `AlertNotification` gives per-channel delivery audit trail for free via the same mechanism that provides retries.

### 4.1 Why template/instance, not a flat table

A flat `AlertRule` table (my original phase-1 sketch) can't represent "a reusable, shareable definition that may or may not apply to a given device." Splitting it means:

- **Your own library**: any `AlertRuleTemplate` with `visibility=private, owner=you` — create from scratch, or "Add to my library" from the public/system catalog, which **copies** the template (`forked_from` set for lineage) rather than linking live. Copy-not-link is the deliberate default: it matches how Grafana dashboards / n8n workflow templates / Zapier "zap templates" behave, so editing your copy never surprises you with upstream changes, and a later official update to the public original doesn't silently alter something already running on a device.
- **The public library**: curated `visibility=public` templates (a handful of premade, genuinely useful examples — the same four defaults in §2, plus more as phases 3/4 unlock command-based ones). For now this is **Portacode-curated only** (staff-authored, `is_reviewed=True`); open community submission/publishing is a real feature but also a real security surface once command-based actions exist (see §9) — deferring that decision rather than baking in an unreviewed marketplace from day one.
- **Your profile's default alerts**: `AlertRule` rows with `device=null` — literally the same row shape used for a real per-device rule, just not yet bound to a device. At each relevant lifecycle stage (§4.3) for a device — pairing, first `system_info`, provisioning completion — we diff the user's `auto_apply` default-profile rules against the device's compatibility expression (§4.2) and auto-create matching per-device `AlertRule` rows; `recommend`-mode ones become `AlertRuleRecommendation`s instead. This is exactly the "filters based on the OS of the new device" behavior requested, applied at the point in the device's lifecycle where the relevant data actually exists.
- **Actions inherit the same shape**: most rules only ever need the four generic, universally-compatible actions (disable/snooze/adjust threshold/acknowledge), but a future template (e.g. "Docker container stuck restarting") can bundle a command-based action ("restart container") that's only compatible with devices where Docker is detected — without needing a different schema.

### 4.2 Compatibility expression — concise, code-like, but safely sandboxed

Both a nested JSON condition tree and free-text OS matching turned out to be the wrong shape: JSON is verbose for anything beyond one or two conditions, and exact-string OS matching is the *binary matching* the user explicitly wants to avoid. What we actually want is a small **expression language** — genuinely code-like and far more concise than JSON — evaluated through a **sandboxed interpreter that only understands a tiny, fixed grammar**, so a public/shared template can never carry arbitrary logic. This is the same approach behind Ansible's `when:` conditions, GitHub Actions' `if:` expressions, and (more formally) Google's CEL — small boolean expression languages purpose-built for "safe rule evaluation," not general-purpose scripting.

```
os_family == "debian" and "docker" in capabilities
arch in ["x86_64", "arm64"] and version_gte(os_version_id, "20.04")
"docker" in capabilities or "podman" in capabilities
```

An empty string always matches (the common case today: CPU/RAM/disk/connectivity are collected identically on every OS `psutil` supports, so the four default templates ship with an empty expression).

**Implementation — a custom AST allowlist, not `eval()`/a template engine, and no new dependency:** parse with Python's `ast.parse(expr, mode="eval")`, then walk the tree and reject anything outside a fixed allowlist before ever evaluating it:

- Allowed nodes: `BoolOp` (`and`/`or`), `UnaryOp` (`not`), `Compare` (`==`, `!=`, `in`, `not in`, `<`, `<=`, `>`, `>=`), `Name` (only names present in the device-profile context — no attribute/subscript access, no builtins), `Constant` (str/int/float/bool), `List`/`Tuple` literals, and `Call` — but only for a fixed, explicitly whitelisted function registry (`version_gte`, `version_lte`, `version_gt`, `version_lt` for semantic-version-aware comparisons that plain string comparison gets wrong).
- Everything else (attribute access, subscripting, comprehensions, lambdas, imports, arbitrary calls, dunder names) is rejected at parse-time, before execution — so there is no code-injection surface even for fully public, user-authored templates.
- Expression length and AST depth are capped (defensive, not because the sandbox is unsafe otherwise) so a pathological expression can't cause pathological evaluation cost.
- This is intentionally hand-rolled rather than pulling in `simpleeval` or `cel-python`: the grammar we need is tiny, and keeping it in-house keeps the entire security-relevant surface auditable in one small module rather than trusting a third-party library's sandbox guarantees.

**Device profile fields available in the expression context** (new, added to `Device` + populated by the agent):

- `os_family` — a normalized family tag (`debian`, `rhel`, `arch`, `darwin`, `windows`, …), not a display string. Linux already self-describes this via `/etc/os-release`'s `ID` + `ID_LIKE` fields (e.g. Ubuntu declares `ID=ubuntu, ID_LIKE=debian`) — the agent's existing `_get_os_info()` only keeps a human-readable `os_version` string today (`system_handlers.py:413-477`); this phase extends it to also emit `os_family`/`os_id`/`os_version_id` so filtering is family-tree-aware ("debian-family" matches Ubuntu, Mint, Debian, etc.) instead of exact-string matching.
- `arch` — already collected (`platform.machine()`).
- `capabilities` — a small opportunistically-detected tag list (`docker`, `systemd`, `apt`, `yum`, `cgroup_v2`, …), extensible over time; only populated for what a given phase actually needs (an unlisted capability just fails an `in` check, correctly excluding that template rather than erroring).

This is a small, additive agent change (independent of phase 3's heavier disk-scanning release) and is worth shipping in the phase 1 agent bump since the schema depends on it existing.

**UI note:** most users shouldn't need to type expressions at all — the library builder UI (phase 2) offers a guided field/operator/value picker for the common cases (which happens to compile to exactly this expression syntax under the hood), with a "advanced/raw expression" toggle for power users. The stored, authoritative form is always the expression string; the guided UI is just one way to produce it.

### 4.3 Multi-stage evaluation — rules get (re-)considered at the right point in a device's lifecycle

Devices enter the system through genuinely different paths with different data-availability timelines (confirmed in the codebase, not assumed):

| Path | What's known, and when | Reference |
|---|---|---|
| **Pairing** | Ownership assigned immediately (`_register_paired_device()`), but OS/arch/capabilities are **not** known yet — the first real `system_info` only arrives after the device establishes its live connection and a dashboard client session triggers it, which can be seconds to indefinitely later | `pairing_dashboard_consumer.py:111-138`, `device_gateway_consumer.py:628-634`, `terminal.py:909-924` |
| **One-click template provisioning** | OS is actually *known upfront* (the template picks `source_template`), but the thing an alert would care about (e.g. "is the app actually running") is only true after the template's `instructions` finish — i.e. `AutomationTask` success, later than bare VM creation (`proxmox_container_created`) | `device_gateway_consumer.py:656-673`, `run_automation_workers.py:~1784` |
| **Manual key / CLI connect without pairing** | Similar to pairing: ownership known immediately, OS known only once connected | `device_views.py:162-195`, `cli.py:248-268` |

Given this, `AlertRuleTemplate.evaluation_stages` names the specific lifecycle moment(s) a template becomes eligible for (re-)evaluation against a device, rather than assuming one global "check compatibility now" moment:

- **`on_pairing_registered`** — fires right after ownership is assigned, before OS info exists. Only meaningful for templates with an empty/OS-independent expression — this is exactly where the four universal system defaults (disk/CPU/RAM/connectivity) get applied, unchanged from the original phase 1 design, just now named precisely.
- **`on_first_system_info`** — fires the first time a device's `system_info` arrives and `os_family`/`capabilities` become known (detected as a one-time transition, not re-fired on every 10s push). This is where OS/capability-filtered templates (e.g. a Docker-only template) get their first real shot at matching.
- **`on_provisioning_complete`** — fires when a one-click template's post-provision `instructions` finish successfully (`AutomationTask` success), scoped **only to the device just deployed by that template** — this is where a template's own bundled `alerts:` entries (below) get evaluated, not the general public/private library.
- **`on_profile_change`** (deferred per §9, but now a named stage rather than an ad-hoc idea) — re-runs `on_first_system_info`-equivalent matching if a device's detected OS/capabilities change later (e.g. Docker gets installed after initial registration).
- **`manual`** — a user can always browse the library and apply any compatible template to a device explicitly, regardless of stage.

**What happens on a match — `auto_apply` vs `recommend`:**

- **`auto_apply`**: immediately instantiates a real `AlertRule` for the device. This stays the behavior for the four system defaults (matches the brief's "certain alerts should be active by default").
- **`recommend`**: creates an `AlertRuleRecommendation` row instead of silently applying anything, surfaced in the dashboard as a card — name, `recommendation_reason`, and **Apply** / **Dismiss** actions. Once dismissed, that template is never re-recommended for that device (the same anti-spam philosophy as the alerts themselves — recommendations shouldn't nag either). A user can also promote any template from their own or the public library into their **default profile** as `auto_apply`, meaning "apply this automatically to every future compatible device without asking me" — this is what "define default alerts on your profile" concretely means now that recommend/auto_apply are distinct.
- Public/private templates default to `recommend` unless explicitly promoted; system defaults ship as `auto_apply`.

**One-click templates defining their own alerts.** The `portafile.yaml` schema (`content/one-click/*.md`, served via `devops/views.py:one_click_portafile_yaml()`) gains an optional top-level `alerts:` list, sitting alongside the existing `instructions`/`expose_ports` keys:

```yaml
alerts:
  - template: docker-log-growth      # reference an existing public/system template by slug, with overrides
    mode: recommend
  - name: Uptime Kuma unreachable     # or define one inline, scoped to this one-click template
    metric: connectivity
    threshold: n/a
    mode: auto_apply
    reason: "Uptime Kuma's entire purpose is uptime monitoring, so we watch it by default."
```

Inline entries get materialized into `AlertRuleTemplate(visibility=system, source_one_click_template=<template>)` rows the first time the template is synced (mirrors how `sync_content.py` already syncs the rest of the YAML), and are evaluated at `on_provisioning_complete` for the specific device that just ran that template — never applied to unrelated devices.

## 5. Phase 1 — Foundations: metric history, default rules, email alerts, full history (MVP)

**Goal at the end of this phase: a user can see their device's health history and receives an email — with working one-click actions — for the default disk/CPU/RAM/connectivity rules, without configuring anything.** This is the smallest slice that is genuinely useful end-to-end.

1. **Server models**: `AlertRuleTemplate`, `AlertActionTemplate`, `AlertRule`, `AlertRuleRecommendation`, `AlertEvent`, `AlertNotification`, `DeviceMetricSample` (§4, minus disk root-cause fields, added in phase 3). Building the template/instance split now — even though only four universal, non-command templates exist at this point — avoids a schema migration later; it's barely more work than a flat table since the four defaults all have an empty `compatibility_expression` anyway. `AlertRuleRecommendation` is created now but stays unused until phase 2 (nothing in phase 1 is `recommend`-mode).
2. **Device profile fields**: add `os_family`, `os_id`, `os_version_id`, `capabilities` to `Device`; extend the agent's `_get_os_info()` to emit them from `/etc/os-release` (`ID`/`ID_LIKE`) instead of only a display string (§4.2). Small, additive, low-risk agent change — bundle it into this phase's agent release rather than waiting for phase 3's heavier disk-scanning release.
3. **Compatibility expression evaluator**: implement the sandboxed AST-allowlist interpreter from §4.2 as a standalone, unit-testable module (no Django dependency) — this is a security-relevant piece worth building and testing in isolation before anything calls into it.
4. **Metric history capture**: on each `system_info` persist in `DeviceGatewayConsumer._persist_device_system_info_snapshot`, also write a `DeviceMetricSample` row (cheap; one row per device per ~10s while connected). Add a retention/rollup job (mirrors existing cron-style management commands) that downsamples samples older than N days (e.g. keep raw for 48h, 5-min averages for 30 days, hourly averages beyond that) so the table doesn't grow unbounded — this is the "prepared for full history" requirement without needing TimescaleDB yet.
5. **Seed the four system templates** (disk/CPU/RAM/connectivity from §2's table) as `AlertRuleTemplate(visibility=system, evaluation_stages=[on_pairing_registered], default_application_mode=auto_apply)` rows with matching `AlertActionTemplate`s for the four generic actions (disable/snooze/adjust_threshold/acknowledge, all universally compatible). A data migration creates each existing user's default-profile `AlertRule` rows (`device=null`) referencing these templates, and per-device `AlertRule` rows for every existing device. New devices get the same auto-instantiation wired into `_register_paired_device()` (§4.3's `on_pairing_registered` hook) — trivial at this phase since every template is universal and pairing already has everything needed, but this is exactly the hook phase 2's OS-filtered examples will reuse for real at a later stage.
6. **Connectivity tracking hook**: reuse existing `_broadcast_device_status` connect/disconnect signal path to feed the evaluator instead of building a new detector.
7. **Evaluator** (`evaluate_alert_rules --run-forever` management command, modeled on `disk_usage_monitor_runner`): every ~30s, for each enabled `AlertRule`, resolves effective threshold/sustain/severity (override, else template default), checks the relevant `DeviceMetricSample` window (or connectivity state), creates/transitions `AlertEvent` rows (open → resolved), and enqueues `AlertNotification` rows for each configured channel. Snoozed/disabled rules are skipped but still logged as suppressed in a debug counter (useful later for a "why didn't I get notified" view).
8. **Notification worker** (`run_alert_notification_workers`, modeled field-for-field on `run_automation_workers`'s claim loop): claims pending `AlertNotification` rows, renders and sends via `unicom`'s email channel (reusing `unicom.services.email` / the same abstraction `send_red_alert` and system emails already use), marks sent/failed with retry+backoff.
9. **Email template** with one-click actions: "Disable this alert" / "Snooze 1h / 24h" / "Increase threshold to X%" — signed links (`django.core.signing`, dedicated salt e.g. `data.alert_action`) pointing at a small public view that validates the token and calls the shared `execute_alert_action()`, then shows a plain confirmation page. A resolved notification is sent too ("Disk usage back to normal") so users aren't left wondering — addresses the "recovery notification" best practice from §2.
10. **Dashboard UI (minimal but real)**:
    - Per-device "Alerts" tab: list of active `AlertRule`s (defaults pre-populated, editable threshold/sustain/enabled/snooze), and an event history table (triggered/resolved timestamps, severity).
    - A simple sparkline/small history chart per metric using `DeviceMetricSample`, addressing the existing gap that health % badges have no trend view today.
    - No library browsing UI yet (phase 2) — rule editing at this stage only tweaks the override fields on existing instances.
11. **Guardrails baked in from day 1**: channel-authentication check before allowing a channel to be selected (§3.3) — at phase 1 this just means "email is always available since it's the user's account email," but the check function is written generically so phase 2 (Telegram) plugs in without touching rule-save logic.

**Exit criteria (testable):** disconnect a real device for >5 min → email fires with correct copy and working action buttons; fill a test disk to 90%+ for 10 min → warning email; hit 95% → instant email; clicking "disable" in the email actually disables the rule and stops further emails; dashboard shows rule list + event history + a metric trend chart; a device's `os_family`/`capabilities` are visible on the `Device` record (even though nothing filters on them yet).

## 6. Phase 2 — Multi-channel delivery + real configurability

**Goal: users can add Telegram (and any other already-authenticated unicom channel) as an alert destination, tune tolerance per rule, and the system clearly guards against depending on unauthenticated channels.**

1. Channel picker UI queries authenticated `Account`s for the user (§3.3 check) per platform; unauthenticated platforms show a "Connect Telegram" CTA instead of being silently selectable.
2. Telegram delivery: notification worker gains a Telegram sender using `unicom`'s existing send/callback stack; action buttons become native Telegram inline keyboards via `CallbackExecution` (§3.4) instead of links.
3. Per-rule notify-channel selection (a rule can go to email only, Telegram only, or both) plus a user-level default channel preference so new default rules don't need per-rule setup.
4. "Degraded rule" indicator when a rule's configured channel becomes unauthenticated (e.g., user disconnects Telegram later) — matches the explicit requirement to never let users silently rely on something not actually connected.
5. Tolerance/threshold self-tuning helpers: since the research consistently says thresholds should reflect actual baseline behavior, show the device's own historical p95 CPU/RAM (from `DeviceMetricSample`) next to the threshold input in the UI ("your device typically peaks at 62% — consider a threshold above that") — this is a cheap, high-value differentiator vs. competitors that just show a blank input box.
6. Connectivity-tolerance becomes explicitly configurable per device (not just a global 5 min default), directly addressing the "if every disconnect sends a warning, it's spammy" concern.
7. **"My Library" management UI**: create/edit/delete your own `AlertRuleTemplate`s (`visibility=private`) from scratch — pick a `kind`/metric, defaults, and (optionally) a compatibility condition via a guided field/operator/value builder (compiling to the §4.2 expression string), with a raw-expression toggle for power users.
8. **Public Library browse UI**: the Portacode-curated catalog (still just the four defaults plus maybe a couple more non-command examples at this point), each card showing a compatibility badge resolved against the currently-selected device ("Compatible" / "Requires Debian-based OS" / "Requires Docker"). "Add to my library" copies the template into the user's private library (`forked_from` set) — never a live link — per §4.1.
9. **`on_first_system_info` hook**: wire the one-time OS/capability-detection transition (§4.3) into the same instantiation path used at pairing — this is what lets OS-filtered templates (phase 3's Docker example) actually get applied/recommended once real device data exists.
10. **Recommendation engine + UI**: `recommend`-mode matches create `AlertRuleRecommendation` rows surfaced as dashboard cards (name, `recommendation_reason`, Apply/Dismiss); dismissed recommendations are never re-shown for that device. "Promote to my default profile as auto-apply" lets a user turn any template (their own or public) into something that's silently applied to every future compatible device — this is what "default alerts on your profile" means now that recommend/auto-apply are distinct concepts.
11. Applying a library template to a device (or to your default profile) is the same "instantiate `AlertRule` from `AlertRuleTemplate`" code path used by auto-instantiation in phase 1 — this phase is mostly UI + the recommendation layer on top of already-working plumbing.

**Exit criteria:** a user can connect Telegram, assign it to a rule, receive and act on a Telegram alert with inline buttons; disconnect Telegram and see the rule marked degraded without alerts silently vanishing unexplained; a user can write their own rule template with a guided compatibility condition, save it to their private library, and apply it to a device; browsing the public library and hovering an incompatible example clearly explains why it's greyed out for the selected device; a recommendation appears on a real device once its OS/capabilities are known, and dismissing it makes it not reappear.

## 7. Phase 3 — Root-cause disk insights

**Goal: a disk alert email says *why*, not just *that*.**

This phase requires a new `portacode` PyPI release (agent-side work), per the user's direction.

1. **Agent-side lightweight always-on scan**: alongside the existing 10s `system_info` push, periodically (e.g. every 15–30 min, cheap) snapshot sizes of a curated set of "usual suspects" (docker data root, common log dirs, package manager caches, user home top-level folders) — not a full recursive filesystem walk.
2. **Agent-side local growth comparison**: each snapshot is diffed against the previous one, stored **locally** (SQLite file alongside existing agent state). Only when a folder shows meaningful/sustained growth does the agent escalate to a deeper recursive scan of *that* subtree to find the actual biggest/fastest-growing items — bounding cost to the rare case that matters.
3. **Compact reason generation on-device**: when a disk `AlertEvent` is about to fire, the agent is asked (via the existing WS command channel, same pattern as `system_info`) for a short structured summary of top offenders + growth trend; this becomes `AlertEvent.detail_summary`, included in the email body ("...largely because `/var/lib/docker/containers/<id>-json.log` grew 2.1GB in 7 days").
4. **Live drill-down in dashboard**: an "Investigate" view issues a live WS request to the (online) device for the fuller breakdown — never persisted centrally, per the privacy decision in §3.1. If the device is offline, the view falls back to the compact summary already stored with the event.
5. Same mechanism generalizes trivially to RAM alerts later (top memory-consuming processes) and is intentionally metric-agnostic in its plumbing.
6. First real payoff of the compatibility-expression work from §4.2: a "Docker logs growing unbounded" `AlertRuleTemplate` (`compatibility_expression = '"docker" in capabilities'`, `evaluation_stages=[on_first_system_info]`, `default_application_mode=recommend`) only makes sense on devices where Docker is detected — this phase adds it to the public library as the first genuinely-filtered example (everything in phases 1–2 was universal), surfaced through the recommendation UI built in phase 2.
7. **One-click template `alerts:` integration**: extend the `portafile.yaml` schema (§4.3) and `sync_content.py` to materialize a template's inline `alerts:` entries as `AlertRuleTemplate(source_one_click_template=...)` rows, and wire the `on_provisioning_complete` hook (`AutomationTask` success) to evaluate them against the specific device that was just provisioned. This is scoped separately from the general public library and can slip independently if the `devops` app side needs more design time — it doesn't block this phase's disk-insight exit criteria.

**Exit criteria:** trigger a real disk-growing scenario (e.g. a runaway log file) → the resulting alert email names the specific offending path and growth rate; opening "Investigate" in the dashboard while the device is online shows a fuller breakdown fetched live; a device with Docker detected gets the log-growth template recommended via `on_first_system_info`, and deploying a one-click template with an `alerts:` section results in the right rule appearing on that specific device once provisioning finishes, and only that device.

## 8. Phase 4 — Scheduled diagnostic commands + historical parsing (forward-looking foundation)

Not building the full feature yet, but this plan is written so phases 1–3 don't need rework when we get here.

1. New model, deliberately modeled on `AutomationTask`'s claim pattern but supporting **recurrence** (cron-like interval) and **repeated runs with a result history table** (`DeviceScheduledCommand` + `DeviceCommandRun`), rather than repurposing `AutomationTask` (which is explicitly single-execution).
2. `DeviceCommandRun.output` follows the same sensitivity posture as §3.1: **local-first**, with only a bounded, user-configured "store output centrally" opt-in per scheduled command (defaults off for anything not explicitly marked safe) — because arbitrary command output is a much higher sensitivity/size concern than disk path names.
3. Result parsing (LLM-based anomaly/issue detection over historical run output) plugs in as a consumer of `DeviceCommandRun` history — doesn't require any change to the ingestion/storage design above, only an additional analysis worker.
4. This is where `AlertRuleTemplate.kind` gains real new values: `command_result` (rule triggers on a scheduled command's parsed output/exit code) and `AlertActionTemplate.key=run_command` (an action that dispatches a command template, e.g. "restart container", "clear old logs in this folder"). Both plug into the *same* compatibility-expression mechanism from §4.2 — a `run_command` action naturally declares stricter requirements (e.g. `"docker" in capabilities`) than the metric-based rules that exist today, and the *same* evaluator/notification/history pipeline handles them; no parallel system.
5. Command-based templates and actions are exactly where the "public library, curated-only for now" decision in §4.1 matters most — letting arbitrary users publish executable command templates to a shared catalog is a real injection/security surface, so this phase should keep new command-based public entries Portacode-authored/reviewed unless/until a moderation workflow exists (see §9).

## 9. Open items intentionally deferred (flagged, not forgotten)

- Native mobile push channel: likely a new `unicom` platform; decide when a mobile app materializes (§3.3).
- Multi-instance scaling of the in-memory Channels layer (would need Redis) — orthogonal to this feature, only matters if Django itself scales horizontally.
- Any plan tiering/limits — explicitly out of scope per user (not a monetization discussion).
- Alert *grouping/inhibition* (e.g. "CPU + RAM + disk all firing at once because of one root event → send one message") — worth revisiting after phase 3 ships, once we have real usage data on whether correlated alerts are actually a problem for a single-device product (less likely than in Datadog's microservices use case, but worth watching per the "alert storm" research in §2).
- **Open community submissions to the public library**: this plan assumes the public library stays Portacode-curated (`is_reviewed=True`, staff-authored) through phase 4. Letting any user publish a template — especially a command-based one — into a shared catalog that other users can one-click "add to my library" is a real feature but needs a moderation/review workflow (and probably a sandboxed preview of exactly what a command template will run) before it's safe to open up. `AlertRuleTemplate.visibility` already has room for a future `community_pending` state; the workflow itself is deferred.
- **`on_profile_change` re-evaluation**: named as a stage in §4.3, but not actually wired up until it's needed — worth doing in phase 3 alongside the first real filtered template — since phase 1/2's exit criteria don't require it (nothing changes a device's OS family after the fact in those phases' test scenarios).
- **Expression language growth**: §4.2's function registry starts with just `version_gte`/`version_lte`/`version_gt`/`version_lt`. If real templates need more (e.g. regex-ish matching), each addition should go through the same "explicit allowlist" discipline rather than loosening the sandbox generally — resist the temptation to "just add attribute access" for convenience.
