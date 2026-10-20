#!/usr/bin/env bash
# create-shortcut.sh - add an "Revoluzen Flow" entry to your Linux application
# menu (source / dev checkout), with the Revoluzen Flow logo.
#
#   ./create-shortcut.sh            # create the menu entry
#   ./create-shortcut.sh --remove   # remove it
#
# Requires the `revoluzen` CLI on PATH (run `pip install -e .` once in this
# folder). The .deb / .rpm / AppImage packages create their own launcher
# and don't need this - it's the source-user equivalent of the Windows
# Create-Shortcut.ps1.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
ICON="$ROOT/installer/windows/icons/revoluzen-flow.png"
DEST="$HOME/.local/share/applications/revoluzen-flow.desktop"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$DEST" && echo "Removed $DEST"
  exit 0
fi

mkdir -p "$(dirname "$DEST")"
cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=Revoluzen Flow
GenericName=Data Pipeline Studio
Comment=Local-first data pipeline studio by Revoluzen
Exec=sh -c 'cd "$ROOT" && revoluzen open'
Icon=$ICON
Terminal=false
Categories=Development;Database;
EOF

chmod +x "$DEST" 2>/dev/null || true
echo "Created $DEST"
echo "Look for 'Revoluzen Flow' in your application menu."
echo "(Remove with: ./create-shortcut.sh --remove)"
