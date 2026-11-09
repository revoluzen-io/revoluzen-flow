# Revoluzen Flow notifications and alerts

Per `edition-matrix.md` line 64-72, notification capabilities split as follows.

## OSS Free includes

**Channels** — in-app bell + email + Slack + Teams + Discord + generic webhook. Configure each in **Settings → Notifications**.

**Per-pipeline triggers**:
- **`ON_FAILURE`** — fires when a pipeline run ends with status=error
- **`ON_LONG_RUNNING`** — fires when a run exceeds the configured duration threshold (default 30 minutes; tunable in Settings)
- **`ON_SCHEDULE_MISS`** — fires when a scheduled pipeline doesn't fire within its expected window (default 5-minute grace)

**Credential expiry warnings** — when a saved credential has an expiry date, administrators are warned on the bell and by email a week ahead, a day ahead and when it expires, once each per date. For example, a key expiring on 30 June is flagged on 23 June, on 29 June and on 30 June. Renewing to a new date starts the warnings again. The check runs hourly. Email needs SMTP. Turn it off with **Settings → Notifications → Automatic Alerts → Credential expiry warnings** (`notify_on_credential_expiry`, on by default). The warning opens the Credentials page, and the **Credentials** chip on the Notifications page filters to these.

**Watchdog** — a background loop that polls the executions store at the schedule cadence to detect schedule-miss + long-running situations. Lives in `worker_pool._timeout_watchdog_loop`.

**Browser desktop alerts** — fires in-tab notifications via the browser Notification API when the chat tab is focused.

**Notification config persistence** — admin-only `GET/PUT /api/notifications/config`. The watchdog reads from `admin_settings`; SettingsPage saves there.

## Revoluzen Flow Enterprise adds

- **Quiet hours** — suppress non-critical alerts during specified time windows. Useful for nighttime operators who only want page-level alerts after hours.
- **Debounce** — collapse repeated alerts within a configurable window. If a pipeline fails 50 times in 10 minutes, only the first alert fires until debounce expires.
- **Daily digest emails** — once-a-day rollup of yesterday's failures + long-running outliers, sent at the configured time.
- **Per-event policies** — different routing rules per event type (FAILURE → on-call rotation; LONG_RUNNING → analytics team).
- **Escalation** — if first responder doesn't acknowledge within N minutes, alert the next person in the rotation.
- **Per-user notification preferences** — each workspace member sets their own channels + thresholds. (OSS Free is single-user, so this doesn't apply.)
- **Compute-usage alerts** — memory/CPU/runtime thresholds beyond the basic triggers. Fires when peak_memory_mb or cpu_seconds exceeds a per-pipeline budget.
- **Drift detection** — scheduled scans that fire critical-event notifications when a pipeline's row count, distinct-key count, or output schema drifts from a baseline.

## Configuration paths

**OSS Free**:
- Settings → Notifications → channels (email SMTP, Slack webhook, Discord webhook, generic webhook URL)
- Pipelines page → pipeline detail → Alerts tab → add rule
- Settings → Notifications → Automatic Alerts section → long-running threshold, schedule-miss detection and credential expiry warnings

**Revoluzen Flow Enterprise** (above plus):
- Settings → Notifications → Quiet Hours
- Settings → Notifications → Debounce policy
- Settings → Notifications → Daily Digest schedule
- Account → Notification Preferences (per-user)
- Settings → Approvals → escalation policy

## Anti-patterns (do not suggest these)

- ❌ **"Set up Pagerduty integration"** as if it's a built-in Revoluzen Flow feature. Revoluzen Flow has a generic webhook + a PagerDuty SaaS Connector for sending events; there's no first-class PD integration setting. If the operator wants PagerDuty alerts, they configure PagerDuty's webhook ingestion endpoint as the Revoluzen Flow alert webhook URL.
- ❌ **"Use OpsGenie / Splunk On-Call"** — same as above. Revoluzen Flow uses generic webhooks; the third-party tool's webhook ingest URL is what goes in the Revoluzen Flow alert config.
- ❌ Telling a Free user "set up daily digest emails" — that's a Plus feature. The Free user's path is per-event email alerts.
- ❌ Telling a Free user "configure escalation" — Plus-only. Free is single-user; escalation has nowhere to escalate to.

## Where to look for evidence

| Question | Where |
| --- | --- |
| Which channels can I send to? | Settings → Notifications |
| Why didn't my schedule-miss alert fire? | Backend log line `_timeout_watchdog_loop` ran at … |
| Did the email leave the host? | Settings → Notifications → "Test email" button |
| What's the long-running threshold? | Settings → Notifications → Automatic Alerts |
| Why was I warned about a credential? | Its expiry date is within a week, or has passed. Connections → Credentials |
| How do I disable a noisy alert temporarily? | Pipelines → pipeline → Alerts tab → toggle |
