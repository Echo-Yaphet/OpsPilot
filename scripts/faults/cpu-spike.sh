#!/usr/bin/env sh
set -eu
echo "Generating bounded CPU work in payment-service for 30 seconds..."
curl -sS "http://localhost:8003/work?seconds=30"

