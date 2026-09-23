#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

set -a
source "$ROOT_DIR/.env"
set +a

export ConnectionStrings__WardFlow="Host=${POSTGRES_HOST};Port=${POSTGRES_PORT};Database=${POSTGRES_DB};Username=wardflow_api;Password=${API_DB_PASSWORD}"
exec dotnet run --project "$ROOT_DIR/api/WardFlow.Api" --launch-profile http
