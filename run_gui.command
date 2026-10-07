#!/bin/bash
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python3 ]; then
    echo "Setup hasn't been run yet. Double-click setup.command first."
    read -p "Press Enter to close this window..."
    exit 1
fi

.venv/bin/python3 run_gui.py
