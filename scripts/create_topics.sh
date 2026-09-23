#!/usr/bin/env bash
set -euo pipefail

TOPIC="${WARDFLOW_TOPIC:-adt.events}"

if docker exec wardflow-redpanda rpk topic describe "$TOPIC" > /dev/null 2>&1; then
  echo "Topic $TOPIC already exists"
else
  docker exec wardflow-redpanda rpk topic create "$TOPIC" \
    --partitions 6 \
    --replicas 1 \
    --topic-config retention.ms=604800000
fi
