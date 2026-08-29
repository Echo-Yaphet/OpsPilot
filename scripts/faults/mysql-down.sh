#!/usr/bin/env sh
set -eu
docker compose stop mysql
echo "MySQL stopped. Generate health traffic, wait about 15 seconds, then analyze."
for port in 8001 8002 8003; do curl -sS "http://localhost:${port}/health" >/dev/null || true; done

