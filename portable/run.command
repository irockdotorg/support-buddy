#!/bin/bash
# SupportBuddy Portable Launcher (macOS)
# Put this in the same folder as the platform binaries.
# Double-click this file (or run from Terminal) to start SupportBuddy.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BINARY="$SCRIPT_DIR/support-buddy-mac"

if [ ! -f "$BINARY" ]; then
    echo "Error: Mac binary not found at $BINARY"
    echo "Download it from the SupportBuddy releases page."
    exit 1
fi

chmod +x "$BINARY"

# Check for admin rights
if [ "$(id -u)" -ne 0 ]; then
    osascript -e 'do shell script "'""$BINARY"'" --port 58678" with administrator privileges'
else
    exec "$BINARY" --port 58678 "$@"
fi
