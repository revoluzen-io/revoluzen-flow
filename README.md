<p align="center">
  <a href="https://revoluzen.io"><img src="docs/assets/revoluzen-logo.png" alt="Revoluzen" width="88" height="88"></a>
</p>

<h1 align="center">Revoluzen Flow</h1>

<p align="center">
  <strong>Local-first data pipeline engine.</strong><br>
  Visual ETL on a vectorised DuckDB core — on your laptop, your VM, your rules.
</p>

<p align="center">
  <a href="https://github.com/revoluzen-io/revoluzen-flow/actions/workflows/ci.yml"><img src="https://github.com/revoluzen-io/revoluzen-flow/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/revoluzen-io/revoluzen-flow/actions/workflows/security-scan.yml"><img src="https://github.com/revoluzen-io/revoluzen-flow/actions/workflows/security-scan.yml/badge.svg?branch=main" alt="Security Scan"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License: Apache 2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/status-1.0.1-blue" alt="Status: 1.0.1"></a>
</p>

<p align="center">
  Built and maintained by <a href="https://revoluzen.io"><strong>Revoluzen</strong></a>
</p>

---

```bash
pip install revoluzen-flow
python -m revoluzen_flow open
```

The backend boots on loopback, your browser opens, and you're in.

Vectorised DuckDB engine · built-in scheduler, alerts and run history · 40 node
types · embedded AI assistance that defaults to a local model · an open
connector framework you can extend in minutes. Apache 2.0 forever, with
predictable seat pricing for teams via Revoluzen Flow Enterprise.

> **Status: 1.0.1 stable.** Tested with Python 3.11/3.12 · Docker 25+ ·
> DuckDB 1.1.3 · Postgres 16. See [CHANGELOG.md](CHANGELOG.md) for the full
> tested-with matrix and known gaps. PyPI, Docker and source installs are all
> supported today; prebuilt desktop installers are still pending.

## Contents

