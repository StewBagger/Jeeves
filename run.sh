#!/bin/bash
# ============================================================================
#  JeevesBot Run Script (Linux — run from source)
#
#  This runs JeevesBot directly from Python source without compiling.
#  Recommended for Linux servers where Python is already installed.
#
#  First-time setup:
#    1. Copy config.env.example to config.env and fill in your values
#    2. chmod +x run.sh install.sh
#    3. ./install.sh    (installs Python dependencies)
#    4. ./run.sh        (starts the bot)
#
#  To run as a systemd service, see the README.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "config.env" ]; then
    echo "ERROR: config.env not found."
    echo "Copy config.env.example to config.env and fill in your values."
    exit 1
fi

exec python3 Jeeves.py
