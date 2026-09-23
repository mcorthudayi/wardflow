#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
source .env
set +a

QUERY=$(cat << 'SQL'
SELECT 'bed held by a visit that is not inpatient' AS rule, count(*) AS violations
FROM ops.beds b
LEFT JOIN ops.visits v ON v.visit_id = b.occupied_by
WHERE b.occupied_by IS NOT NULL AND (v.visit_id IS NULL OR v.status <> 'inpatient')
UNION ALL
SELECT 'inpatient without a bed', count(*)
FROM ops.visits v
WHERE v.status = 'inpatient' AND NOT EXISTS (SELECT 1 FROM ops.beds b WHERE b.occupied_by = v.visit_id)
UNION ALL
SELECT 'visit holding more than one bed', count(*)
FROM (SELECT occupied_by FROM ops.beds WHERE occupied_by IS NOT NULL GROUP BY occupied_by HAVING count(*) > 1) AS doubled
UNION ALL
SELECT 'discharged visit without discharge time', count(*)
FROM ops.visits WHERE status = 'discharged' AND discharged_at IS NULL
UNION ALL
SELECT 'ward over capacity', count(*)
FROM ops.v_unit_status WHERE kind = 'ward' AND occupied > capacity
SQL
)

docker exec wardflow-postgres psql -P pager=off -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$QUERY"
TOTAL=$(docker exec wardflow-postgres psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT coalesce(sum(violations), 0) FROM ($QUERY) AS checks")

if [ "$TOTAL" -eq 0 ]; then
  echo "All invariants hold"
else
  echo "$TOTAL invariant violations" >&2
  exit 1
fi
