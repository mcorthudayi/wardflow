#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for name in simulator processor api dashboard; do
  if [ -f ".pids/$name.pid" ]; then
    kill "$(cat ".pids/$name.pid")" 2> /dev/null || true
    rm -f ".pids/$name.pid"
  fi
done

pkill -f "simulator/simulate.py" 2> /dev/null || true
pkill -f "processor/processor.py" 2> /dev/null || true
pkill -f "WardFlow.Api" 2> /dev/null || true
pkill -f "wardflow/dashboard/node_modules" 2> /dev/null || true

if [ "${1:-}" = "--all" ]; then
  docker compose down
fi

echo "WardFlow stopped"
