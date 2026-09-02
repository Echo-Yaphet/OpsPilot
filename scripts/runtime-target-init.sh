#!/bin/sh
set -eu

"$@" &
target_pid=$!

forward_and_wait() {
  kill -TERM "$target_pid" 2>/dev/null || true
  wait "$target_pid" || true
  exit 0
}

trap forward_and_wait TERM INT
wait "$target_pid"
