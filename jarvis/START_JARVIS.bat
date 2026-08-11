@echo off
setlocal EnableExtensions
title JARVIS
cd /d "%~dp0"

echo.
echo  ========================================
echo   JARVIS
echo  ========================================
echo.
echo  Folder: %CD%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python is not on your PATH.
    echo Install Python from https://www.python.org/downloads/
    echo When installing, check "Add python.exe to PATH", then try again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a local virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv
        pause
        exit /b 1
    )
)

set "PY=.venv\Scripts\python.exe"
set "PIP=.venv\Scripts\pip.exe"

echo Installing / updating required packages (first run can take a minute)...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Package install failed. Check the errors above.
    pause
    exit /b 1
)

echo.
echo Starting JARVIS...
echo When you see "Uvicorn running", the site is ready:
echo.
echo     http://127.0.0.1:8742
echo.
echo Leave this window OPEN. Closing it stops JARVIS.
echo.

start "" http://127.0.0.1:8742
"%PY%" -m jarvis --host 127.0.0.1 --port 8742
echo.
echo JARVIS stopped.
pause
