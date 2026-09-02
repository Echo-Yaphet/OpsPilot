#!/bin/sh
set -eu

health=$(curl -sS http://localhost:8003/health || true)

case "$health" in
  *'"redis":false'*)
    curl -fsS -X POST http://localhost:8080/api/v1/incidents/analyze \
      -H 'content-type: application/json' \
      -d '{"service":"payment-service","symptom":"Redis unavailable","execute":true,"approved":true}'
    echo
    ;;
esac

case "$health" in
  *'"mysql":false'*)
    curl -fsS -X POST http://localhost:8080/api/v1/incidents/analyze \
      -H 'content-type: application/json' \
      -d '{"service":"payment-service","symptom":"MySQL unavailable","execute":true,"approved":true}'
    echo
    ;;
esac
