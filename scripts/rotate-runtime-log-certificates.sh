#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if [ -z "${RUNTIME_LOG_SECRET_SOURCE_DIR:-}" ]; then
  echo "RUNTIME_LOG_SECRET_SOURCE_DIR must point to an externally delivered bundle" >&2
  exit 2
fi

secret_dir=${RUNTIME_LOG_SECRET_DIR:-"$project_root/work/runtime-log-secrets"}
promtail_id_before=$(docker compose ps -q promtail)
if [ -z "$promtail_id_before" ]; then
  echo "Promtail must be running before a zero-downtime rotation" >&2
  exit 1
fi

RUNTIME_LOG_SECRET_INSTALL_PHASE=validate \
  "$project_root/scripts/install-runtime-log-secrets.py"
for service in user-service order-service payment-service; do
  current_certificate="$secret_dir/current/clients/$service/cert.pem"
  if [ -f "$current_certificate" ]; then
    if [ -f "$RUNTIME_LOG_SECRET_SOURCE_DIR/crl.pem" ]; then
      openssl verify -purpose sslclient -crl_check \
        -CAfile "$RUNTIME_LOG_SECRET_SOURCE_DIR/ca.pem" \
        -CRLfile "$RUNTIME_LOG_SECRET_SOURCE_DIR/crl.pem" \
        "$current_certificate" >/dev/null
    else
      openssl verify -purpose sslclient \
        -CAfile "$RUNTIME_LOG_SECRET_SOURCE_DIR/ca.pem" \
        "$current_certificate" >/dev/null
    fi
  fi
done

openssl verify -purpose sslserver \
  -CAfile "$RUNTIME_LOG_SECRET_SOURCE_DIR/ca.pem" \
  "$secret_dir/current/gateway/server-cert.pem" >/dev/null

RUNTIME_LOG_SECRET_INSTALL_PHASE=gateway-trust \
  "$project_root/scripts/install-runtime-log-secrets.py"
docker compose kill -s HUP promtail

RUNTIME_LOG_SECRET_INSTALL_PHASE=clients \
  "$project_root/scripts/install-runtime-log-secrets.py"
promtail_image=$(docker compose images -q promtail)
docker run --rm --entrypoint /bin/true \
  -v "$secret_dir/current/clients:/run/opspilot-runtime-log-clients:ro" \
  "$promtail_image"

for service in user-service order-service payment-service; do
  recreate_attempt=0
  until docker compose up -d --no-deps --force-recreate "$service"; do
    recreate_attempt=$((recreate_attempt + 1))
    if [ "$recreate_attempt" -ge 5 ]; then
      echo "$service could not read the projected Docker logging secrets" >&2
      exit 1
    fi
    sleep 1
  done
  port=8001
  [ "$service" = "order-service" ] && port=8002
  [ "$service" = "payment-service" ] && port=8003
  attempt=0
  until curl -fsS "http://localhost:$port/health" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "$service did not recover during certificate rotation" >&2
      exit 1
    fi
    sleep 1
  done
done

RUNTIME_LOG_SECRET_INSTALL_PHASE=gateway-identity \
  "$project_root/scripts/install-runtime-log-secrets.py"
docker compose kill -s HUP promtail

"$project_root/scripts/validate-runtime-log-mtls.py"
promtail_id_after=$(docker compose ps -q promtail)
if [ "$promtail_id_before" != "$promtail_id_after" ]; then
  echo "Promtail was recreated during certificate rotation" >&2
  exit 1
fi
echo "Runtime-log certificates rotated without restarting the TLS receiver or Promtail"
