"""
Isolated Windows Package Builder
Creates a standalone, portable distribution package for Windows that runs completely
isolated on any target Windows machine, storing all SQLite database files, CSV orders,
and configuration locally inside the package directory.

Outputs:
  dist/procurement-agent-portable/
  dist/procurement-agent-portable.zip
"""
import os
import shutil
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT_DIR / "win_run_pkg"
ZIP_OUTPUT = ROOT_DIR / "win_run_pkg.zip"


def create_isolated_package():
    print("=" * 60)
    print("  Building Windows Run Package into: win_run_pkg/")
    print("=" * 60)

    # 1. Clean previous build
    if PACKAGE_DIR.exists():
        print(f"[*] Refreshing package directory at {PACKAGE_DIR}...")
        shutil.rmtree(PACKAGE_DIR)
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Copy application code
    print("[*] Copying application source code...")
    shutil.copytree(
        ROOT_DIR / "backend",
        PACKAGE_DIR / "backend",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")
    )
    shutil.copytree(
        ROOT_DIR / "frontend",
        PACKAGE_DIR / "frontend",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    )

    # 3. Copy launcher and manifests
    print("[*] Copying launchers and requirements...")
    shutil.copy2(ROOT_DIR / "launcher.py", PACKAGE_DIR / "launcher.py")
    shutil.copy2(ROOT_DIR / "requirements.txt", PACKAGE_DIR / "requirements.txt")
    shutil.copy2(ROOT_DIR / "pyproject.toml", PACKAGE_DIR / "pyproject.toml")

    # 4. Create isolated storage directories on Windows
    print("[*] Initializing local isolated storage folders (data, outbox, inbox)...")
    (PACKAGE_DIR / "data").mkdir(exist_ok=True)
    (PACKAGE_DIR / "outbox").mkdir(exist_ok=True)
    (PACKAGE_DIR / "inbox").mkdir(exist_ok=True)

    # Keep empty folders in git/zip with .gitkeep
    (PACKAGE_DIR / "data" / ".keep").touch()
    (PACKAGE_DIR / "outbox" / ".keep").touch()
    (PACKAGE_DIR / "inbox" / ".keep").touch()

    # 5. Create isolated .env
    print("[*] Generating isolated .env configuration...")
    isolated_env_content = """# MARG Procurement Agent - Isolated Machine Configuration
APP_NAME=MARG Procurement Agent (Isolated)

# Store SQLite database completely inside local ./data/ directory on this machine
DATABASE_URL=sqlite:///./data/procurement_agent.db

# Execution mode: 'csv' writes POs to local ./outbox/ folder
EXECUTION_MODE=csv
REQUIRE_HUMAN_APPROVAL=true

# Inventory Policy Defaults
DEFAULT_REVIEW_DAYS=7
DEFAULT_SAFETY_DAYS=3
EXPIRY_RISK_HORIZON_DAYS=180
DEFAULT_LEAD_TIME_DAYS=45

# Guardrails
MAX_PROPOSAL_QTY_UNITS=5000
MAX_PROPOSAL_VALUE=250000

# MARG ERP Integration (disable by default for local offline use)
MARG_SOURCE_ENABLED=false
MARG_ENABLED=false
"""
    (PACKAGE_DIR / ".env").write_text(isolated_env_content, encoding="utf-8")
    (PACKAGE_DIR / ".env.example").write_text(isolated_env_content, encoding="utf-8")

    # 6. Generate the standalone one-click batch launcher for the portable package
    print("[*] Generating isolated run launcher (run_isolated.bat)...")
    bat_content = r"""@echo off
setlocal enabledelayedexpansion
rem ==============================================================================
rem MARG Pharmaceutical Procurement Agent — Isolated Windows Launcher
rem Completely self-contained: stores all database, CSV orders, and state locally.
rem Double-click this file to set up and run on this machine.
rem ==============================================================================
title MARG Procurement Agent (Isolated)
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
    echo  You opened 'run_agent.bat' directly from INSIDE the ZIP file
    echo  without extracting it first.
    echo.
    echo  Windows only extracts the batch file to a Temp folder, so
    echo  the application cannot find 'launcher.py' or 'pyproject.toml'.
    echo.
    echo  HOW TO FIX THIS:
    echo  ------------------------------------------------------------
    echo   1. Close this Command Prompt window.
    echo   2. Go to where 'win_run_pkg.zip' is saved.
    echo   3. RIGHT-CLICK on 'win_run_pkg.zip'.
    echo   4. Click "Extract All..." and click "Extract".
    echo   5. Open the newly extracted folder and double-click:
    echo          run_agent.bat
    echo  ------------------------------------------------------------
    echo.
    pause
    exit /b 1
)

rem 1. Guarantee isolated storage folders exist on this machine
if not exist "data" mkdir "data"
if not exist "outbox" mkdir "outbox"
if not exist "inbox" mkdir "inbox"

rem 2. Ensure .env exists
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
    if !errorlevel! neq 0 (
        echo [!] Notice: 'uv sync' reported an issue. Attempting launch...
    )
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
echo   Running in ISOLATED mode on this machine:
echo   - Local Database : %~dp0data\procurement_agent.db
echo   - PO CSV Outbox  : %~dp0outbox\
echo   - Web UI         : http://localhost:8501
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
echo   Running in ISOLATED mode on this machine:
echo   - Local Database : %~dp0data\procurement_agent.db
echo   - PO CSV Outbox  : %~dp0outbox\
echo   - Web UI         : http://localhost:8501
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
"""
    (PACKAGE_DIR / "run_agent.bat").write_text(bat_content, encoding="utf-8")
    (PACKAGE_DIR / "run_isolated.bat").write_text(bat_content, encoding="utf-8")

    # 7. Write TRANSFER_INSTRUCTIONS.txt
    readme_portable = """========================================================================
MARG PHARMACEUTICAL PROCUREMENT AGENT - WINDOWS RUN PACKAGE
========================================================================

WHAT THIS FOLDER IS:
This "win_run_pkg" folder is the complete, self-contained package to transfer
to any Windows computer. You do not need to copy any other repository files.

HOW TO TRANSFER TO A WINDOWS COMPUTER:
- Option A: Copy this entire "win_run_pkg" folder (e.g. via USB stick or network share).
- Option B: Copy "win_run_pkg.zip" and extract it on the Windows computer.

HOW TO RUN ON WINDOWS:
1. Open the "win_run_pkg" folder on the Windows computer.
2. Double-click "run_agent.bat".
3. The script will automatically:
   - Check/create an isolated local virtual environment.
   - Install required packages (FastAPI, Streamlit, etc.).
   - Configure local data storage in "./data/".
   - Start the Web Dashboard at: http://localhost:8501
   - Open your web browser automatically!

DATA STORAGE ON THE WINDOWS COMPUTER:
- SQLite Database: All data, suppliers, products, and audits are saved in:
    win_run_pkg\\data\\procurement_agent.db
- Purchase Order Exports: Generated CSV orders are saved in:
    win_run_pkg\\outbox\\
- MARG ERP Excel Ingestion: Place your uploaded files in:
    win_run_pkg\\inbox\\

HOW TO STOP:
Press Ctrl+C in the Command Prompt window to cleanly shut down all services.
"""
    (PACKAGE_DIR / "TRANSFER_INSTRUCTIONS.txt").write_text(readme_portable, encoding="utf-8")

    # 8. Create ZIP archive
    print(f"[*] Compressing package to {ZIP_OUTPUT}...")
    with zipfile.ZipFile(ZIP_OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in PACKAGE_DIR.rglob("*"):
            if file.is_file():
                zf.write(file, arcname=file.relative_to(ROOT_DIR))

    print("=" * 60)
    print(f"[+] Windows Run Package successfully created!")
    print(f"    Folder to copy to Windows: {PACKAGE_DIR}")
    print(f"    Or ZIP archive to copy   : {ZIP_OUTPUT}")
    print("=" * 60)


if __name__ == "__main__":
    create_isolated_package()
