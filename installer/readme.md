# Revoluzen Flow — Distribution & Installer Recipes

This directory holds the per-OS recipes Revoluzen uses to ship
**packaged installers** of Revoluzen Flow to end users who don't have
Python or Node set up.

Four audiences, four install paths:

| Audience | Install path | Where it lives |
|---|---|---|
| Anyone on Linux, nothing pre-installed | `curl … install.sh \| sh` | `install.sh` (this directory) |
| Developer (any OS) | `pip install -e .` then `revoluzen serve` | repo root + `pyproject.toml` |
| Power user (any OS) | `pip install revoluzen-flow` then `revoluzen install-service` | `backend/revoluzen_flow/cli/install_service.py` |
| Non-technical end user | One-click installer — **`.exe` / `.pkg` / `.deb`** | **this directory** |

`pip install` can never be a "no Python needed" path — `pip` *is* Python. The
bootstrap scripts are what deliver that; they download a release asset that
carries its own runtime.

For ops folks: Docker Compose remains the supported deployment path
(`docker-compose.yml` at repo root).

---

## ⚠ Three spellings, and they are not interchangeable

Four separate build defects have come from confusing these, all introduced by
one rebrand find/replace. Before editing anything here, know which is which:

| Spelling | What it names | Appears in |
|---|---|---|
| `revoluzen_flow` | the **import package** | `--collect-all`, `--hidden-import`, `pathex`, entry script path |
| `revoluzen-flow` | the **dist name** and **output folder** | PyPI name, `dist/revoluzen-flow/`, `.iss` source glob |
| `revoluzen` | the **executable / console script** | `revoluzen.exe`, `[project.scripts]`, `MyAppExeName` |

The failure mode is nastier than a crash. `collect_all("revoluzen-flow")` does
**not** raise — PyInstaller logs `WARNING: … is not a package` and returns
nothing. The build then succeeds and ships an executable with a working Python
and **none of the application's 548 data files**: no connector manifests, no
bundled UI, no seed data. It installs cleanly and fails at runtime in ways that
look nothing like a packaging problem.

`revoluzen-flow.spec` now asserts the entry script exists at parse time, and CI
asserts the bundle contains `connectors/manifests`, `frontend_dist` and
`seed_data` on both Windows and macOS. Keep both guards.

---

## What CI builds, and what it publishes

These are different lists on purpose.

| Platform | Built on every tag | Published to the release | Why |
|---|---|---|---|
| Linux `.deb` | ✅ | ✅ | apt warns about an unsigned package and the user decides — the normal path |
| Linux `.rpm` | ✅ | ✅ | same |
| Linux AppImage | ✅ | ✅ | same; also what `install.sh` downloads |
| Windows freeze | ✅ | ❌ | unsigned, SmartScreen shows a red **block** dialog |
| macOS `.pkg` | ✅ | ❌ | unsigned, Gatekeeper refuses to open it at all |

Windows and macOS are built and smoke-tested on every tag even though nothing
ships, because "we don't publish it" had silently become "nobody builds it",
and both scripts were broken for an unknown length of time. A red check is
cheaper than finding out on the day the certificates arrive.

To start publishing them: obtain the certs (below), add the signing steps, then
add the artifacts to the `files:` list in `github-release`. Nothing else needs
to change.

---

## What the packaged installers do

Each installer bundles:

1. **A frozen Python runtime** — so the user doesn't need to install
   Python 3.11+. Built via PyInstaller (`--onedir` mode for fast
   startup + small per-update deltas).
2. **The compiled frontend** — `frontend/dist/` produced by
   `npm run build`. The backend serves it as static at `/`.
3. **A first-run wizard** — opens the browser to
   `http://localhost:8001/` after install completes.
4. **OS-native service registration** — internally calls
   `revoluzen install-service` so the app auto-starts at logon /
   reboot and survives crashes.
5. **A clean uninstaller** — removes the binary, deregisters the
   service, OPTIONALLY removes the data dir (user is prompted).

---

## The one-line installers (`install.sh`, `install.ps1`)

These are not built; they are served as-is and run on the user's machine.

```bash
curl -fsSL https://raw.githubusercontent.com/revoluzen-io/revoluzen-flow/main/installer/install.sh | sh
```

```powershell
irm https://raw.githubusercontent.com/revoluzen-io/revoluzen-flow/main/installer/install.ps1 | iex
```

The raw URLs work the moment the repository is public — no DNS, no hosting.
`get.revoluzen.io/install.{sh,ps1}` is the intended vanity alias; point it at
these files and the commands shorten.

