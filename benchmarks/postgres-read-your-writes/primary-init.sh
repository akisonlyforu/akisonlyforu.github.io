#!/bin/bash
# Runs once, during the primary's initdb phase (before the real server starts).
# Opens replication connections and creates the streaming-replication role.
set -euo pipefail

echo "host replication all all trust" >> "$PGDATA/pg_hba.conf"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE ROLE replicator WITH REPLICATION LOGIN;
SQL
