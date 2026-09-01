#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=${RUNTIME_LOG_PKI_DIR:-"$project_root/work/runtime-log-pki"}
force=${RUNTIME_LOG_PKI_FORCE:-false}
if [ "${1:-}" = "--force" ]; then
  force=true
elif [ "$#" -gt 0 ]; then
  printf 'Usage: %s [--force]\n' "$0" >&2
  exit 2
fi

required_files="bundle.json ca-key.pem ca.pem server-key.pem server-cert.pem"
for service in user-service order-service payment-service; do
  required_files="$required_files $service-key.pem $service-cert.pem"
done

if [ -d "$output_dir" ] && [ "$force" != "true" ]; then
  complete=true
  for required_file in $required_files; do
    if [ ! -f "$output_dir/$required_file" ]; then
      complete=false
      break
    fi
  done
  if [ "$complete" = "true" ]; then
    printf 'Reusing runtime log mTLS material in %s\n' "$output_dir"
    exit 0
  fi
fi

parent_dir=$(dirname -- "$output_dir")
mkdir -p "$parent_dir"
temporary_dir=$(mktemp -d "$parent_dir/.runtime-log-pki.XXXXXX")

cleanup() {
  rm -rf "$temporary_dir"
}
trap cleanup EXIT HUP INT TERM

openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
  -keyout "$temporary_dir/ca-key.pem" \
  -out "$temporary_dir/ca.pem" \
  -days 3650 \
  -subj '/CN=OpsPilot Runtime Log CA' >/dev/null 2>&1

cat >"$temporary_dir/server-ext.cnf" <<'EOF'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:host.docker.internal,DNS:promtail,DNS:localhost,IP:127.0.0.1
EOF

openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout "$temporary_dir/server-key.pem" \
  -out "$temporary_dir/server.csr" \
  -subj '/CN=host.docker.internal' >/dev/null 2>&1
openssl x509 -req -sha256 \
  -in "$temporary_dir/server.csr" \
  -CA "$temporary_dir/ca.pem" \
  -CAkey "$temporary_dir/ca-key.pem" \
  -CAcreateserial \
  -out "$temporary_dir/server-cert.pem" \
  -days 825 \
  -extfile "$temporary_dir/server-ext.cnf" >/dev/null 2>&1

for service in user-service order-service payment-service; do
  cat >"$temporary_dir/$service-ext.cnf" <<EOF
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectAltName=DNS:$service
EOF
  openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -keyout "$temporary_dir/$service-key.pem" \
    -out "$temporary_dir/$service.csr" \
    -subj "/CN=$service" >/dev/null 2>&1
  openssl x509 -req -sha256 \
    -in "$temporary_dir/$service.csr" \
    -CA "$temporary_dir/ca.pem" \
    -CAkey "$temporary_dir/ca-key.pem" \
    -CAcreateserial \
    -out "$temporary_dir/$service-cert.pem" \
    -days 825 \
    -extfile "$temporary_dir/$service-ext.cnf" >/dev/null 2>&1
done

rm -f "$temporary_dir"/*.csr "$temporary_dir"/*-ext.cnf "$temporary_dir"/*.srl
chmod 600 "$temporary_dir"/*-key.pem
chmod 644 "$temporary_dir"/*-cert.pem "$temporary_dir/ca.pem"
bundle_version="local-$(date -u +%Y%m%dT%H%M%SZ)"
printf '{"version":"%s","issuer":"OpsPilot local development PKI"}\n' \
  "$bundle_version" >"$temporary_dir/bundle.json"

if [ -d "$output_dir" ]; then
  backup_dir="$output_dir.previous.$$"
  mv "$output_dir" "$backup_dir"
  mv "$temporary_dir" "$output_dir"
  rm -rf "$backup_dir"
else
  mv "$temporary_dir" "$output_dir"
fi
trap - EXIT HUP INT TERM

printf 'Generated runtime log mTLS material in %s\n' "$output_dir"
