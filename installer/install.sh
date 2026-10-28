#!/bin/sh
# Revoluzen Flow — one-line installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/revoluzen-io/revoluzen-flow/main/installer/install.sh | sh
#
# That URL works as soon as the repository is public — no DNS, no hosting.
# https://get.revoluzen.io/install.sh is the intended vanity alias; point it
# at this file (302 is fine) and the command above shortens to
# `curl -fsSL https://get.revoluzen.io/install.sh | sh`.
#
# Installs per-user. No root, no Python, no package manager. The AppImage
# it fetches carries its own Python runtime, so nothing has to be present
# on the machine beforehand.
#
# Read before you pipe this to a shell. Piping a remote script to sh runs
# whatever that URL serves today, and a product that argues for sovereignty
# should not ask you to skip that check. To inspect first:
#
#   curl -fsSL https://get.revoluzen.io/install.sh -o install.sh
#   less install.sh && sh install.sh
#
# Environment:
#   REVOLUZEN_VERSION   tag to install (default: latest release)
#   REVOLUZEN_PREFIX    install root    (default: ~/.local)
#   REVOLUZEN_NO_PATH=1 skip the PATH advice
#
set -eu

REPO="revoluzen-io/revoluzen-flow"
API="https://api.github.com/repos/${REPO}/releases"
DL="https://github.com/${REPO}/releases/download"
PREFIX="${REVOLUZEN_PREFIX:-$HOME/.local}"
BIN="${PREFIX}/bin"
LIBDIR="${PREFIX}/share/revoluzen-flow"

say()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

need() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."
}

# ── Fetch helper ─────────────────────────────────────────────────────
# curl and wget disagree on almost every flag, so normalise once. Both
# are told to fail on HTTP errors rather than writing an error page to
# the output file, which would otherwise be "downloaded" and then fail
# the checksum with a confusing message.
if command -v curl >/dev/null 2>&1; then
    fetch()  { curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$2"; }
    fetchs() { curl -fsSL --proto '=https' --tlsv1.2 "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch()  { wget -qO "$2" "$1"; }
    fetchs() { wget -qO- "$1"; }
else
    die "need curl or wget."
fi

# ── Platform ─────────────────────────────────────────────────────────
os=$(uname -s)
arch=$(uname -m)
case "$arch" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64)
        # installer/linux/build-appimage.sh hardcodes ARCH=x86_64, so no
        # arm64 asset is produced. Say that, rather than letting the user
        # discover it as a 404 on a URL that was never published.
        cat >&2 <<'EOF'
error: there is no arm64 Linux build yet.

  The release pipeline builds x86_64 only
  (installer/linux/build-appimage.sh hardcodes it).

  On arm64, use either:
    pipx install revoluzen-flow      # needs Python 3.11+
    docker compose up -d             # needs Docker
EOF
        exit 1
        ;;
    *) die "unsupported architecture: $arch" ;;
esac

case "$os" in
    Linux) ;;
    Darwin)
        # Be specific rather than 404-ing against a URL that was never
        # published. macOS packages are withheld on purpose: without an
        # Apple Developer ID signature and notarisation, Gatekeeper
        # refuses to open the package at all, so shipping one would help
        # nobody. See installer/readme.md.
        cat >&2 <<'EOF'
error: there is no macOS build to install yet.

  Revoluzen Flow does not publish an unsigned macOS package. Without
  Developer ID signing and notarisation, Gatekeeper blocks it outright,
  so the download would not run even if you fetched it.

  Until signing is in place, on macOS use either:

    pipx install revoluzen-flow      # needs Python 3.11+
    docker compose up -d             # needs Docker

  Progress: https://github.com/revoluzen-io/revoluzen-flow/issues
EOF
        exit 1
        ;;
    *) die "unsupported OS: $os (this script covers Linux and macOS)" ;;
esac

# ── Resolve the version ──────────────────────────────────────────────
if [ -n "${REVOLUZEN_VERSION:-}" ]; then
    tag="$REVOLUZEN_VERSION"
else
    say "Looking up the latest release..."
    # Deliberately no jq dependency — one grep beats asking the user to
    # install a JSON parser before they can install anything.
    tag=$(fetchs "${API}/latest" \
          | grep -m1 '"tag_name"' \
          | sed 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/') || true
    [ -n "$tag" ] || die "could not determine the latest release from ${API}/latest"
fi
version="${tag#v}"

# The AppImage is the right artifact for a per-user install: one file,
# its own glibc-compatible payload, no root and no package manager. The
# .deb/.rpm in the same release are for system-wide installs.
asset="RevoluzenFlow-${version}-${arch}.AppImage"
say "Installing Revoluzen Flow ${tag} (${os}/${arch})"

tmp=$(mktemp -d)
# shellcheck disable=SC2317  # invoked by the trap below, not inline
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

say "  downloading ${asset}"
fetch "${DL}/${tag}/${asset}" "${tmp}/${asset}" || die \
"could not download ${asset}.

  The release ${tag} may not publish an AppImage for ${arch}. Assets:
    https://github.com/${REPO}/releases/tag/${tag}"

# ── Verify ───────────────────────────────────────────────────────────
# Unsigned packages cannot prove authorship, but they can prove the
# bytes arrived intact. A missing or mismatched sum stops the install —
# never "warn and continue", which would make the check decorative.
say "  verifying checksum"
fetch "${DL}/${tag}/SHA256SUMS" "${tmp}/SHA256SUMS" \
    || die "release ${tag} publishes no SHA256SUMS — refusing to install unverified."

expected=$(grep " ${asset}\$" "${tmp}/SHA256SUMS" | awk '{print $1}' | head -n1)
[ -n "$expected" ] || die "${asset} is not listed in SHA256SUMS — refusing to install."

if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "${tmp}/${asset}" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "${tmp}/${asset}" | awk '{print $1}')
else
    die "need sha256sum or shasum to verify the download."
fi

[ "$actual" = "$expected" ] || die \
"checksum mismatch — refusing to install.
    expected ${expected}
    actual   ${actual}"

# ── Install ──────────────────────────────────────────────────────────
mkdir -p "$BIN" "$LIBDIR"
install -m 0755 "${tmp}/${asset}" "${LIBDIR}/${asset}"
ln -sf "${LIBDIR}/${asset}" "${BIN}/revoluzen"
say "  installed ${LIBDIR}/${asset}"
say "  linked    ${BIN}/revoluzen"

# ── PATH ─────────────────────────────────────────────────────────────
# Tell, don't edit. Rewriting someone's shell profile from a piped
# script is the kind of surprise that costs trust, and the fix is one
# line the user can read.
if [ "${REVOLUZEN_NO_PATH:-}" != "1" ]; then
    case ":${PATH}:" in
        *":${BIN}:"*) ;;
        *)
            say ""
            say "${BIN} is not on your PATH. Add it:"
            say ""
            say "    echo 'export PATH=\"${BIN}:\$PATH\"' >> ~/.profile"
            say "    export PATH=\"${BIN}:\$PATH\""
            ;;
    esac
fi

cat <<EOF

Revoluzen Flow ${tag} installed.

  revoluzen serve     start the server, then open http://localhost:8001
  revoluzen --help    everything else

Uninstall: rm -rf "${LIBDIR}" "${BIN}/revoluzen"
Data lives separately in \${REVOLUZEN_FLOW_DATA_DIR:-~/.local/share/revoluzen-flow}
and is NOT removed by that.
EOF
