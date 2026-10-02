@echo off
setlocal enabledelayedexpansion
rem ==============================================================================
rem MARG Pharmaceutical Procurement Agent — Isolated Windows Launcher
rem Completely self-contained: stores all SQLite data, CSV orders, and state locally.
rem Double-click this file to set up and run in isolated mode on this Windows machine.
rem ==============================================================================
title MARG Procurement Agent (Isolated Mode)
cd /d "%~dp0"

echo ============================================================
echo   MARG Procurement Agent — Isolated Machine Setup ^& Runner
echo ============================================================
echo.

rem Pre-flight check: Detect if running directly inside an unextracted ZIP preview
if not exist "%~dp0launcher.py" (
    echo ============================================================
    echo  [!] ERROR: APPLICATION FILES NOT FOUND!
    echo ============================================================
    echo.
    echo  You opened this batch file directly from INSIDE a ZIP file
    echo  without extracting it first.
    echo.
    echo  Windows only extracts the batch file to a Temp folder, so
    echo  it cannot find 'launcher.py' or 'pyproject.toml'.
    echo.
    echo  HOW TO FIX THIS:
    echo  ------------------------------------------------------------
    echo   1. Close this Command Prompt window.
    echo   2. Go to where the ZIP archive is saved.
    echo   3. RIGHT-CLICK on the ZIP file.
    echo   4. Click "Extract All..." and click "Extract".
    echo   5. Open the newly extracted folder and double-click the .bat.
    echo  ------------------------------------------------------------
    echo.
    pause
    exit /b 1
)

rem 1. Guarantee isolated storage folders exist on this machine
if not exist "data" mkdir "data"
if not exist "outbox" mkdir "outbox"
if not exist "inbox" mkdir "inbox"

rem 2. Ensure .env exists with isolated configuration
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    )
)

rem 3. Explicitly set isolated runtime variables
set "DATABASE_URL=sqlite:///./data/procurement_agent.db"
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"
set "PYTHONPATH=%~dp0"

rem Include user local bin in PATH if present
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;!PATH!"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"

rem 4. Check for Astral uv
where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Detected 'uv' runtime manager.
    echo [*] Synchronizing isolated local environment...
    call uv sync
    goto launch_uv
)

rem 5. Check for system Python (Python 3.10+ / py -3)
set "PYTHON_EXE="
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    goto found_python
)

py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py -3"
    goto found_python
)

python -c "import sys; sys.exit(0)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    goto found_python
)

py -c "import sys; sys.exit(0)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py"
    goto found_python
)

rem 6. Auto-bootstrap uv via PowerShell if no Python is installed
echo [*] No Python found. Automatically bootstrapping lightweight runtime (Astral uv)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >nul 2>nul

if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;!PATH!"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"

where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [+] Runtime installed successfully. Synchronizing dependencies...
    call uv sync
    goto launch_uv
)

goto error_exit

:launch_uv
echo.
echo ============================================================
echo   Running in ISOLATED mode on this Windows machine:
echo   - Local Database : %~dp0data\procurement_agent.db
echo   - PO CSV Outbox  : %~dp0outbox\
echo   - Web Dashboard  : http://localhost:8501
echo   - API Server     : http://localhost:8000/docs
echo   (Press Ctrl+C in this terminal window to stop)
echo ============================================================
echo.
call uv run python launcher.py
goto on_exit

:found_python
echo [*] Using Python: %PYTHON_EXE%
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating isolated virtual environment in .venv...
    %PYTHON_EXE% -m venv .venv
    if %errorlevel% neq 0 goto error_exit
)

echo [*] Ensuring dependencies are installed...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip >nul 2>nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [!] Notice: Dependency check had warnings. Attempting launch...
)

echo.
echo ============================================================
echo   Running in ISOLATED mode on this Windows machine:
echo   - Local Database : %~dp0data\procurement_agent.db
echo   - PO CSV Outbox  : %~dp0outbox\
echo   - Web Dashboard  : http://localhost:8501
echo   - API Server     : http://localhost:8000/docs
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
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo Make sure to check [x] "Add python.exe to PATH" during installation.
echo.

:on_exit
echo.
echo Application stopped.
pause
