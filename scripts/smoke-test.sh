#!/usr/bin/env sh
set -eu
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
loki_attempt=0
while [ "$loki_attempt" -lt 10 ]; do
  payment_logs=$(curl -fsS -G http://localhost:3100/loki/api/v1/query_range \
    --data-urlencode 'query={compose_service="payment-service"}' \
    --data-urlencode 'limit=1' \
    --data-urlencode 'direction=backward' \
    --data-urlencode 'since=5m')
  case "$payment_logs" in
    *'"result":[]'*)
      loki_attempt=$((loki_attempt + 1))
      sleep 2
      ;;
    *)
      break
      ;;
  esac
done
if [ "$loki_attempt" -eq 10 ]; then
  echo "payment-service logs are unavailable in Loki" >&2
  exit 1
fi
log_target_publication=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=docker_proxy_log_target_publication_up == 1')
case "$log_target_publication" in
  *'"result":[]'*)
    echo "Promtail log target publication is unavailable" >&2
    exit 1
    ;;
esac
fresh_log_targets=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=time() - docker_proxy_log_target_publication_last_success_timestamp_seconds < 15')
case "$fresh_log_targets" in
  *'"result":[]'*)
    echo "Promtail log targets are stale" >&2
    exit 1
    ;;
esac
active_log_targets=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=promtail_targets_active_total == 3')
case "$active_log_targets" in
  *'"result":[]'*)
    echo "Promtail does not have all three log targets active" >&2
    exit 1
    ;;
esac
container_metrics=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=container_cpu_metrics_up{service="payment-service"} == 1')
case "$container_metrics" in
  *'"result":[]'*)
    echo "payment-service container CPU metrics are unavailable" >&2
    exit 1
    ;;
esac
container_threshold=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=container_cpu_alert_threshold_ratio{service="payment-service"} == 0.8')
case "$container_threshold" in
  *'"result":[]'*)
    echo "payment-service container CPU threshold is not active" >&2
    exit 1
    ;;
esac
fresh_container_metrics=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=time() - container_cpu_metrics_last_success_timestamp_seconds{service="payment-service"} < 30')
case "$fresh_container_metrics" in
  *'"result":[]'*)
    echo "payment-service container CPU metrics are stale" >&2
    exit 1
    ;;
esac
curl -fsS -X POST http://localhost:8080/api/v1/incidents/analyze \
  -H 'content-type: application/json' \
  -d '{"service":"payment-service","symptom":"dependency unavailable"}'
echo
