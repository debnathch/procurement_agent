@echo off
setlocal enabledelayedexpansion
rem ==============================================================================
rem MARG Pharmaceutical Procurement Agent — One-Click Setup & Launch (Windows)
rem Double-click this file in Windows File Explorer to automatically set up and run.
rem ==============================================================================
title MARG Pharmaceutical Procurement Agent
cd /d "%~dp0"

echo ============================================================
echo   MARG Pharmaceutical Procurement Copilot Setup ^& Launcher
echo ============================================================
echo.

rem 1. Initialize .env if missing
if not exist ".env" (
    if exist ".env.example" (
        echo [*] Initializing environment configuration ^(.env^)...
        copy ".env.example" ".env" >nul
        echo [+] Configuration file ^(.env^) created successfully.
    )
)

rem Include common user binary folders in PATH if present
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;!PATH!"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"

rem 2. Strategy 1: Check for Astral uv (fastest & most reliable)
where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Detected 'uv' package manager.
    echo [*] Synchronizing environment dependencies...
    call uv sync
    if %errorlevel% neq 0 (
        echo [!] Notice: 'uv sync' had non-zero status. Attempting launch...
    )
    goto launch_uv
)

rem 3. Strategy 2: Detect system Python or Windows Python Launcher (py)
set "PYTHON_EXE="

rem Test standard 'python' with Python 3.10+ check
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    goto found_python
)

rem Test 'py -3' (official Windows Python launcher from python.org)
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py -3"
    goto found_python
)

rem Fallback: test 'python' without strict 3.10 check
python -c "import sys; sys.exit(0)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    goto found_python
)

rem Fallback: test 'py'
py -c "import sys; sys.exit(0)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py"
    goto found_python
)

rem 4. Strategy 3: Automatic Zero-Config Setup via PowerShell
rem If neither Python nor uv is installed, automatically install uv (no admin required)
echo [*] No pre-existing Python or uv installation detected.
echo [*] Automatically downloading and setting up lightweight runtime ^(Astral uv^)...
echo [*] ^(This requires no administrator privileges and only runs once^)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >nul 2>nul

if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;!PATH!"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"

where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [+] Runtime environment successfully installed!
    echo [*] Fetching Python and synchronizing dependencies...
    call uv sync
    goto launch_uv
)

rem If all automatic installation attempts failed:
goto error_exit

:launch_uv
echo.
echo ============================================================
echo   Starting Procurement Agent Services...
echo   Streamlit UI: http://localhost:8501
echo   FastAPI Docs: http://localhost:8000/docs
echo   (Press Ctrl+C in this terminal window to stop)
echo ============================================================
echo.
call uv run python launcher.py
goto on_exit

:found_python
echo [*] Found Python interpreter: %PYTHON_EXE%

rem Create virtual environment if missing
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating virtual environment ^(.venv^)...
    %PYTHON_EXE% -m venv .venv
    if %errorlevel% neq 0 (
        echo [!] Error: Failed to create virtual environment with %PYTHON_EXE%.
        goto error_exit
    )
    echo [+] Virtual environment created.
)

rem Install / verify dependencies
echo [*] Ensuring required dependencies are installed...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip >nul 2>nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [!] Notice: Some dependencies returned warnings. Attempting launch...
)

echo.
echo ============================================================
echo   Starting Procurement Agent Services...
echo   Streamlit UI: http://localhost:8501
echo   FastAPI Docs: http://localhost:8000/docs
echo   (Press Ctrl+C in this terminal window to stop)
echo ============================================================
echo.
.venv\Scripts\python.exe launcher.py
goto on_exit

:error_exit
echo.
echo ============================================================
echo [!] Setup could not detect or automatically install Python.
echo ============================================================
echo.
echo Please ensure you have an active internet connection, or install Python:
echo   1. Download Python: https://www.python.org/downloads/
echo   2. IMPORTANT: During installation, make sure to check:
echo      [x] "Add python.exe to PATH"
echo.

:on_exit
echo.
echo Application stopped.
pause


