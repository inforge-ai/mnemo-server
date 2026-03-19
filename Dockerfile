FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# HF model cache lives on a volume mount at /app/.cache
ENV HF_HOME=/app/.cache
ENV TRANSFORMERS_CACHE=/app/.cache

CMD ["uv", "run", "uvicorn", "mnemo.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
