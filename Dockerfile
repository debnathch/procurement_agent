# Multi-stage production Dockerfile using official astral-sh/uv image
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency specifications
COPY pyproject.toml uv.lock ./

# Install application dependencies without dev packages
RUN uv sync --frozen --no-dev --no-install-project

# Final runtime image
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    FRONTEND_PORT=8501

# Copy installed venv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code and config
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY launcher.py ./
COPY .env.example ./.env

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

ENTRYPOINT ["python", "launcher.py"]
