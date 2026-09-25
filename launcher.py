"""
Unified launcher for Procurement Agent.
Runs both the FastAPI backend and the Streamlit frontend.
Supports standard Python execution and PyInstaller standalone frozen executables.
Handles graceful shutdown of both services on Ctrl+C.
"""
import os
import sys
import time
import signal
import subprocess
import urllib.request
import webbrowser
from pathlib import Path

# When frozen by PyInstaller, sys._MEIPASS holds the bundled resources directory.
IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    APP_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "8501"))


def wait_for_backend(url: str, timeout: int = 15) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def run_backend_service():
    """Direct entrypoint for backend service."""
    os.chdir(APP_DIR)
    if str(BUNDLE_DIR) not in sys.path:
        sys.path.insert(0, str(BUNDLE_DIR))
    import uvicorn
    if IS_FROZEN:
        from backend.app.main import app
        uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT, log_level="info")
    else:
        uvicorn.run(
            "backend.app.main:app",
            host=BACKEND_HOST,
            port=BACKEND_PORT,
            reload=True,
            reload_dirs=[str(BUNDLE_DIR / "backend")],
            log_level="info"
        )


def run_frontend_service():
    """Direct entrypoint for frontend service."""
    os.chdir(APP_DIR)
    if str(BUNDLE_DIR) not in sys.path:
        sys.path.insert(0, str(BUNDLE_DIR))
    from streamlit.web import cli as stcli
    frontend_script = BUNDLE_DIR / "frontend" / "streamlit_app.py"
    sys.argv = [
        "streamlit",
        "run",
        str(frontend_script),
        "--global.developmentMode=false",
        "--server.port",
        str(FRONTEND_PORT),
        "--server.headless",
        "true",
    ]
    stcli.main()


def supervisor():
    os.chdir(APP_DIR)
    if str(BUNDLE_DIR) not in sys.path:
        sys.path.insert(0, str(BUNDLE_DIR))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(BUNDLE_DIR)

    print("=" * 60)
    print("  Starting MARG Pharmaceutical Procurement Agent")
    print("=" * 60)
    print(f"[*] Starting FastAPI backend on http://{BACKEND_HOST}:{BACKEND_PORT} ...")

    if IS_FROZEN:
        backend_cmd = [sys.executable, "--run-backend"]
        frontend_cmd = [sys.executable, "--run-frontend"]
    else:
        launcher_file = str(BUNDLE_DIR / "launcher.py")
        backend_cmd = [sys.executable, launcher_file, "--run-backend"]
        frontend_cmd = [sys.executable, launcher_file, "--run-frontend"]

    backend_proc = subprocess.Popen(backend_cmd, env=env)

    # Wait for backend to be healthy
    health_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/health"
    if not wait_for_backend(health_url, timeout=20):
        print(f"[!] Warning: Backend health check timed out at {health_url}. Starting frontend anyway.")
    else:
        print(f"[+] Backend is ready at http://{BACKEND_HOST}:{BACKEND_PORT} (Docs: http://{BACKEND_HOST}:{BACKEND_PORT}/docs)")

    # Start Streamlit UI
    print(f"[*] Starting Streamlit UI on http://localhost:{FRONTEND_PORT} ...")
    frontend_proc = subprocess.Popen(frontend_cmd, env=env)

    # Automatically open browser
    try:
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{FRONTEND_PORT}")
    except Exception:
        pass

    def shutdown(signum, frame):
        print("\n[*] Shutting down Procurement Agent services...")
        frontend_proc.terminate()
        backend_proc.terminate()
        try:
            frontend_proc.wait(timeout=5)
            backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            frontend_proc.kill()
            backend_proc.kill()
        print("[+] Application stopped cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Monitor both processes
    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"[!] Backend stopped unexpectedly (exit code {backend_proc.returncode}).")
                shutdown(None, None)
            if frontend_proc.poll() is not None:
                print(f"[!] Frontend stopped unexpectedly (exit code {frontend_proc.returncode}).")
                shutdown(None, None)
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    if len(sys.argv) > 1:
        if sys.argv[1] == "--run-backend":
            run_backend_service()
            sys.exit(0)
        elif sys.argv[1] == "--run-frontend":
            run_frontend_service()
            sys.exit(0)

    supervisor()

