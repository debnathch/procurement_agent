# MARG Pharmaceutical Procurement Agent

Local-first, human-approved procurement agent for pharmaceutical distribution with MARG ERP as the system of record.

## Included design

*   MARG Excel ingestion with report-specific profiles
*   Future MARG API adapter converging into the same normalization/validation pipeline
*   Canonical procurement data model
*   Demand, inventory, supplier and FEFO/expiry-aware procurement logic
*   Deterministic guardrails
*   Human approval and feedback loop
*   Dry-run / CSV / future MARG execution adapters
*   FastAPI backend + Streamlit UI
*   SQLite local development database
*   Tests and architecture/[LLD documentation](LLD.md)

## Safety defaults

The default execution mode is `dry_run`. Real MARG execution is disabled until the exact connector contract is configured and validated.

## Local setup (using uv)

Ensure [`uv`](https://docs.astral.sh/uv/) is installed (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
# 1. Install dependencies & sync virtual environment
uv sync

# 2. Set up environment configuration
cp .env.example .env

# 3. Seed demo database
# uv run python scripts/generate_demo_data.py

# 4. Start the FastAPI backend
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
# Start Streamlit UI
uv run streamlit run frontend/streamlit_app.py
```

Run test suite:

```bash
uv run pytest
```

Alternative: Traditional setup with venv & pip

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/generate_demo_data.py
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
# In another terminal:
streamlit run frontend/streamlit_app.py
```

API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)  
UI: [http://localhost:8501](http://localhost:8501)

### Stopping the Application

- **If running in the foreground**: Press `Ctrl + C` in both terminal windows (FastAPI and Streamlit).
- **If running in the background (or to free ports 8000 and 8501)**:
  ```bash
  lsof -ti:8000 -ti:8501 | xargs kill -9
  ```

## MARG data flow

MARG ERP -> Excel/API Adapter -> Common Normalization -> Validation/Cleaning -> Canonical Data Store -> Procurement Agent -> Human Approval -> Execution Adapter -> MARG

## Source bundle

The complete generated MARG-native v1.1 source bundle is also available in the ChatGPT conversation as:  
`marg_procurement_agent_marg_native_excel_v1_1.zip`

Do not commit local `.env`, credentials, virtual environments, databases, caches, or real customer MARG exports.