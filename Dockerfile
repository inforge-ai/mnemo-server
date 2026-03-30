FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY . .
RUN uv sync --frozen --no-dev

# Bake build metadata (pass via: docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) .)
ARG BUILD_COMMIT=unknown
ENV BUILD_COMMIT=${BUILD_COMMIT}

EXPOSE 8000

# HF model cache lives on a volume mount at /app/.cache
ENV HF_HOME=/app/.cache
ENV TRANSFORMERS_CACHE=/app/.cache
ENV UV_CACHE_DIR=/app/.cache/uv

# Run as non-root user
RUN useradd -r -s /bin/false mnemo \
    && mkdir -p /app/.cache/uv \
    && chown -R mnemo:mnemo /app
USER mnemo

CMD ["uv", "run", "uvicorn", "mnemo.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
