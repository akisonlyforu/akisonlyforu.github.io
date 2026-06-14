#!/bin/bash
# Turn a fresh postgres container into a streaming standby of `primary`.
# On first boot (empty data dir) it pg_basebackups from the primary, writes a
# standby.signal + primary_conninfo (-R), and sets recovery_min_apply_delay so
# the lag is deterministic. Then it hands off to the normal postgres entrypoint.
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
DELAY="${RECOVERY_MIN_APPLY_DELAY:-250ms}"
PRIMARY_HOST="${PRIMARY_HOST:-primary}"
PRIMARY_PORT="${PRIMARY_PORT:-5432}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: waiting for primary ${PRIMARY_HOST}:${PRIMARY_PORT} ..."
  until pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U replicator -q; do
    sleep 1
  done

  echo "replica: pg_basebackup from ${PRIMARY_HOST}:${PRIMARY_PORT} ..."
  rm -rf "${PGDATA:?}"/*
  gosu postgres pg_basebackup \
    -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U replicator \
    -D "$PGDATA" -Fp -Xs -R -P

  # a hot standby requires these to be >= the primary's values; the primary sets
  # them via -c flags (not in the copied config file), so pin them here too.
  gosu postgres tee -a "$PGDATA/postgresql.auto.conf" >/dev/null <<-CONF
	recovery_min_apply_delay = '${DELAY}'
	max_connections = 200
	max_wal_senders = 10
	max_replication_slots = 10
	hot_standby = on
	CONF
  echo "replica: base backup complete, apply delay = ${DELAY}"
fi

exec docker-entrypoint.sh postgres
