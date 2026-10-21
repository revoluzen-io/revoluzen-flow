<p align="center">
  <a href="https://revoluzen.io"><img src="docs/assets/revoluzen-logo.png" alt="Revoluzen" width="88" height="88"></a>
</p>

<h1 align="center">Revoluzen Flow</h1>

<p align="center">
  Built and maintained by <a href="https://revoluzen.io"><strong>Revoluzen</strong></a>
</p>

<p align="center">
  <a href="https://github.com/revoluzen-technologies/revoluzen-flow/actions/workflows/ci.yml"><img src="https://github.com/revoluzen-technologies/revoluzen-flow/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/revoluzen-technologies/revoluzen-flow/actions/workflows/security-scan.yml"><img src="https://github.com/revoluzen-technologies/revoluzen-flow/actions/workflows/security-scan.yml/badge.svg?branch=main" alt="Security Scan"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License: Apache 2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/status-1.0.1-blue" alt="Status: 1.0.1"></a>
</p>

Local-first data pipeline engine. `pip install revoluzen-flow`, `python -m revoluzen_flow open` — backend boots on loopback, browser opens, you're in. Vectorised DuckDB engine, built-in scheduler + alerts + run history, 40 node types, embedded AI assistance with a privacy-preserving local default, and an open connector framework you can extend in minutes. Apache 2.0 forever; predictable seat pricing for teams via Revoluzen Flow Enterprise.

