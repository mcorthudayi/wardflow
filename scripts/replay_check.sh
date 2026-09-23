#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
source .env
set +a

sql() { docker exec wardflow-postgres psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"; }

fingerprint() {
  sql "SELECT md5(
    coalesce((SELECT string_agg(concat_ws(',', visit_id, status, current_unit, bed_id, admitted_at, discharged_at), '|' ORDER BY visit_id) FROM ops.visits), '') ||
    coalesce((SELECT string_agg(concat_ws(',', bed_id, occupied_by), '|' ORDER BY bed_id) FROM ops.beds), '') ||
    coalesce((SELECT string_agg(concat_ws(',', captured_at, ed_census, beds_occupied), '|' ORDER BY captured_at) FROM ops.kpi_snapshots), ''))"
}

BEFORE=$(fingerprint)
EVENTS=$(sql "SELECT count(*) FROM ops.processed_events")
echo "Fingerprint before replay: $BEFORE ($EVENTS events applied)"

docker exec wardflow-redpanda rpk group seek wardflow-processor --to start > /dev/null
echo "Consumer group rewound to the start of the topic, replaying everything..."
python processor/processor.py --once 2>&1 | tail -n 1

AFTER=$(fingerprint)
echo "Fingerprint after replay:  $AFTER"

if [ "$BEFORE" = "$AFTER" ]; then
  echo "PASS: replaying the full topic left the state unchanged"
else
  echo "FAIL: state changed after replay" >&2
  exit 1
fi
