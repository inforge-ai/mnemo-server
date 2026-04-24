#!/usr/bin/env bash
# Apply the 005_remembered_on migration to both production and test databases.
#
# Run as the mnemo user (NOT as postgres): the SQL file lives under /home/mnemo
# which postgres can't traverse. This script reads the file as mnemo and pipes
# its contents into `sudo -u postgres psql`, so only psql runs as postgres.
#
# Usage:
#   bash scripts/apply_005_remembered_on.sh

set -euo pipefail

MIGRATION="migrations/005_remembered_on.sql"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SQL_FILE="$SCRIPT_DIR/$MIGRATION"

if [ ! -f "$SQL_FILE" ]; then
    echo "ERROR: Migration file not found: $SQL_FILE"
    exit 1
fi

echo "Applying $MIGRATION to mnemo..."
sudo -u postgres psql -d mnemo < "$SQL_FILE"

echo "Applying $MIGRATION to mnemo_test..."
sudo -u postgres psql -d mnemo_test < "$SQL_FILE"

echo "Done."
