FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Non-root user first, so the dependency layers are written already owned by
# it. (A `chown -R /app` after `uv sync` rewrote the whole ~7.6 GB venv into
# a second layer and doubled the image.) --no-cache keeps uv's download cache
# out of the layers; the runtime cache is UV_CACHE_DIR below.
RUN useradd -r -s /bin/false mnemo \
    && mkdir -p /app/.cache/uv \
    && chown -R mnemo:mnemo /app
USER mnemo

# Install dependencies first (cached layer)
COPY --chown=mnemo:mnemo pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Copy source and install the project itself
COPY --chown=mnemo:mnemo . .
RUN uv sync --frozen --no-dev --no-cache

# Bake build metadata (pass via: docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) .)
ARG BUILD_COMMIT=unknown
ENV BUILD_COMMIT=${BUILD_COMMIT}

EXPOSE 8000

# HF model cache lives on a volume mount at /app/.cache
ENV HF_HOME=/app/.cache
ENV TRANSFORMERS_CACHE=/app/.cache
ENV UV_CACHE_DIR=/app/.cache/uv

CMD ["uv", "run", "uvicorn", "mnemo.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
