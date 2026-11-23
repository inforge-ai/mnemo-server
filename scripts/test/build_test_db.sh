#!/usr/bin/env bash
# Build a disposable test database: schema.sql + `mnemo migrate`.
# Usage: scripts/test/build_test_db.sh [host] [port]
# Expects a superuser `postgres` with PGPASSWORD set (or trust auth), e.g. the
# pgvector/pgvector:pg16 container started by `make`-less local setup:
#   docker run -d --name mnemo-test-pg -p 127.0.0.1:5433:5432 \
#     -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
set -euo pipefail
HOST="${1:-localhost}"
PORT="${2:-5433}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export PGPASSWORD="${PGPASSWORD:-postgres}"
psql -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -U postgres -d postgres -f "$HERE/bootstrap.sql"
PGPASSWORD=mnemo_migrator_test_pw psql -v ON_ERROR_STOP=1 -q -h "$HOST" -p "$PORT" \
  -U mnemo_migrator -d mnemo_test -f "$ROOT/schema.sql"
cd "$ROOT"
UV_FROZEN=1 uv run mnemo migrate \
  --database-url "postgresql://mnemo_migrator:mnemo_migrator_test_pw@$HOST:$PORT/mnemo_test"
PGPASSWORD=mnemo_migrator_test_pw psql -v ON_ERROR_STOP=1 -q -h "$HOST" -p "$PORT" \
  -U mnemo_migrator -d mnemo_test -f "$HERE/test_only_grants.sql"
