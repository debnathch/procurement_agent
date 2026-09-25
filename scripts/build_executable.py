"""
Script to build a standalone executable for the Procurement Agent using PyInstaller.
Can be executed locally or inside CI/CD pipelines via:
    uv run python scripts/build_executable.py
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def build():
    os.chdir(ROOT_DIR)
    dist_dir = ROOT_DIR / "dist"
    build_dir = ROOT_DIR / "build"

    print("[*] Preparing build environment...")
    
    # Locate streamlit static assets
    import streamlit
    streamlit_path = Path(streamlit.__file__).parent

    # Prepare PyInstaller command
    sep = ";" if sys.platform == "win32" else ":"
    
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name=procurement-agent",
        "--onedir",  # onedir is faster to start and more reliable for complex frameworks like Streamlit
        "--noconfirm",
        "--clean",
        # Include application directories
        f"--add-data=backend{sep}backend",
        f"--add-data=frontend{sep}frontend",
        f"--add-data={streamlit_path}{sep}streamlit",
        # Hidden imports
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespans",
        "--hidden-import=uvicorn.lifespans.on",
        "--hidden-import=sqlalchemy.dialects.sqlite",
        "--hidden-import=engineio.async_drivers.asgi",
        "--hidden-import=pydantic_settings",
        "--hidden-import=openpyxl",
        "--hidden-import=xlrd",
        # Main entry script
        "launcher.py",
    ]

    print(f"[*] Running PyInstaller build on {sys.platform}...")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print("[!] Build failed!")
        sys.exit(result.returncode)

    # Copy .env.example into dist
    target_dist = dist_dir / "procurement-agent"
    if (ROOT_DIR / ".env.example").exists() and target_dist.exists():
        shutil.copy(ROOT_DIR / ".env.example", target_dist / ".env.example")

    print("=" * 60)
    print(f"[+] Build successful! Output directory: {target_dist}")
    print("=" * 60)


if __name__ == "__main__":
    build()
