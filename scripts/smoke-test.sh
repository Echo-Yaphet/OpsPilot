#!/usr/bin/env sh
set -eu
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
container_metrics=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=container_cpu_metrics_up{service="payment-service"} == 1')
case "$container_metrics" in
  *'"result":[]'*)
    echo "payment-service container CPU metrics are unavailable" >&2
    exit 1
    ;;
esac
curl -fsS -X POST http://localhost:8080/api/v1/incidents/analyze \
  -H 'content-type: application/json' \
  -d '{"service":"payment-service","symptom":"dependency unavailable"}'
echo
