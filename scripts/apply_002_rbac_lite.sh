#!/usr/bin/env bash
# Apply the 002_rbac_lite migration to both production and test databases.
# Must be run as a user who can sudo to postgres (or directly as postgres).
#
# Usage:
#   sudo -u postgres bash scripts/apply_002_rbac_lite.sh

set -euo pipefail

MIGRATION="migrations/002_rbac_lite.sql"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SQL_FILE="$SCRIPT_DIR/$MIGRATION"

if [ ! -f "$SQL_FILE" ]; then
    echo "ERROR: Migration file not found: $SQL_FILE"
    exit 1
fi

echo "Applying $MIGRATION to mnemo..."
psql -d mnemo -f "$SQL_FILE"

echo "Applying $MIGRATION to mnemo_test..."
psql -d mnemo_test -f "$SQL_FILE"

echo "Done."
