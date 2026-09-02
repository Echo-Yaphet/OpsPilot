#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=${RUNTIME_LOG_SECRET_SOURCE_DIR:-}
vault_mount=${RUNTIME_LOG_VAULT_KV_MOUNT:-secret}
vault_path=${RUNTIME_LOG_VAULT_KV_PATH:-opspilot/runtime-log}

if [ -z "$source_dir" ]; then
  echo "RUNTIME_LOG_SECRET_SOURCE_DIR must point to a complete bundle" >&2
  exit 2
fi
if ! command -v vault >/dev/null 2>&1; then
  echo "Vault CLI is required to publish the runtime-log bundle" >&2
  exit 2
fi

RUNTIME_LOG_SECRET_INSTALL_PHASE=validate \
  "$project_root/scripts/install-runtime-log-secrets.py"

set -- \
  "bundle.json=@$source_dir/bundle.json" \
  "ca.pem=@$source_dir/ca.pem" \
  "server-cert.pem=@$source_dir/server-cert.pem" \
  "server-key.pem=@$source_dir/server-key.pem" \
  "user-service-cert.pem=@$source_dir/user-service-cert.pem" \
  "user-service-key.pem=@$source_dir/user-service-key.pem" \
  "order-service-cert.pem=@$source_dir/order-service-cert.pem" \
  "order-service-key.pem=@$source_dir/order-service-key.pem" \
  "payment-service-cert.pem=@$source_dir/payment-service-cert.pem" \
  "payment-service-key.pem=@$source_dir/payment-service-key.pem"
if [ -f "$source_dir/crl.pem" ]; then
  set -- "$@" "crl.pem=@$source_dir/crl.pem"
fi

vault kv put -mount="$vault_mount" "$vault_path" "$@"
echo "Published one atomic Vault KV version at $vault_mount/$vault_path"
