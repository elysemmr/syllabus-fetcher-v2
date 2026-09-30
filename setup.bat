@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Couldn't create the virtual environment. Make sure Python is installed
        echo and available as "python" from a Command Prompt, then try again.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, skipping creation.
)

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo Some dependencies failed to install ^(sometimes docx2pdf's pywin32
    echo dependency, which can fail to build on some Windows/Python combos^).
    echo Retrying without docx2pdf so the rest of setup can finish -- PDF
    echo conversion just won't be available; syllabi will still download, left
    echo in their original file format.
    findstr /v /b /i "docx2pdf" requirements.txt > requirements-fallback.tmp
    .venv\Scripts\python.exe -m pip install -r requirements-fallback.tmp -q
    set "FALLBACK_STATUS=!errorlevel!"
    del requirements-fallback.tmp
    if not "!FALLBACK_STATUS!"=="0" (
        echo.
        echo Installing dependencies failed. Check the error above.
        pause
        exit /b 1
    )
)

echo.
echo Setup complete. To run the script, open a new terminal in this folder and use:
echo   .venv\Scripts\activate
echo   python fetch_syllabi.py --courses CSE201,MATH150
echo.
echo (The first run will pop up a box asking for your school's Brightspace login URL.)
pause