What they do: resolve the latest release through the GitHub API, download the
matching asset, **verify it against that release's `SHA256SUMS` and abort if the
sum is missing, unlisted or wrong**, install per-user (no root/admin), and print
how to put the binary on `PATH` — `install.sh` never edits a shell profile;
`install.ps1` sets the *user* PATH and says so.

Honest state today:

| Platform | Works | Notes |
|---|---|---|
| Linux x86_64 | ✅ once a tag is pushed | downloads the AppImage — one file, no root |
| Linux arm64 | ❌ | `build-appimage.sh` hardcodes `ARCH=x86_64`; the script says so and stops |
| macOS | ❌ | no signed package exists; the script explains and points at pipx/Docker |
| Windows | ❌ | same, waiting on an EV certificate |

Each unsupported case exits with a specific reason and a working alternative,
rather than a 404 against a URL that was never published.

Both are linted on every CI run (`installer-scripts` job in `ci.yml`): `sh -n`,
`shellcheck`, a PowerShell parse, and an assertion that the three checksum
guard clauses are still present — so nobody can quietly soften "refuse" into
"warn". Note that the shell script **cannot be syntax-checked on a Windows dev
box**: starting Git-for-Windows `sh`, even as `sh -n`, reliably fork-bombs that
machine. Let CI do it.

**Prerequisite for either to work at all:** a tag has to be pushed so
`release.yml` runs and produces assets. As of 2026-10-28 the repository has
local tags `v1.0.0` / `v1.0.1` that were never pushed, no remote tags, and
therefore no releases — the pipeline below has never executed.

## Build recipes per OS

### Windows — Inno Setup `.exe`

```
installer/windows/
├── revoluzen-flow.iss             # Inno Setup script — input
├── build.ps1              # one-command build wrapper
├── icons/                 # branding (the .ico file Inno embeds)
└── output/                # RevoluzenFlow-Setup-1.0.0.exe lands here
```

Pipeline:

```powershell
# Prereqs (one-time): Inno Setup 6 from https://jrsoftware.org/isinfo.php
# Add Inno Setup's ISCC.exe to PATH.

cd installer\windows
.\build.ps1
# Produces: output\RevoluzenFlow-Setup-1.0.0.exe
```

> **Size.** Measured 2026-10-28 on a real `--onedir` freeze: the staged
> bundle is **630 MB across 8,474 files**, which Inno's LZMA2 compresses to
> roughly 150–200 MB of installer. An earlier revision of this page said
> "~50 MB"; that was never measured and is out by more than an order of
> magnitude.
>
> Nearly all of it is three wheels — pyarrow (~85 MB), pandas (~66 MB) and
> numpy (~32 MB) — before their transitive native libraries. If the download
> size matters, that is where to look first: DuckDB reads and writes Parquet
> natively, so pyarrow may be reducible to an optional extra rather than a
> bundled default. That is a measurement exercise, not a certainty.

What `build.ps1` does:

1. Cleans `dist/`
2. Runs `pyinstaller --onedir backend/revoluzen_flow/__main__.py`
   pinned to Python 3.12 from `..\..\.venv` so the bundled runtime
   matches CI.
3. Runs `npm run build` in `frontend/` so `frontend/dist/` is fresh.
4. Calls `ISCC.exe revoluzen-flow.iss` to compile the installer.
5. Optionally signs the result (`signtool.exe`) if `-CertThumbprint`
   is passed.

The installer ships a "Run Revoluzen Flow at logon" checkbox in its Tasks
page. Checking it makes the post-install step run
`revoluzen install-service` to register the Scheduled Task.

### macOS — `.pkg` via `pkgbuild` + `productbuild`

```
installer/macos/
├── build.sh               # one-command build (runs on macOS 12+)
├── scripts/
│   ├── preinstall         # POSIX shell — stop running service
│   └── postinstall        # POSIX shell — run revoluzen install-service
├── component.plist
└── distribution.xml       # productbuild distribution
```

Pipeline:

```bash
cd installer/macos
./build.sh
# Produces: output/RevoluzenFlow-1.0.0.pkg
```

What `build.sh` does:

1. Cleans `build/` + `output/`.
2. Builds the frontend (`cd ../../frontend && npm run build`).
3. Freezes the backend with PyInstaller into
   `build/payload/RevoluzenFlow.app/Contents/Resources/`.
