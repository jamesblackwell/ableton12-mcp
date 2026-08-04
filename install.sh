#!/usr/bin/env bash
# Install the ableton12 Remote Script into Ableton Live's User Library.
#
# Safe to re-run: it backs up any existing AbletonMCP script before replacing
# it, and clears the stale __pycache__ so Live recompiles the new source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/remote_script/AbletonMCP"
DEST_DIR="$HOME/Music/Ableton/User Library/Remote Scripts"
DEST="$DEST_DIR/AbletonMCP"
# Back up OUTSIDE Remote Scripts — Ableton scans that folder for control
# surfaces, so a backup left inside it would show up as a bogus surface.
BACKUP_DIR="$HOME/Music/Ableton/User Library/_AbletonMCP_backups"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: source remote script not found at: $SRC" >&2
  exit 1
fi

# Refuse to clobber while Live holds the port — the new script won't load anyway.
if lsof -nP -iTCP:9877 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "WARNING: something is listening on port 9877 (Ableton is probably running)."
  echo "         Quit Ableton Live fully before installing, then re-run this script."
  echo
fi

mkdir -p "$DEST_DIR"

if [[ -d "$DEST" ]]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  BACKUP="$BACKUP_DIR/AbletonMCP.backup-$STAMP"
  echo "Backing up existing remote script -> $BACKUP"
  cp -R "$DEST" "$BACKUP"
  rm -rf "$DEST"
fi

echo "Installing remote script -> $DEST"
cp -R "$SRC" "$DEST"
# Clear stale bytecode so Live recompiles from the new source.
rm -rf "$DEST/__pycache__"

cat <<'EOF'

✅ Remote Script installed.

Next steps (do these in order):

  1. Quit Ableton Live completely (Cmd-Q), if it is running.
  2. Relaunch Ableton Live 12.
  3. Settings → Link, Tempo & MIDI → Control Surfaces:
       pick "AbletonMCP", with Input = None and Output = None.
       (If it was already selected, it should still be — just confirm.)
  4. Watch the status bar at the bottom: it should read
       "AbletonMCP (ableton12): listening on port 9877".
  5. Make sure the MCP server is registered to THIS project (not the old
     'uvx ableton-mcp'). From the project root:
       claude mcp remove AbletonMCP -s user
       claude mcp add AbletonMCP -s user -- \
         uvx --from "PROJECT_PATH/mcp_servers/ableton12" ableton12
     Then run /mcp in Claude Code to reconnect.

Rollback: if the control surface fails to load, restore the printed backup
folder over the AbletonMCP folder and restart Ableton.
EOF
