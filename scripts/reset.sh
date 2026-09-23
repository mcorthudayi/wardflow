#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
source .env
set +a

TOPIC="${WARDFLOW_TOPIC:-adt.events}"

docker exec wardflow-redpanda rpk topic delete "$TOPIC" > /dev/null 2>&1 || true
docker exec wardflow-redpanda rpk group delete wardflow-processor > /dev/null 2>&1 || true
sleep 2
./scripts/create_topics.sh

docker exec -i wardflow-postgres psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 << 'SQL'
TRUNCATE ops.visits, ops.processed_events, ops.dead_letter, ops.kpi_snapshots;
UPDATE ops.beds SET occupied_by = NULL, assigned_at = NULL, updated_at = now();
SQL

echo "Stream and operational state reset"
