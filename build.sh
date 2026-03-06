#!/bin/bash
# ============================================================================
#  JeevesBot Build Script (Linux)
#  Compiles Jeeves into a standalone executable using PyInstaller.
# ============================================================================
#
#  Prerequisites:
#    1. Python 3.10+ installed
#    2. Run once:  pip install -r requirements.txt
#
#  Usage:
#    chmod +x build.sh && ./build.sh
#    Output:  dist/Jeeves/Jeeves
#
#  After building:
#    1. Copy the entire dist/Jeeves/ folder to your server
#    2. Place config.env next to the Jeeves binary
#    3. Run: ./Jeeves
# ============================================================================

set -e

echo ""
echo "============================================"
echo "  JeevesBot Build Script (Linux)"
echo "============================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+."
    exit 1
fi

echo "Installing/verifying dependencies..."
python3 -m pip install -r requirements.txt

echo ""
echo "Building Jeeves..."
echo ""

python3 -m PyInstaller Jeeves.spec --noconfirm --clean

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Build failed! Check output above."
    exit 1
fi

# Copy config to output
if [ -f "config.env.example" ]; then
    cp config.env.example dist/Jeeves/config.env.example
fi

echo ""
echo "============================================"
echo "  Build complete!"
echo "  Output: dist/Jeeves/Jeeves"
echo "============================================"
echo ""
