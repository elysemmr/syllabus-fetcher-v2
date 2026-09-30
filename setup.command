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

echo
echo "Setup complete. To run the script, open a new Terminal window in this"
echo "folder and use:"
echo "  source .venv/bin/activate"
echo "  python fetch_syllabi.py --courses CSE201,MATH150"
echo
echo "(The first run will pop up a box asking for your school's Brightspace login URL.)"
read -p "Press Enter to close this window..."
