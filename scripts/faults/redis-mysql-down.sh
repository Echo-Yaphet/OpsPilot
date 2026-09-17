#!/usr/bin/env sh
set -eu

curl -fsS -X POST http://localhost:3001/api/control/api/v1/faults/redis-down \
  -H 'content-type: application/json' -d '{"approved":true}'
echo
curl -fsS -X POST http://localhost:3001/api/control/api/v1/faults/mysql-down \
  -H 'content-type: application/json' -d '{"approved":true}'
echo
echo "Redis and MySQL stopped by their OS-isolated actuators. Generating health traffic."
for port in 8001 8002 8003; do
  curl -sS "http://localhost:${port}/health" >/dev/null || true
done
echo "Wait for Prometheus to observe both failures, then analyze and explicitly approve the ordered plan."
