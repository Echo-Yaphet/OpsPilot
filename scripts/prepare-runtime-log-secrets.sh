#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${RUNTIME_LOG_SECRET_SOURCE_DIR:-}" ]; then
  "$project_root/scripts/generate-runtime-log-pki.sh"
  RUNTIME_LOG_SECRET_SOURCE_DIR=${RUNTIME_LOG_PKI_DIR:-"$project_root/work/runtime-log-pki"}
  export RUNTIME_LOG_SECRET_SOURCE_DIR
fi

exec "$project_root/scripts/install-runtime-log-secrets.py"