> **Status:** 1.0.1 stable. Tested with Python 3.11/3.12 · Docker 25+ · DuckDB 1.1.3 · Postgres 16. See [CHANGELOG.md](CHANGELOG.md) for the full tested-with matrix and known gaps.
>
> **Install today: PyPI, Docker, or source** — all are supported
> ([Quick start](#quick-start)). `pip install revoluzen-flow` is published on PyPI;
> prebuilt desktop installers are still pending.

## Why Revoluzen Flow

Revoluzen Flow is built for teams who want a local, visual ETL engine without dragging
in a warehouse-first platform or a heavyweight IDE.

| What you get | Why it matters |
|---|---|
| **DuckDB execution** | Joins, group-bys, pivots and aggregates run in a vectorised engine, with disk spill for larger local workloads. |
| **Operations in the box** | Scheduler, alerts, run history, per-step row counts, lineage view, versioning, deploy and rollback are included. |
| **Local-first install** | Runs on a laptop or VM. No cloud lock-in. Telemetry is off by default and opt-in only. |
| **Visual + code workflow** | Drag nodes on the canvas, use expressions for light logic, and drop into DuckDB SQL when the canvas is not enough. |
| **Optional AI assistance** | Claude, OpenAI, Gemini, Ollama and OpenRouter can help with node setup, SQL and error diagnosis; deterministic fallbacks keep the app useful without an LLM. |

**Connector model.** Revoluzen Flow ships an open connector framework, not a closed
vendor queue. Current OSS includes native database/bulk-load connectors plus
REST-manifest SaaS connectors, each labeled by maturity: Production, Verified,
Beta, Experimental or Hidden. The picker shows stable tiers first; Experimental
connectors sit behind a toggle. See [docs/connectors.md](docs/connectors.md)
or `GET /api/connectors/cert-matrix` for the live matrix.

Need a connector that is not shipped yet?

| Path | Best when |
|---|---|
| Paste an OpenAPI URL | The vendor publishes an OpenAPI spec. |
| Paste sample API responses | The API has no public spec, but you have example payloads. |
| Hand-author a manifest | The API needs custom paging, auth or field mapping. |
| Request or contribute | The connector should become part of the OSS catalog. |

The first three paths all live in **Insights → API Explorer**: load a spec (or any URL), send one authenticated request, assert on the response, then generate and save the connector — no restart, no compile step, no LLM.

No connector is Plus-gated. Every OSS manifest, node and extension path remains
open. See [docs/extend/build-a-connector.md](docs/extend/build-a-connector.md)
for the connector authoring tutorial.

**Steward reliability layer.** Revoluzen Flow Steward is a read-only observer for
pipeline health. It detects duplicate sources/pipelines, connector health,
schema drift, volume anomalies, data-quality threshold failures, empty node
outputs, warehouse waste, governance issues and user-defined YAML rules. Findings
carry occurrence counts, severity escalation, rebound detection, sanitized
dismiss reasons and notification de-duplication.

Steward does not mutate workflows and does not use an LLM to decide findings.
Its Memory Layer is gated: lessons are proposed first and stay inert until a
human approves them. See [docs/steward/overview.md](docs/steward/overview.md),
[docs/steward/positioning.md](docs/steward/positioning.md) and
[docs/steward/architecture.md](docs/steward/architecture.md).

**OSS vs Plus.** OSS is a solo / single-workspace install. Revoluzen Flow Enterprise adds the
team layer: multi-user workspaces, RBAC, approval flows, enterprise deployment,
monitoring and advanced governance surfaces.

> **Evaluating against another orchestrator?** See [docs/vs-talend.md](docs/vs-talend.md) for the side-by-side comparison.

## Quick start

Revoluzen Flow is a **self-hosted server + web app** (a backend that serves a browser
UI), not a desktop program. So the recommended path on **any OS** is Docker —
the *same one command* on Windows, macOS, and Linux. Pick what fits you:

| You are | Recommended path | Time |
|---|---|---|
| **On Windows / macOS / Linux — want it to just work** | **PyPI install** below (needs Python 3.11+) or **Docker Compose** | 2-5 min |
| **On Linux, and you'd rather not run Docker** | **Linux package** below — `.deb` / `.rpm` / `.AppImage` (no Python, no Docker on the box) | 2 min |
| **A developer / contributor** | **From source** below (Python 3.11+, Node 20+) | 10 min |

**Windows / macOS — where's the native installer?** For a self-hosted server
app, the standard way to run it on Windows or macOS is **Docker Desktop** (the
Docker Compose path above) — that's how comparable tools (Grafana, Metabase,
Gitea, Nextcloud…) ship, and it works identically everywhere. A native `.exe` /
`.pkg` *builds and works*, but we don't publish it: unsigned, it trips Windows
SmartScreen / macOS Gatekeeper with a scary dialog, and we won't hand you that.
Want the native app anyway? Build it yourself in one command — see
[`installer/readme.md`](installer/readme.md) — and click through the warning.
Signed installers land once we have an EV certificate + Apple notarization.

### PyPI install

```powershell
# Windows: avoids Python Scripts/PATH shortcut issues
py -m pip install --upgrade revoluzen-flow
py -m revoluzen_flow open
```

```bash
# macOS / Linux
python -m pip install --upgrade revoluzen-flow
python -m revoluzen_flow open
```

> **Intel Macs (x86_64).** `cryptography` 49+ publishes macOS wheels for Apple
> Silicon only, so on an Intel Mac pip falls back to a source build that needs
> a Rust toolchain and OpenSSL headers — it fails with `openssl-sys: Could not
> find directory of OpenSSL installation`. Revoluzen Flow 1.0.1 caps that dependency
> to the last Intel-compatible release, so a current install just works. If you
> are pinned to 1.0.0, run `pip install "cryptography<49"` first.

`python -m revoluzen_flow open` defaults to port `8001`, but if that port is already in use it
prints the alternate URL it selected, for example `http://127.0.0.1:8003`.
Open the printed URL, not necessarily `http://localhost:8001`.

The health endpoint is `/api/health`:

```bash
curl http://localhost:8001/api/health
```

`revoluzen open` is optional shorthand. If `revoluzen` is not found after install,
use the same Python that installed it:

```bash
python -m revoluzen_flow open
```

**Windows: `revoluzen` is not recognized after install**

If `pip install revoluzen-flow` succeeds but PowerShell says:

```text
The term 'revoluzen' is not recognized
```

Revoluzen Flow is usually installed correctly. Windows just cannot find the generated
`revoluzen.exe` command because Python's `Scripts` folder is not on `PATH`.
This is a Windows/Python PATH setting, not an Revoluzen Flow server failure.

Use the Python launcher form first:

```powershell
py -m pip install --upgrade revoluzen-flow
py -m revoluzen_flow open
```

If `py` is unavailable:

```powershell
python -m pip install --upgrade revoluzen-flow
python -m revoluzen_flow open
```

To confirm where it installed:

```powershell
py -m pip show revoluzen-flow
py -m site --user-base
```

The direct command works only when the matching Python `Scripts` folder is on
your user `PATH`, for example:

```text
<user-base>\Scripts
```

After changing `PATH`, close and reopen PowerShell or Visual Studio Code. You
can always keep using `py -m revoluzen_flow open`; it avoids the shortcut-path issue
entirely. Running `C:\...\Scripts\revoluzen.exe open` proves the package installed,
but `py -m revoluzen_flow open` is the cleaner command to give new Windows users.

**Not on Docker Hub yet** (we'd rather say so than send you to a 404):

| Not yet available | Use this instead |
|---|---|
| `docker pull revoluzen/flow` | Not on Docker Hub yet, but **you don't need it** — `docker compose up` builds it locally on first run (see below). |

> **Behind a corporate / office proxy?** If `pip install` — or the first-time
> `docker compose up` build, which runs `pip` *inside* the image — fails with
> `SSL: CERTIFICATE_VERIFY_FAILED … self-signed certificate in certificate chain`,
> that's your network's TLS-inspecting proxy, not Revoluzen Flow. It breaks *any* Python
> install on that network. Fixes, best first:
> 1. **Trust your org's root CA (proper fix).** Get the cert from IT, then
>    `pip config set global.cert C:\path\to\corp-ca.pem` and point
>    `REQUESTS_CA_BUNDLE` at the same file. For the Docker build, bake the CA into
>    the image — or just pull a prebuilt one (next point).
> 2. **Pull a prebuilt image (cleanest, once published).** `docker pull` is a
>    single TLS handshake your Docker Desktop proxy settings already handle — no
>    in-network `pip` build at all.
> 3. **Quick unblock (less strict).**
>    `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e .`

### Linux package — `.deb` / `.rpm` / `.AppImage`

Bundles a frozen Python runtime and the compiled UI — no Python, no Node,
no Docker needed on the target machine. After install, Revoluzen Flow registers
itself as a systemd **user** service that starts at login and survives
reboots.

Download from the [latest GitHub Release](https://github.com/revoluzen-technologies/revoluzen-flow/releases/latest):

| Distro | Package | Install |
|---|---|---|
| Debian / Ubuntu 22.04+ | `revoluzen_flow_1.0.0_amd64.deb` | `sudo apt install ./revoluzen_flow_1.0.0_amd64.deb` |
| Fedora / RHEL / Alma / Rocky 9+ | `revoluzen-flow-1.0.0-1.x86_64.rpm` | `sudo dnf install ./revoluzen-flow-1.0.0-1.x86_64.rpm` |
| Any Linux (glibc 2.27+) | `RevoluzenFlow-1.0.0-x86_64.AppImage` | `chmod +x ./RevoluzenFlow-*.AppImage && ./RevoluzenFlow-*.AppImage` |

> **These packages are not GPG-signed**, so `apt` / `dnf` will note that the
> package is unauthenticated. Verify what you downloaded against
> `SHA256SUMS` on the release: `sha256sum -c SHA256SUMS`. That proves the
> file arrived intact, not who built it — signing is on the list.

After install, open <http://localhost:8001> — the service is already running.

**Manage the service** (any OS, same commands):

```bash
python -m revoluzen_flow service-status        # is it running?
python -m revoluzen_flow uninstall-service     # stop + deregister (does NOT delete data)
python -m revoluzen_flow install-service       # re-register after an update
```

Building these installers yourself (CI / private builds): see
[`installer/readme.md`](installer/readme.md).

### PyPI install

**Requires Python 3.11+.** Check first — an older Python fails with `No matching
distribution found`: `py --version` (Windows) or `python3 --version` (macOS/Linux).

```powershell
# Windows: the py launcher avoids Python Scripts/PATH shortcut issues
py -m pip install --upgrade revoluzen-flow
py -m revoluzen_flow open
```

```bash
# macOS / Linux — modern macOS ships python3, not python
python3 -m pip install --upgrade revoluzen-flow
python3 -m revoluzen_flow open
```

If PowerShell says `revoluzen` is not recognized but running
`C:\...\Scripts\revoluzen.exe open` works, Revoluzen Flow installed correctly. The
generated shortcut is just not on `PATH`; keep using `py -m revoluzen_flow open` or
add Python's `Scripts` folder to your user `PATH`.

On Homebrew/Debian Python you may hit `error: externally-managed-environment` —
install into a virtualenv instead: `python3 -m venv ~/revoluzen-flow-venv && source
~/revoluzen-flow-venv/bin/activate && pip install --upgrade revoluzen-flow`. Full install
troubleshooting is in [docs/quickstart.md](docs/quickstart.md).

**Intel Macs (x86_64).** `cryptography` 49+ publishes macOS wheels for Apple
Silicon only, so pip falls back to a source build that needs Rust and OpenSSL
headers and fails with `openssl-sys: Could not find directory of OpenSSL
installation`. Revoluzen Flow caps that dependency to the last Intel-compatible
release on this platform. If you installed a build from before the cap
(`revoluzen` 1.0.0 or earlier), pull the working version first:

```bash
python3 -m pip install "cryptography<49"
python3 -m pip install --upgrade revoluzen-flow
```

### Docker Compose

```bash
git clone https://github.com/revoluzen-technologies/revoluzen-flow.git
cd revoluzen-flow
cp .env.example .env            # optional — defaults work
docker compose up -d            # Revoluzen Flow only
# or:  docker compose --profile ai up -d   # Revoluzen Flow + local Ollama
```

Open <http://localhost:8001> — on first launch Revoluzen Flow asks you to **create your admin account** (your email + a strong password; this first account owns the instance). Then open **Templates → First pipeline — CSV in, CSV out → Use Template → Run**. You'll see real output from a seeded sample dataset in under a minute — no Postgres, no API keys, no cloud account required.

> **Automating a headless deploy** that can't do the interactive first run? Set `REVOLUZEN_FLOW_BOOTSTRAP_ADMIN=1` and Revoluzen Flow auto-creates `admin@revoluzen.local` with a random password — read it with `docker compose exec flow cat /data/INITIAL_ADMIN_PASSWORD.txt`, sign in, and rotate it.

To upgrade later:

```bash
docker compose pull && docker compose up -d
```

> **Heads-up — `revoluzen/flow:1.0.0` is built locally, not pulled.** The
> image is not on Docker Hub yet. `docker-compose.yml` declares
> `build: context: .`, so `docker compose up` auto-builds it from the
> `Dockerfile` at repo root on first run — ~3-5 min, one-time, no
> `pull access denied` despite the `revoluzen/flow` name. `docker compose
> pull` will fail until the image is published; use `docker compose build`
> to pick up changes.

### Single-container alternative (`docker run`)

If you don't want compose, one container is enough — but **build the image
first**. Unlike compose, `docker run` has no build step and would try to
pull an image that isn't published yet:

```bash
docker build -t revoluzen/flow:1.0.0 .
docker run -d --name revoluzen-flow \
  -p 8001:8001 \
  -v revoluzen_flow_data:/data \
  -e REVOLUZEN_FLOW_DATA_DIR=/data \
  revoluzen/flow:1.0.1
```

Then open <http://localhost:8001>.

### From source (Python 3.11+ and Node 20+)

`pip install -e .` and `python -m revoluzen_flow open` are **two separate commands** — install first,
then run. The `.` means "the project in the current folder", so you must be
**inside the cloned repo** when you run it. `revoluzen` only exists *after* the
install succeeds (it's the optional command shortcut that install creates).

**macOS / Linux:**

```bash
git clone https://github.com/revoluzen-technologies/revoluzen-flow.git
cd revoluzen-flow

# 1. Build the UI (one-time). The built bundle isn't committed — skip this
#    and the API runs but / serves nothing.
cd frontend && npm ci && npm run build && cd ..

# 2. Install into a venv, then run.
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m revoluzen_flow open
```

**Windows (PowerShell)** — run these **one line at a time**; `&&` chaining and
`source` are not valid in Windows PowerShell:

```powershell
git clone https://github.com/revoluzen-technologies/revoluzen-flow.git
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

> **Windows tip:** if `Activate.ps1` is blocked by execution policy, don't fight
> it — just call the venv's executables directly instead:
> `.\.venv\Scripts\pip.exe install -e .` then `.\.venv\Scripts\python.exe -m revoluzen_flow open`.

> Skipping step 1 gets you a working API and a blank page. Revoluzen Flow will say
> so loudly at startup (`no frontend build found …`) rather than leaving you
> to guess.

The `python -m revoluzen_flow open` command starts the backend on a free port (defaults to 8001, falls back if in use) and opens your default browser to the local URL. If the output says `port 8001 was in use — using 8003 instead`, open the printed URL (`http://127.0.0.1:8003` in that example). No need to type or remember a URL.

If you prefer the manual flow:
```bash
python -m revoluzen_flow serve        # starts on http://127.0.0.1:8001, you open the browser yourself
python -m revoluzen_flow serve --open # same as `python -m revoluzen_flow open`
python -m revoluzen_flow serve --port 9000
```

Headless / WSL2 / Docker / DevContainer / remote-SSH users: `python -m revoluzen_flow open` detects these automatically, skips the browser auto-launch, and prints the URL prominently so you can paste it into a browser on your host machine. You can also pass `--no-open` to force-skip the auto-launch while keeping the friendly port-fallback behaviour.

> **Bind defaults to loopback (127.0.0.1)** — invisible to your LAN, no port exposure to coworkers / hotel WiFi / conference networks. If you genuinely need LAN-visible binding for an on-prem multi-user install, set `REVOLUZEN_FLOW_ALLOW_LAN=1` or pass `--host 0.0.0.0` explicitly. Full rationale: [docs/install/security-hardening.md](docs/install/security-hardening.md).

For environment-variable reference see [`.env.example`](.env.example).
For day-2 ops (upgrades, backup, DR, troubleshooting) see the canonical
runbook: [docs/deployment.md](docs/deployment.md).

### Run it in the background (as a service)

`python -m revoluzen_flow serve` / `python -m revoluzen_flow open` run in the **foreground** — fine while you're
trying it out, but they stop the moment you close the terminal. For an
always-on install that survives terminal close, logout, and reboot, register
Revoluzen Flow with your OS service manager. **One command, every platform:**

```bash
python -m revoluzen_flow install-service     # register + start it supervised in the background
python -m revoluzen_flow service-status      # check whether it's running
python -m revoluzen_flow uninstall-service   # remove it
```

| OS | What it registers | Notes |
|---|---|---|
| **Windows** | Scheduled Task at logon (`schtasks`) | runs hidden, restarts on crash — no NSSM / pywin32. Current 1.0.x builds register with highest privileges, so run PowerShell as Administrator if you see `REGISTER_FAIL: Access is denied`. |
| **macOS** | launchd LaunchAgent | auto-starts at login, `KeepAlive` restart |
| **Linux** | user-mode `systemd` unit | `systemctl --user enable --now`; add `loginctl enable-linger $USER` to keep it running after you log out |

**On Docker instead?** You don't need this — `docker compose up -d` is already
detached, and the compose file's restart policy brings the container back on
reboot/crash.

> This is the same OS-native supervisor the (not-yet-shipped) desktop
> installers wrap under the hood — so power users get background operation
> today without waiting for the `.msi` / `.pkg` / `.deb`.

### Deploy to cloud

Revoluzen Flow runs on any platform that takes a Docker image. Config files
for the most common ones are in the repo root:

| Platform | Config | Quick start |
|---|---|---|
| Fly.io   | [`fly.toml`](fly.toml)         | `fly launch --copy-config` |
| Render   | [`render.yaml`](render.yaml)   | New → Blueprint → connect this repo |
| Railway  | [`railway.toml`](railway.toml) | New → Deploy from GitHub repo |

> **Persistence matters.** Cloud filesystems are ephemeral by default —
> every restart wipes the SQLite DB and all configuration. Each config
> above mounts `/data` to a managed volume so your work survives
> restarts. Free tiers have small disks; for production sizing see
> [docs/deployment.md](docs/deployment.md).

## What's in Revoluzen Flow vs Revoluzen Flow Enterprise

Revoluzen Flow is the open-source core for individuals and small teams.
Revoluzen Flow Enterprise is a commercial layer adding team workspaces, RBAC, two-gate
approvals, audit logs, vault-backed credentials, sandbox, drift detection,
and SLA-backed support.

See [edition-matrix.md](edition-matrix.md) for the full per-feature breakdown.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

"Revoluzen Flow" and "Revoluzen" are trademarks of Revoluzen.
See [TRADEMARK.md](TRADEMARK.md) for usage policy.

## Contributing

**External pull requests are paused for v1.0.0** while our Contributor
License Agreement clears legal review — we won't ask you to sign a draft.
Bug reports, connector/node requests, questions and design feedback are all
open and wanted, and Revoluzen Flow is Apache 2.0 so forking is always yours.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the details and for how to park a
fix you've already written.

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) has the rules of the road.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please do not file
public issues for security reports.

## Links

- Documentation: in-repo at [docs/](docs/) (start with [docs/quickstart.md](docs/quickstart.md)). A hosted site at `docs.revoluzen.io/flow` is on the launch checklist.
- Revoluzen Flow Enterprise commercial: `revoluzen.io/flow` — email hello@revoluzen.io for details
- Issues: GitHub Issues on this repo
- Contact: hello@revoluzen.io
