#!/bin/bash
# ==============================================================================
# MARG Pharmaceutical Procurement Agent — One-Click Setup & Launch (macOS/Linux)
# Double-click this file in Finder to set up and launch the application.
# ==============================================================================

set -e

# Change directory to the repository root where this script resides
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "============================================================"
echo "  MARG Pharmaceutical Procurement Copilot Setup & Launcher"
echo "============================================================"

# Ensure .env exists
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "[*] Initializing environment configuration (.env)..."
    cp .env.example .env
fi

# Strategy 1: Check if Astral uv is installed (fastest)
if command -v uv >/dev/null 2>&1; then
    echo "[*] Found 'uv' package manager. Synchronizing environment..."
    uv sync
    echo "[*] Launching Procurement Agent..."
    uv run python launcher.py
    exit 0
fi

# Strategy 2: Standard Python 3 + Virtual Environment
if command -v python3 >/dev/null 2>&1; then
    echo "[*] 'uv' not found. Using system Python 3..."
    
    if [ ! -d ".venv" ]; then
        echo "[*] Creating local virtual environment (.venv)..."
        python3 -m venv .venv
    fi

    echo "[*] Activating virtual environment..."
    source .venv/bin/activate

    echo "[*] Ensuring required dependencies are installed..."
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt

    echo "[*] Launching Procurement Agent..."
    python launcher.py
    exit 0
fi

echo ""
echo "[!] Error: Neither 'uv' nor 'python3' was detected on your system."
echo "    Please install Python 3 (https://www.python.org) or uv (curl -LsSf https://astral.sh/uv/install.sh | sh)"
echo ""
read -p "Press Enter to exit..."
