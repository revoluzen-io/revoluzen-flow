<#
.SYNOPSIS
    Revoluzen Flow — one-line installer for Windows.

.DESCRIPTION
    irm https://raw.githubusercontent.com/revoluzen-io/revoluzen-flow/main/installer/install.ps1 | iex

    That URL works as soon as the repository is public — no DNS, no
    hosting. https://get.revoluzen.io/install.ps1 is the intended vanity
    alias; point it at this file and the command shortens to
    `irm https://get.revoluzen.io/install.ps1 | iex`.

    Installs per-user under %LOCALAPPDATA%. No admin, no Python, no
    package manager: the executable it fetches carries its own Python
    runtime, so nothing needs to be present on the machine first.

    Read before you pipe this to iex. `irm ... | iex` runs whatever that
    URL serves at that moment, and a product that argues for sovereignty
    should not ask you to skip that check. To inspect first:

        irm https://get.revoluzen.io/install.ps1 -OutFile install.ps1
        notepad install.ps1 ; .\install.ps1

.PARAMETER Version
    Release tag to install. Defaults to the latest release.

.PARAMETER Prefix
    Install root. Defaults to %LOCALAPPDATA%\Programs\RevoluzenFlow.

.PARAMETER NoPath
    Do not offer to add the install directory to the user PATH.

.PARAMETER DryRun
    Resolve and report what would be installed, then stop.
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$Prefix = "$env:LOCALAPPDATA\Programs\RevoluzenFlow",
    [switch]$NoPath,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Repo = 'revoluzen-io/revoluzen-flow'
$Api = "https://api.github.com/repos/$Repo/releases"
$Dl = "https://github.com/$Repo/releases/download"

function Say { param([string]$m) Write-Host $m }
function Die { param([string]$m) Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# TLS 1.2 is not the default on Windows PowerShell 5.1, and without it the
# GitHub calls below fail with an opaque "could not create SSL/TLS secure
# channel" rather than anything a user can act on.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# ── Platform ─────────────────────────────────────────────────────────
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq 'AMD64') {
    $archTag = 'x86_64'
} elseif ($arch -eq 'ARM64') {
    Die @"
Windows on ARM is not published yet.

  The build matrix produces x86_64 only. An x64 build will run under
  emulation, but it is not tested and is not offered here.

  Meanwhile:
    pip install revoluzen-flow     (needs Python 3.11+)
    docker compose up -d           (needs Docker)
"@
} else {
    Die "unsupported architecture: $arch"
}

# ── Resolve the version ──────────────────────────────────────────────
if ($Version) {
    $tag = $Version
} else {
    Say 'Looking up the latest release...'
    try {
        $rel = Invoke-RestMethod -Uri "$Api/latest" -UseBasicParsing `
            -Headers @{ 'User-Agent' = 'revoluzen-flow-installer' }
        $tag = $rel.tag_name
    } catch {
        Die "could not reach $Api/latest - $($_.Exception.Message)"
    }
    if (-not $tag) { Die "could not determine the latest release." }
}
$ver = $tag -replace '^v', ''
$asset = "RevoluzenFlow-$ver-$archTag-windows.zip"

Say "Installing Revoluzen Flow $tag (Windows/$archTag)"
if ($DryRun) {
    Say "  would download $Dl/$tag/$asset"
    Say "  would install  $Prefix"
    Say '  (dry run - nothing written)'
    exit 0
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("rzf-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    $zip = Join-Path $tmp $asset
    Say "  downloading $asset"
    try {
        Invoke-WebRequest -Uri "$Dl/$tag/$asset" -OutFile $zip -UseBasicParsing `
            -Headers @{ 'User-Agent' = 'revoluzen-flow-installer' }
    } catch {
        Die @"
could not download $asset.

  Revoluzen Flow does not currently publish a signed Windows build.
  Unsigned, SmartScreen shows a red block dialog to exactly the users
  these installers exist for, so none is released. See installer/readme.md.

  Until then, on Windows use either:
    pip install revoluzen-flow     (needs Python 3.11+)
    docker compose up -d           (needs Docker)

  Assets for this tag: https://github.com/$Repo/releases/tag/$tag
"@
    }

    # ── Verify ───────────────────────────────────────────────────────
    # An unsigned package cannot prove who built it, but it can prove the
    # bytes arrived intact. A missing or mismatched sum aborts; warning
    # and continuing would make the check decorative.
    Say '  verifying checksum'
    $sumsPath = Join-Path $tmp 'SHA256SUMS'
    try {
        Invoke-WebRequest -Uri "$Dl/$tag/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing `
            -Headers @{ 'User-Agent' = 'revoluzen-flow-installer' }
    } catch {
        Die "release $tag publishes no SHA256SUMS - refusing to install unverified."
    }

    $line = Get-Content $sumsPath | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
    if (-not $line) { Die "$asset is not listed in SHA256SUMS - refusing to install." }
    $expected = ($line -split '\s+')[0]
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected.ToLower()) {
        Die "checksum mismatch - refusing to install.`n    expected $expected`n    actual   $actual"
    }

    # ── Install ──────────────────────────────────────────────────────
    # Replace rather than merge: leaving an old build's files behind is
    # how a half-upgraded install starts failing in ways nobody can
    # reproduce.
    if (Test-Path $Prefix) {
        Say "  removing previous install at $Prefix"
        Remove-Item $Prefix -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
    Say "  extracting to $Prefix"
    Expand-Archive -Path $zip -DestinationPath $Prefix -Force

    $exe = Get-ChildItem $Prefix -Recurse -Filter 'revoluzen.exe' | Select-Object -First 1
    if (-not $exe) { Die "the archive did not contain revoluzen.exe." }
    $binDir = $exe.Directory.FullName

    # ── PATH ─────────────────────────────────────────────────────────
    # User scope only, and announced. Editing the machine PATH from a
    # piped script would need admin and would be a genuine surprise.
    if (-not $NoPath) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($userPath -notlike "*$binDir*") {
            Say "  adding $binDir to your user PATH"
            $joined = if ([string]::IsNullOrEmpty($userPath)) { $binDir } else { "$userPath;$binDir" }
            [Environment]::SetEnvironmentVariable('Path', $joined, 'User')
            $env:Path = "$env:Path;$binDir"
            Say '  (open a new terminal for this to apply everywhere)'
        }
    }

    Say ''
    Say "Revoluzen Flow $tag installed to $Prefix"
    Say ''
    Say '  revoluzen serve     start the server, then open http://localhost:8001'
    Say '  revoluzen --help    everything else'
    Say ''
    Say "Uninstall: Remove-Item '$Prefix' -Recurse -Force"
    Say 'Data lives separately in %LOCALAPPDATA%\RevoluzenFlow\data and is NOT removed by that.'
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