4. Runs `pkgbuild --root build/payload --identifier com.revoluzen.flow ...`
5. Wraps with `productbuild --distribution distribution.xml ...`.
6. Optionally signs (`--sign "Developer ID Installer: Revoluzen ..."`)
   and notarizes (`xcrun notarytool submit ...`).

The `postinstall` script calls `/Applications/RevoluzenFlow.app/Contents/MacOS/revoluzen install-service`,
which writes the LaunchAgent under `~/Library/LaunchAgents/`.

### Linux — `.deb` (Debian/Ubuntu) and `.rpm` (RHEL/Fedora/SUSE)

```
installer/linux/
├── build-deb.sh           # produces revoluzen_flow_1.0.0_amd64.deb
├── build-rpm.sh           # produces revoluzen-flow-1.0.0.x86_64.rpm
├── build-appimage.sh      # produces RevoluzenFlow-1.0.0-x86_64.AppImage
└── debian/                # .deb metadata (control, postinst, prerm, ...)
```

Pipeline:

```bash
cd installer/linux
./build-deb.sh          # apt-installable on Ubuntu/Debian
./build-rpm.sh          # dnf-installable on Fedora/RHEL
./build-appimage.sh     # works on every glibc 2.27+ distro
```

The `postinst` (DEB) / `%post` (RPM) script calls
`revoluzen install-service` as the invoking user.

---

## Why we DIDN'T pick a single cross-platform installer framework

| Framework | Why we passed |
|---|---|
| **Electron Builder** | We're a Python+SPA, not a JS-on-Desktop app. Adds Chromium to the bundle. |
| **PyInstaller alone** | Produces a binary, not an installer. Needs OS-specific wrapping. |
| **InstallForge / NSIS** | Windows-only. |
| **Snap** | Linux-only, requires snapd, doesn't fit on RHEL. |
| **Flatpak** | Linux-only, sandbox model conflicts with localhost-binding requirement. |

Per-OS native wrappers (Inno / pkgbuild / dpkg-deb) give the best
"feels like a normal app" experience on each platform, at the cost
of three build pipelines instead of one. We accept that cost.

---

## Update story

Two paths, depending on how the user installed:

| Install path | Update command |
|---|---|
| `pip install revoluzen-flow` | `pip install -U revoluzen-flow` |
| Packaged installer | Download new installer; run it. The installer detects the existing install, stops the service, swaps files, restarts. |
| Docker | `docker compose pull && docker compose up -d` |

For Plus tier (commercial), in-app auto-update is on the roadmap —
the OSS posture is "user controls when to update."

---

## Code signing & notarization checklist

We don't ship unsigned binaries to end users. Per-OS:

- **Windows**: EV code-signing cert. SmartScreen reputation builds
  up over the first ~1000 downloads — expect SmartScreen warnings
  on the first release until enough installs accumulate.
- **macOS**: Developer ID Application + Developer ID Installer
  certs. Notarize with `xcrun notarytool submit --wait`. Without
  this, Gatekeeper blocks first-launch with "unidentified developer."
- **Linux**: GPG-sign the `.deb` / `.rpm` so `apt-key` / `rpm
  --import` verify the signature.

The signing keys are NOT in this repo. Build scripts read them
from env vars (`REVOLUZEN_FLOW_WIN_CERT_THUMBPRINT`, `REVOLUZEN_FLOW_MACOS_SIGN_IDENTITY`,
`REVOLUZEN_FLOW_GPG_KEY_ID`).

---

## Where each path puts files (for reference)

| OS | Binary | Data dir | Service definition | Logs |
|---|---|---|---|---|
| **Windows** | `%PROGRAMFILES%\RevoluzenFlow\` | `%LOCALAPPDATA%\RevoluzenFlow\data` | Scheduled Task `RevoluzenFlow` | `%LOCALAPPDATA%\RevoluzenFlow\logs\` |
| **macOS** | `/Applications/RevoluzenFlow.app/` | `~/Library/Application Support/RevoluzenFlow` | `~/Library/LaunchAgents/com.revoluzen.flow.plist` | `~/Library/Logs/RevoluzenFlow/` |
| **Linux** | `/usr/lib/revoluzen-flow/` + symlink in `/usr/bin/revoluzen` | `~/.local/share/revoluzen-flow` | `~/.config/systemd/user/revoluzen-flow.service` | `journalctl --user -u revoluzen-flow` |

These paths match what `revoluzen install-service` / `_default_data_dir()`
in `backend/revoluzen_flow/cli/install_service.py` use, so the CLI-installed
and packaged-installed deployments are interchangeable.
