#!/bin/bash
# ============================================================================
#  JeevesBot Dependency Installer (Linux)
#
#  Installs Python package dependencies. Run once before first launch.
#  Re-run after updating JeevesBot to pick up any new dependencies.
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+."
    echo "  Ubuntu/Debian:  sudo apt install python3 python3-pip"
    echo "  Fedora/RHEL:    sudo dnf install python3 python3-pip"
    exit 1
fi

echo "Installing JeevesBot dependencies..."

# Install runtime dependencies only (skip PyInstaller on servers)
python3 -m pip install discord.py python-dotenv rcon httpx

echo ""
echo "Dependencies installed. Run ./run.sh to start the bot."