- [Quick start](#quick-start) — pick an install path
- [First run](#first-run) — from install to real output in a minute
- [Running it](#running-it) — ports, background service, cloud
- [Why Revoluzen Flow](#why-revoluzen-flow)
- [Connectors](#connectors)
- [Steward reliability layer](#steward-reliability-layer)
- [Editions](#editions)
- [Install troubleshooting](#install-troubleshooting)
- [License](#license) · [Contributing](#contributing) · [Security](#security) · [Links](#links)

## Quick start

Revoluzen Flow is a **self-hosted server plus web app** — a backend that serves
a browser UI, not a desktop program. Pick the path that fits you:

| You are | Path | Time |
|---|---|---|
| On any OS and want it to just work | **[Docker Compose](#docker-compose)** — one identical command everywhere | 2–5 min |
| Comfortable with Python 3.11+ | **[PyPI](#pypi)** | 2 min |
| On Linux and would rather not run Docker | **[Linux package](#linux-package)** — `.deb` / `.rpm` / `.AppImage` | 2 min |
| A developer or contributor | **[From source](#from-source)** | 10 min |

Hitting an error? Everything we know about install failures is collected in
[Install troubleshooting](#install-troubleshooting) rather than scattered
through these steps.

### Docker Compose

```bash
git clone https://github.com/revoluzen-io/revoluzen-flow.git
cd revoluzen-flow
cp .env.example .env            # optional — defaults work
docker compose up -d            # Revoluzen Flow only
# or:
docker compose --profile ai up -d   # Revoluzen Flow + local Ollama
```

Open <http://localhost:8001>.

> **The image builds locally on first run** — `revoluzen/flow` is not on Docker
> Hub yet. `docker-compose.yml` declares `build: context: .`, so
> `docker compose up` builds it from the `Dockerfile` at repo root: ~3–5 min,
> one time, and no `pull access denied` despite the image name. `docker compose
> pull` will fail until the image is published — use `docker compose build` to
> pick up changes.

To upgrade later: `docker compose pull && docker compose up -d`.

<details>
<summary><strong>Single container instead of compose</strong></summary>

One container is enough, but **build the image first** — `docker run` has no
build step and would try to pull an image that isn't published yet:

```bash
docker build -t revoluzen/flow:1.0.1 .
docker run -d --name revoluzen-flow \
  -p 8001:8001 \
  -v revoluzen_flow_data:/data \
  -e REVOLUZEN_FLOW_DATA_DIR=/data \
  revoluzen/flow:1.0.1
```

</details>

### PyPI

**Requires Python 3.11+.** Check first — an older Python fails with `No matching
distribution found`:

```bash
py --version        # Windows
python3 --version   # macOS / Linux
```

```powershell
# Windows — the py launcher avoids Python Scripts/PATH shortcut issues
py -m pip install --upgrade revoluzen-flow
py -m revoluzen_flow open
```

```bash
# macOS / Linux — modern macOS ships python3, not python
python3 -m pip install --upgrade revoluzen-flow
python3 -m revoluzen_flow open
```

`revoluzen open` is an optional shorthand the install creates. If it isn't
found, `python -m revoluzen_flow open` always works — see
[Install troubleshooting](#install-troubleshooting).

### Linux package

Bundles a frozen Python runtime and the compiled UI — no Python, no Node, no
Docker needed on the target machine. After install, Revoluzen Flow registers
itself as a systemd **user** service that starts at login and survives reboots.

Download from the [latest GitHub Release](https://github.com/revoluzen-io/revoluzen-flow/releases/latest):

| Distro | Package | Install |
|---|---|---|
| Debian / Ubuntu 22.04+ | `revoluzen_flow_1.0.0_amd64.deb` | `sudo apt install ./revoluzen_flow_1.0.0_amd64.deb` |
| Fedora / RHEL / Alma / Rocky 9+ | `revoluzen-flow-1.0.0-1.x86_64.rpm` | `sudo dnf install ./revoluzen-flow-1.0.0-1.x86_64.rpm` |
| Any Linux (glibc 2.27+) | `RevoluzenFlow-1.0.0-x86_64.AppImage` | `chmod +x ./RevoluzenFlow-*.AppImage && ./RevoluzenFlow-*.AppImage` |

Then open <http://localhost:8001> — the service is already running.

> **These packages are not GPG-signed**, so `apt` / `dnf` will note the package
> is unauthenticated. Verify what you downloaded against `SHA256SUMS` on the
> release: `sha256sum -c SHA256SUMS`. That proves the file arrived intact, not
> who built it — signing is on the list.

Building these installers yourself (CI / private builds): see
[`installer/readme.md`](installer/readme.md).

### From source

Requires Python 3.11+ and Node 20+. Install and run are **two separate steps**,
and you must be **inside the cloned repo** — the `.` in `pip install -e .`
means "the project in this folder".

**macOS / Linux:**

```bash
git clone https://github.com/revoluzen-io/revoluzen-flow.git
cd revoluzen-flow

# 1. Build the UI (one-time). The bundle isn't committed — skip this and
#    the API runs but / serves nothing.
cd frontend && npm ci && npm run build && cd ..

# 2. Install into a venv, then run.
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m revoluzen_flow open
```

**Windows (PowerShell)** — one line at a time; `&&` chaining and `source` are
not valid in Windows PowerShell:

```powershell
git clone https://github.com/revoluzen-io/revoluzen-flow.git
cd revoluzen-flow

# 1. Build the UI (one-time).
cd frontend
npm ci
npm run build
cd ..

# 2. Install into a venv, then run.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m revoluzen_flow open
```

> **If `Activate.ps1` is blocked by execution policy**, don't fight it — call
> the venv's executables directly: `.\.venv\Scripts\pip.exe install -e .` then
> `.\.venv\Scripts\python.exe -m revoluzen_flow open`.

> Skipping the UI build gets you a working API and a blank page. Revoluzen Flow
> says so loudly at startup (`no frontend build found …`) rather than leaving
> you to guess.

### Where's the native Windows / macOS installer?

For a self-hosted server app, the standard way to run it on Windows or macOS is
Docker Desktop — that's how comparable tools (Grafana, Metabase, Gitea,
Nextcloud) ship, and it works identically everywhere. A native `.exe` / `.pkg`
*builds and works*, but we don't publish it: unsigned, it trips Windows
SmartScreen and macOS Gatekeeper with a scary dialog, and we won't hand you
that. Want it anyway? Build it in one command — see
[`installer/readme.md`](installer/readme.md). Signed installers land once we
have an EV certificate and Apple notarization.

## First run

On first launch Revoluzen Flow asks you to **create your account** — your email
and a password. This first account owns the instance.

Then open **Templates → First pipeline — CSV in, CSV out → Use Template → Run**.
You'll see real output from a seeded sample dataset in under a minute: no
Postgres, no API keys, no cloud account.

> **Automating a headless deploy** that can't do the interactive first run? Set
> `REVOLUZEN_FLOW_BOOTSTRAP_ADMIN=1` and Revoluzen Flow auto-creates
> `admin@revoluzen.local` with a random password — read it with
> `docker compose exec flow cat /data/INITIAL_ADMIN_PASSWORD.txt`, sign in, and
> rotate it.

## Running it

`python -m revoluzen_flow open` starts the backend on a free port (8001 by
default, falling back if in use) and opens your browser. If the output says
`port 8001 was in use — using 8003 instead`, open the printed URL — no need to
remember one.

```bash
python -m revoluzen_flow serve             # start on 127.0.0.1:8001, open the browser yourself
python -m revoluzen_flow serve --open      # same as `open`
python -m revoluzen_flow serve --port 9000
```

Health check: `curl http://localhost:8001/api/health`

**Headless, WSL2, Docker, DevContainer or remote-SSH?** `open` detects these
automatically, skips the browser launch and prints the URL prominently so you
can paste it into a browser on your host. `--no-open` force-skips the launch
while keeping the port-fallback behaviour.

> **Binding defaults to loopback (127.0.0.1)** — invisible to your LAN, no port
> exposed to coworkers, hotel WiFi or conference networks. If you genuinely need
> LAN-visible binding for an on-prem multi-user install, set
> `REVOLUZEN_FLOW_ALLOW_LAN=1` or pass `--host 0.0.0.0` explicitly. Full
> rationale: [docs/install/security-hardening.md](docs/install/security-hardening.md).

Environment variables: [`.env.example`](.env.example). Day-2 ops (upgrades,
backup, DR, troubleshooting): [docs/deployment.md](docs/deployment.md).

### As a background service

`serve` and `open` run in the **foreground** — fine while trying it out, but
they stop when you close the terminal. For an always-on install that survives
logout and reboot, register with your OS service manager. One command, every
platform:

```bash
python -m revoluzen_flow install-service     # register + start it supervised
python -m revoluzen_flow service-status      # is it running?
python -m revoluzen_flow uninstall-service   # remove it (does NOT delete data)
```

| OS | What it registers | Notes |
|---|---|---|
| **Windows** | Scheduled Task at logon (`schtasks`) | Runs hidden, restarts on crash — no NSSM or pywin32. 1.0.x registers with highest privileges, so run PowerShell as Administrator if you see `REGISTER_FAIL: Access is denied`. |
| **macOS** | launchd LaunchAgent | Auto-starts at login, `KeepAlive` restart. |
| **Linux** | user-mode `systemd` unit | `systemctl --user enable --now`; add `loginctl enable-linger $USER` to keep it running after logout. |

**On Docker instead?** You don't need this — `docker compose up -d` is already
detached, and the compose file's restart policy brings the container back on
reboot or crash.

> This is the same OS-native supervisor the (not-yet-shipped) desktop installers
> wrap, so power users get background operation today.

### Deploy to cloud

Runs on any platform that takes a Docker image. Config files are in the repo root:

| Platform | Config | Quick start |
|---|---|---|
| Fly.io | [`fly.toml`](fly.toml) | `fly launch --copy-config` |
| Render | [`render.yaml`](render.yaml) | New → Blueprint → connect this repo |
| Railway | [`railway.toml`](railway.toml) | New → Deploy from GitHub repo |

> **Persistence matters.** Cloud filesystems are ephemeral by default — every
> restart wipes the SQLite DB and all configuration. Each config above mounts
> `/data` to a managed volume so your work survives restarts. Free tiers have
> small disks; for production sizing see [docs/deployment.md](docs/deployment.md).

## Why Revoluzen Flow

Built for teams who want a local, visual ETL engine without dragging in a
warehouse-first platform or a heavyweight IDE.

| What you get | Why it matters |
|---|---|
| **DuckDB execution** | Joins, group-bys, pivots and aggregates run in a vectorised engine, with disk spill for larger local workloads. |
| **Operations in the box** | Scheduler, alerts, run history, per-step row counts, lineage view, versioning, deploy and rollback are included. |
| **Local-first install** | Runs on a laptop or VM. No cloud lock-in. Telemetry is off by default and opt-in only. |
| **Visual + code workflow** | Drag nodes on the canvas, use expressions for light logic, and drop into DuckDB SQL when the canvas is not enough. |
| **Optional AI assistance** | Claude, OpenAI, Gemini, Ollama and OpenRouter can help with node setup, SQL and error diagnosis; deterministic fallbacks keep the app useful without an LLM. |

> **Evaluating against another orchestrator?** See
> [docs/vs-talend.md](docs/vs-talend.md) for the side-by-side comparison.

## Connectors

Revoluzen Flow ships an open connector framework, not a closed vendor queue.
Current OSS includes native database and bulk-load connectors plus
REST-manifest SaaS connectors, each labeled by maturity: Production, Verified,
Beta, Experimental or Hidden. The picker shows stable tiers first; Experimental
sits behind a toggle. See [docs/connectors.md](docs/connectors.md) or
`GET /api/connectors/cert-matrix` for the live matrix.

Need one that isn't shipped yet?

| Path | Best when |
|---|---|
| Paste an OpenAPI URL | The vendor publishes an OpenAPI spec. |
| Paste sample API responses | The API has no public spec, but you have example payloads. |
| Hand-author a manifest | The API needs custom paging, auth or field mapping. |
| Request or contribute | The connector should become part of the OSS catalog. |

The first three live in **Insights → API Explorer**: load a spec (or any URL),
send one authenticated request, assert on the response, then generate and save
the connector — no restart, no compile step, no LLM.

No connector is Plus-gated. Every OSS manifest, node and extension path stays
open. See [docs/extend/build-a-connector.md](docs/extend/build-a-connector.md)
for the authoring tutorial.

## Steward reliability layer

Steward is a read-only observer for pipeline health. It detects duplicate
sources and pipelines, connector health, schema drift, volume anomalies,
data-quality threshold failures, empty node outputs, warehouse waste,
governance issues and user-defined YAML rules. Findings carry occurrence
counts, severity escalation, rebound detection, sanitized dismiss reasons and
notification de-duplication.

Steward does not mutate workflows and does not use an LLM to decide findings.
Its Memory Layer is gated: lessons are proposed first and stay inert until a
human approves them. See [docs/steward/overview.md](docs/steward/overview.md),
[docs/steward/positioning.md](docs/steward/positioning.md) and
[docs/steward/architecture.md](docs/steward/architecture.md).

## Editions

Revoluzen Flow is the open-source core for individuals and small teams — a solo,
single-workspace install. Revoluzen Flow Enterprise is a commercial layer adding
team workspaces, RBAC, two-gate approvals, audit logs, vault-backed credentials,
sandbox, drift detection, enterprise deployment and SLA-backed support.

See [edition-matrix.md](edition-matrix.md) for the per-feature breakdown.

## Install troubleshooting

<details>
<summary><strong>Windows: <code>revoluzen</code> is not recognized after install</strong></summary>

If `pip install revoluzen-flow` succeeds but PowerShell says
`The term 'revoluzen' is not recognized`, Revoluzen Flow installed correctly.
Windows just cannot find the generated `revoluzen.exe` because Python's
`Scripts` folder is not on `PATH`. This is a Windows/Python PATH setting, not a
server failure.

Use the Python launcher form:

```powershell
py -m pip install --upgrade revoluzen-flow
py -m revoluzen_flow open
```

If `py` is unavailable, use `python` instead. To confirm where it installed:

```powershell
py -m pip show revoluzen-flow
py -m site --user-base
```

The direct command works only when the matching Python `Scripts` folder is on
your user `PATH` (for example `<user-base>\Scripts`). After changing `PATH`,
close and reopen PowerShell or VS Code. Running
`C:\...\Scripts\revoluzen.exe open` proves the package installed, but
`py -m revoluzen_flow open` avoids the shortcut-path issue entirely.

</details>

<details>
<summary><strong>Intel Macs (x86_64): <code>openssl-sys: Could not find directory of OpenSSL installation</code></strong></summary>

`cryptography` 49+ publishes macOS wheels for Apple Silicon only, so on an Intel
Mac pip falls back to a source build needing a Rust toolchain and OpenSSL
headers. Revoluzen Flow 1.0.1 caps that dependency to the last
Intel-compatible release, so a current install just works.

If you are on 1.0.0 or earlier:

```bash
python3 -m pip install "cryptography<49"
python3 -m pip install --upgrade revoluzen-flow
```

</details>

<details>
<summary><strong><code>error: externally-managed-environment</code></strong></summary>

Homebrew and Debian Pythons refuse system-wide installs. Use a virtualenv:

```bash
python3 -m venv ~/revoluzen-flow-venv
source ~/revoluzen-flow-venv/bin/activate
pip install --upgrade revoluzen-flow
```

</details>

<details>
<summary><strong>Corporate proxy: <code>SSL: CERTIFICATE_VERIFY_FAILED</code></strong></summary>

If `pip install` — or the first `docker compose up` build, which runs `pip`
*inside* the image — fails with
`self-signed certificate in certificate chain`, that's your network's
TLS-inspecting proxy, not Revoluzen Flow. It breaks *any* Python install on that
network. Fixes, best first:

1. **Trust your org's root CA (proper fix).** Get the cert from IT, then
   `pip config set global.cert C:\path\to\corp-ca.pem` and point
   `REQUESTS_CA_BUNDLE` at the same file. For the Docker build, bake the CA into
   the image.
2. **Pull a prebuilt image (cleanest, once published).** A single TLS handshake
   your Docker Desktop proxy settings already handle — no in-network `pip` build.
3. **Quick unblock (less strict).**
   `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e .`

</details>

Full install troubleshooting lives in [docs/quickstart.md](docs/quickstart.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

"Revoluzen Flow" and "Revoluzen" are trademarks of Revoluzen. See
[TRADEMARK.md](TRADEMARK.md) for usage policy.

## Contributing

**External pull requests are paused for v1.0.0** while our Contributor License
Agreement clears legal review — we won't ask you to sign a draft. Bug reports,
connector and node requests, questions and design feedback are all open and
wanted, and Revoluzen Flow is Apache 2.0 so forking is always yours. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the details and for how to park a fix
you've already written.

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) has the rules of the road.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please do not file public
issues for security reports.

## Links

- **Documentation** — in-repo at [docs/](docs/), start with [docs/quickstart.md](docs/quickstart.md). A hosted site at `docs.revoluzen.io/flow` is on the launch checklist.
- **Enterprise** — `revoluzen.io/flow`, or email <hello@revoluzen.io>
- **Issues** — GitHub Issues on this repo
- **Contact** — <hello@revoluzen.io>
