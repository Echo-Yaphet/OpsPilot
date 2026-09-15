#!/bin/sh
set -eu

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  if [ -n "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "refusing to initialize a non-empty standby data directory" >&2
    exit 1
  fi
  until PGPASSWORD="$POSTGRES_REPLICATION_PASSWORD" pg_basebackup \
    --host=memory-db-stage10-primary \
    --username=opspilot_replication \
    --pgdata="$PGDATA" \
    --format=plain \
    --wal-method=stream \
    --write-recovery-conf \
    --checkpoint=fast
  do
    sleep 1
  done
  chmod 0700 "$PGDATA"
fi

exec docker-entrypoint.sh postgres
