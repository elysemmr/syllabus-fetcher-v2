#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo
        echo "Couldn't create the virtual environment. Make sure Python 3 is"
        echo "installed (python.org) and try again."
        read -p "Press Enter to close this window..."
        exit 1
    fi
else
    echo "Virtual environment already exists, skipping creation."
fi

echo "Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
if [ $? -ne 0 ]; then
    echo
    echo "Installing dependencies failed. Check the error above."
    read -p "Press Enter to close this window..."
    exit 1
fi

TK_VERSION=$(.venv/bin/python3 -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))" 2>/dev/null)
case "$TK_VERSION" in
    8.5*)
        echo
        echo "============================================================"
        echo "WARNING: This Python is linked against Tcl/Tk $TK_VERSION --"
        echo "Apple's old, deprecated system Tk. The point-and-click GUI"
        echo "(run_gui.command) will likely open as a blank, unresponsive"
        echo "window on this version. That's a known bug in that old Tk,"
        echo "not something this script can work around."
        echo
        echo "The command-line version (fetch_syllabi.py) still works fine"
        echo "either way. To fix the GUI, install a Python build linked"
        echo "against a modern Tk (8.6+) and recreate the virtual environment:"
        echo
        echo "  With Homebrew:"
        echo "    brew install python-tk@3.13"
        echo "    rm -rf .venv"
        echo "    /opt/homebrew/bin/python3.13 -m venv .venv"
        echo "    (Intel Mac: use /usr/local/bin/python3.13 instead)"
        echo "    source .venv/bin/activate && pip install -r requirements.txt"
        echo
        echo "  Without Homebrew: install Python from python.org (its"
        echo "  installer bundles a modern Tk), then recreate .venv the"
        echo "  same way using that Python instead of the system one."
        echo "============================================================"
        ;;
esac

echo
echo "Setup complete. Easiest way to run it: double-click run_gui.command."
echo
echo "Or from a terminal:"
echo "  source .venv/bin/activate"
echo "  python fetch_syllabi.py --courses CSE201,MATH150"
echo
echo "(The first run will pop up a box asking for your school's Brightspace login URL.)"
read -p "Press Enter to close this window..."
