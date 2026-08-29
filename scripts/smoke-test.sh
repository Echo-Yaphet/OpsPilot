#!/usr/bin/env sh
set -eu
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS -X POST http://localhost:8080/api/v1/incidents/analyze \
  -H 'content-type: application/json' \
  -d '{"service":"payment-service","symptom":"dependency unavailable"}'
echo

