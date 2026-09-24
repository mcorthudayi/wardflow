#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SPEED="${WARDFLOW_SPEED:-600}"
API_URL="http://localhost:5090"
DASHBOARD_URL="http://127.0.0.1:5174"
STARTED=$SECONDS

step() { printf '\n==> %s\n' "$1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

step "Checking prerequisites"
for tool in docker python3 dotnet npm openssl curl; do
  command -v "$tool" > /dev/null || fail "$tool is not installed"
done
docker info > /dev/null 2>&1 || fail "Docker is not running. Start Docker Desktop and try again."

if [ ! -f .env ]; then
  {
    echo "POSTGRES_USER=wardflow"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "POSTGRES_DB=wardflow"
    echo "POSTGRES_HOST=localhost"
    echo "POSTGRES_PORT=5434"
    echo "WARDFLOW_BROKERS=127.0.0.1:19092"
    echo "WARDFLOW_TOPIC=adt.events"
    echo "API_DB_PASSWORD=$(openssl rand -hex 24)"
  } > .env
  echo "Created .env with generated passwords"
fi
grep -q '^API_DB_PASSWORD=' .env || echo "API_DB_PASSWORD=$(openssl rand -hex 24)" >> .env
if grep -q '=change_me$' .env; then
  fail ".env still contains change_me placeholders"
fi

set -a
source .env
set +a

step "Stopping anything left running"
./scripts/down.sh > /dev/null 2>&1 || true
mkdir -p logs .pids

step "Installing dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r simulator/requirements.txt -r processor/requirements.txt
[ -d dashboard/node_modules ] || npm --prefix dashboard install --silent
dotnet build api/WardFlow.Api -v quiet -nologo > logs/build.log 2>&1 || { cat logs/build.log; fail "API build failed"; }

step "Starting Redpanda and PostgreSQL"
docker compose up -d --wait

step "Resetting the stream and hospital state"
./scripts/reset.sh
./scripts/setup_api_role.sh > /dev/null

launch() {
  local name="$1"
  shift
  nohup "$@" > "logs/$name.log" 2>&1 &
  echo $! > ".pids/$name.pid"
}

wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 90); do
    curl -sf "$url" > /dev/null 2>&1 && return 0
    sleep 1
  done
  fail "$name did not become ready, see logs/$name.log"
}

step "Starting processor, API, simulator and dashboard"
launch processor .venv/bin/python processor/processor.py
launch api ./scripts/api.sh
wait_for api "$API_URL/health"
launch simulator .venv/bin/python simulator/simulate.py --speed "$SPEED"
launch dashboard npm --prefix dashboard run dev
wait_for dashboard "$DASHBOARD_URL"

step "Waiting for 3 simulated hours so the forecast can warm up"
for _ in $(seq 1 90); do
  curl -sf "$API_URL/api/forecast" | grep -q '"ready":true' && break
  sleep 1
done

step "Running smoke tests"
.venv/bin/python scripts/smoke_test.py || echo "Some smoke tests failed, see the output above"

cat << INFO

WardFlow is running ($((SECONDS - STARTED)) s)
  Dashboard        $DASHBOARD_URL
  API              $API_URL/api/overview
  Redpanda Console http://localhost:8080
  Logs             logs/processor.log, logs/api.log, logs/simulator.log, logs/dashboard.log
  Stop             ./scripts/down.sh   (add --all to stop containers too)
INFO

if [ "${WARDFLOW_NO_OPEN:-0}" != "1" ]; then
  open "$DASHBOARD_URL" 2>/dev/null || true
fi
