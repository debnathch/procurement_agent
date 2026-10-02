@echo off
setlocal enabledelayedexpansion
rem ==============================================================================
rem MARG Procurement Agent — Windows Run Package Builder
rem Double-click this file to build/refresh the "win_run_pkg" folder and .zip archive.
rem Output: win_run_pkg\ (folder) and win_run_pkg.zip
rem ==============================================================================
title Build Windows Run Package (win_run_pkg)
cd /d "%~dp0"

echo ============================================================
echo   Building Windows Run Package into: win_run_pkg\
echo ============================================================
echo.

where python >nul 2>nul
if %errorlevel% equ 0 (
    python scripts\package_windows.py
    goto end
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 scripts\package_windows.py
    goto end
)

where uv >nul 2>nul
if %errorlevel% equ 0 (
    uv run python scripts\package_windows.py
    goto end
)

echo [!] Error: Python or uv was not detected. Please install Python to build the package.

:end
echo.
pause
