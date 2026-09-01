#!/usr/bin/env sh
set -eu
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
for service in user-service order-service payment-service; do
  loki_attempt=0
  while [ "$loki_attempt" -lt 10 ]; do
    service_logs=$(curl -fsS -G http://localhost:3100/loki/api/v1/query_range \
      --data-urlencode "query={compose_service=\"$service\"}" \
      --data-urlencode 'limit=1' \
      --data-urlencode 'direction=backward' \
      --data-urlencode 'since=5m')
    case "$service_logs" in
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
    echo "$service logs are unavailable in Loki" >&2
    exit 1
  fi
done
service_log_freshness_attempt=0
while [ "$service_log_freshness_attempt" -lt 10 ]; do
  curl -fsS http://localhost:8001/health >/dev/null
  curl -fsS http://localhost:8002/health >/dev/null
  curl -fsS http://localhost:8003/health >/dev/null
  fresh_service_logs=$(curl -fsS -G http://localhost:9090/api/v1/query \
    --data-urlencode 'query=sum(opspilot_service_log_read_fresh{service=~"user-service|order-service|payment-service"}) == 3')
  case "$fresh_service_logs" in
    *'"result":[]'*)
      service_log_freshness_attempt=$((service_log_freshness_attempt + 1))
      sleep 2
      ;;
    *)
      break
      ;;
  esac
done
if [ "$service_log_freshness_attempt" -eq 10 ]; then
  echo "per-service Promtail log freshness is unavailable" >&2
  exit 1
fi
runtime_forwarding=$(curl -fsS -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=count(increase(promtail_runtime_forwarded_lines_total{compose_service=~"user-service|order-service|payment-service"}[1m]) > 0) == 3')
case "$runtime_forwarding" in
  *'"result":[]'*)
    echo "three-service runtime log forwarding is unavailable" >&2
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
