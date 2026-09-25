# MARG Pharmaceutical Procurement Agent

Local-first, human-approved procurement agent for pharmaceutical distribution with MARG ERP as the system of record.

## Included design

* MARG Excel ingestion with report-specific profiles
* Future MARG API adapter converging into the same normalization/validation pipeline
* Canonical procurement data model
* Demand, inventory, supplier and FEFO/expiry-aware procurement logic
* Deterministic guardrails
* Human approval and feedback loop
* Dry-run / CSV / future MARG execution adapters
* FastAPI backend + Streamlit UI
* SQLite local development database
* Tests and architecture/[LLD documentation](LLD.md)
* Standalone executable compilation (PyInstaller) & multi-stage Dockerfile
* Industry-standard GitHub Actions CI/CD pipeline

## Safety defaults

The default execution mode is `dry_run`. Real MARG execution is disabled until the exact connector contract is configured and validated.

---

## Quick Start (One-Click Launchers)

Double-click to automatically set up the environment, install dependencies, and launch the application:

* **macOS**: Double-click [`run_mac.command`](run_mac.command) in Finder.
* **Windows**: Double-click [`run_windows.bat`](run_windows.bat) in File Explorer.

Or run manually from the command line:

```bash
# 1. Install dependencies & sync virtual environment with uv
uv sync

# 2. Set up environment configuration
cp .env.example .env

# 3. Start both backend and frontend together
uv run python launcher.py
```

* API Docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
* Streamlit UI: [http://localhost:8501](http://localhost:8501)

### Stopping the Application
* **Foreground**: Press `Ctrl + C` in the launcher terminal window (stops both services gracefully).
* **Background / Clear Ports**:
  ```bash
  lsof -ti:8000 -ti:8501 | xargs kill -9
  ```

---

## Separate Service Execution (Development Mode)

If you prefer running services in separate terminal windows:

```bash
# Terminal 1: FastAPI Backend
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2: Streamlit UI
uv run streamlit run frontend/streamlit_app.py
```

### Running Tests & Linting

```bash
# Run test suite
uv run pytest

# Run linter
uv run ruff check .
```

---

## Standalone Executable Build

You can bundle the entire application into a standalone executable package (without requiring end users to install Python or dependencies):

```bash
uv run python scripts/build_executable.py
```

The output will be placed in `dist/procurement-agent/`. Users can launch it directly:
* **macOS / Linux**: `./dist/procurement-agent/procurement-agent`
* **Windows**: `dist\procurement-agent\procurement-agent.exe`

---

## Docker Deployment

Build and run using the optimized multi-stage Docker container:

```bash
# Build Docker image
docker build -t procurement-agent .

# Run container
docker run -d -p 8000:8000 -p 8501:8501 --name procurement-app procurement-agent
```

---

## CI/CD Pipeline (GitHub Actions)

The repository includes a production-grade CI/CD pipeline defined in [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml).

### Pipeline Stages

1. **Lint & Code Style (`quality`)**:
   - Runs `ruff check` to enforce code quality and standards.
2. **Automated Testing (`test`)**:
   - Installs dependencies using `uv` and runs the full `pytest` suite.
3. **Multi-OS Standalone Executable Builds (`build-executable`)**:
   - Builds standalone binaries in parallel on:
     - **Linux x86_64** (`procurement-agent-linux-x86_64.tar.gz`)
     - **macOS ARM64** (`procurement-agent-macos-arm64.tar.gz`)
     - **Windows x64** (`procurement-agent-windows-x64.zip`)
   - Uploads compiled binaries as workflow artifacts on every build.
4. **Docker Verification (`docker-build`)**:
   - Verifies container build integrity via Docker Buildx.
5. **Automated GitHub Release (`release`)**:
   - Triggered automatically whenever a version tag is pushed (e.g. `v1.0.0`).
   - Generates release notes and attaches downloadable executables for all platforms.

### Triggering a Release

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## MARG Data Flow

MARG ERP -> Excel/API Adapter -> Common Normalization -> Validation/Cleaning -> Canonical Data Store -> Procurement Agent -> Human Approval -> Execution Adapter -> MARG

Do not commit local `.env`, credentials, virtual environments, databases, caches, or real customer MARG exports.