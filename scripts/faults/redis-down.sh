#!/usr/bin/env sh
set -eu
curl -fsS -X POST http://localhost:8080/api/v1/faults/redis-down \
  -H 'content-type: application/json' -d '{"approved":true}'
echo
echo "Redis stopped by its OS-isolated actuator. Generate health traffic, wait about 15 seconds, then analyze."
for port in 8001 8002 8003; do curl -sS "http://localhost:${port}/health" >/dev/null || true; done
