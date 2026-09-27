# ── Stage 1: build dependencies ──────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build tools needed by some wheels (e.g. lxml, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies into an isolated prefix so the final
# stage can simply copy /install without the full build toolchain.
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Make /install packages available
    PYTHONPATH=/install/lib/python3.12/site-packages \
    PATH="/install/bin:$PATH" \
    # Service ports (override with -e at runtime)
    BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    FRONTEND_PORT=8501 \
    # Tell launcher we are running inside Docker (disables browser auto-open)
    DOCKER_RUNTIME=1

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /install

# Copy application source
COPY backend/  ./backend/
COPY frontend/ ./frontend/
COPY scripts/  ./scripts/
COPY launcher.py ./

# Copy default config (real secrets should be injected via -e or --env-file)
COPY .env.example ./.env

# Create a writable data directory for the SQLite database
RUN mkdir -p /data && chmod 777 /data

# Expose both service ports
EXPOSE 8000 8501

# Health-check polls the FastAPI /health endpoint every 30 s
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

# Single entrypoint: launcher.py starts both backend + frontend
ENTRYPOINT ["python", "launcher.py"]
