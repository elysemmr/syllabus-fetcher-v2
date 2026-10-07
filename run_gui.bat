@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Setup hasn't been run yet. Double-click setup.bat first.
    pause
    exit /b 1
)

REM pythonw.exe has no console window of its own, so this opens straight
REM into the GUI instead of a black command-prompt window.
if exist .venv\Scripts\pythonw.exe (
    start "" .venv\Scripts\pythonw.exe run_gui.py
) else (
    start "" .venv\Scripts\python.exe run_gui.py
)
