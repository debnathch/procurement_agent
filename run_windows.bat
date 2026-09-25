@echo off
rem ==============================================================================
rem MARG Pharmaceutical Procurement Agent — One-Click Setup & Launch (Windows)
rem Double-click this file in Windows File Explorer to set up and run.
rem ==============================================================================
title MARG Pharmaceutical Procurement Agent
cd /d "%~dp0"

echo ============================================================
echo   MARG Pharmaceutical Procurement Copilot Setup ^& Launcher
echo ============================================================

rem 1. Initialize .env if missing
if not exist ".env" (
    if exist ".env.example" (
        echo [*] Initializing environment configuration ^(.env^)...
        copy ".env.example" ".env" >nul
    )
)

rem 2. Check for Astral uv
where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Found 'uv' package manager. Synchronizing environment...
    uv sync
    echo [*] Launching Procurement Agent...
    uv run python launcher.py
    goto end
)

rem 3. Check for standard Python
where python >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] 'uv' not found. Using system Python...
    if not exist ".venv" (
        echo [*] Creating virtual environment ^(.venv^)...
        python -m venv .venv
    )
    echo [*] Activating virtual environment...
    call .venv\Scripts\activate.bat

    echo [*] Ensuring required dependencies are installed...
    python -m pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt

    echo [*] Launching Procurement Agent...
    python launcher.py
    goto end
)

echo.
echo [!] Error: Python was not detected on your system.
echo     Please install Python from https://www.python.org/downloads/
echo     or install uv from https://github.com/astral-sh/uv
echo.
pause

:end
