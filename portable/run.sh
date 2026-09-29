#!/bin/bash
# SupportBuddy Portable Launcher
# Detects the OS and runs the correct binary.
# Put this in the same folder as the platform binaries.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

detect_os() {
    local os_type
    os_type=$(uname -s)
    case "$os_type" in
        Linux*)   echo "linux";;
        Darwin*)  echo "mac";;
        *)        echo "unknown";;
    esac
}

OS="$(detect_os)"
BINARY=""

case "$OS" in
    linux)
        BINARY="$SCRIPT_DIR/support-buddy-linux"
        if [ ! -f "$BINARY" ]; then
            echo "Error: Linux binary not found at $BINARY"
            echo "Download it from the SupportBuddy releases page."
            exit 1
        fi
        chmod +x "$BINARY"
        # Try to run with sudo if the user wants admin access
        if [ "$(id -u)" -ne 0 ]; then
            echo "SupportBuddy — run as normal user or with sudo for admin actions?"
            echo "  [1] Normal user (no admin — some cleanups will be limited)"
            echo "  [2] sudo (full access)"
            echo "  [Enter] Normal user (default)"
            read -r choice
            case "$choice" in
                2|s|sudo)
                    exec sudo "$BINARY" "$@"
                    ;;
            esac
        fi
        exec "$BINARY" "$@"
        ;;
    mac)
        BINARY="$SCRIPT_DIR/support-buddy-mac"
        if [ ! -f "$BINARY" ]; then
            echo "Error: Mac binary not found at $BINARY"
            echo "Download it from the SupportBuddy releases page."
            exit 1
        fi
        chmod +x "$BINARY"
        exec "$BINARY" "$@"
        ;;
    *)
        echo "Error: Unsupported operating system: $OS"
        echo "This launcher supports Linux and macOS."
        exit 1
        ;;
esac
