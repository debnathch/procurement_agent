"""
Unified launcher for Procurement Agent.
Runs both the FastAPI backend and the Streamlit frontend in a single process / command.
Handles graceful shutdown of both services on Ctrl+C.
"""
import os
import sys
import time
import signal
import subprocess
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
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


def main():
    os.chdir(ROOT_DIR)
    sys.path.insert(0, str(ROOT_DIR))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR)

    print("=" * 60)
    print("  Starting MARG Pharmaceutical Procurement Agent")
    print("=" * 60)
    print(f"[*] Starting FastAPI backend on http://{BACKEND_HOST}:{BACKEND_PORT} ...")

    # Start FastAPI backend
    backend_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app.main:app",
        "--host",
        BACKEND_HOST,
        "--port",
        str(BACKEND_PORT),
    ]

    backend_proc = subprocess.Popen(backend_cmd, env=env)

    # Wait for backend to be healthy
    health_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/health"
    if not wait_for_backend(health_url, timeout=20):
        print(f"[!] Warning: Backend health check timed out at {health_url}. Starting frontend anyway.")
    else:
        print(f"[+] Backend is ready at http://{BACKEND_HOST}:{BACKEND_PORT} (Docs: http://{BACKEND_HOST}:{BACKEND_PORT}/docs)")

    # Start Streamlit UI
    print(f"[*] Starting Streamlit UI on http://localhost:{FRONTEND_PORT} ...")
    frontend_script = ROOT_DIR / "frontend" / "streamlit_app.py"
    frontend_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(frontend_script),
        "--server.port",
        str(FRONTEND_PORT),
        "--server.headless",
        "true",
    ]

    frontend_proc = subprocess.Popen(frontend_cmd, env=env)

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
    main()
