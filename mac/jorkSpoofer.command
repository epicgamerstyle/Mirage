#!/bin/bash
# Luke's Mirage Instance Manager — Mac Launcher
# Finds Python 3, checks for pywebview, and launches the GUI.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Find Python 3 ──────────────────────────────────────────────────────────

PYTHON=""

# Check common locations (including Homebrew versioned binaries)
for candidate in python3 python3.13 python3.12 python3.11 python3.10 python /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" = "3" ] && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ not found."
    echo ""
    echo "Install Python from https://python.org or via Homebrew:"
    echo "  brew install python@3.12"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Using Python: $PYTHON ($($PYTHON --version 2>&1))"

# ── Virtual environment ─────────────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi

# Activate venv — use its Python from here on
PYTHON="$VENV_DIR/bin/python"

# ── Check pywebview ─────────────────────────────────────────────────────────

if ! "$PYTHON" -c "import webview" 2>/dev/null; then
    echo ""
    echo "Installing pywebview..."
    "$PYTHON" -m pip install pywebview --quiet
    if ! "$PYTHON" -c "import webview" 2>/dev/null; then
        echo "ERROR: Failed to install pywebview."
        echo "Try manually: $PYTHON -m pip install pywebview"
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo "pywebview installed successfully."
fi

# ── Launch ──────────────────────────────────────────────────────────────────

echo "Starting Luke's Mirage..."
exec "$PYTHON" "$SCRIPT_DIR/gui.py" "$@"
