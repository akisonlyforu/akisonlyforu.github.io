# postgres read-your-writes / LSN-gated routing harness

This is the harness behind the read-your-writes post.

It runs a digest-pinned PostgreSQL 16 **primary** and one **streaming replica**
and measures the stale-read window you get when an app writes to the primary and
then immediately reads from the replica, plus the fix: routing reads on the WAL
LSN so a user always sees their own writes while the replica still absorbs most
of the read traffic.

The replica runs with `recovery_min_apply_delay = 250ms`, so it deliberately
holds every commit for 250ms before applying it. That makes the lag
*deterministic* — the stale window is reproducible instead of lumpy — which is
the whole point of pinning it.

Two experiments, both against the real primary + replica:

- **A. The stale window** — seed `user_counter(user_id, val)`, then for each
  trial `UPDATE` a row on the primary, wait `D` milliseconds, and `SELECT` it
  back from the replica. A read is *stale* if the replica's value isn't the one
  just written. `D` is swept over `{0, 50, 100, 200, 250, 300, 500, 1000}` ms.
  Expect stale% high below the 250ms apply boundary and dropping to zero above
  it — a cliff.
- **B. The gate** — a mixed read/write workload over many users, run twice on the
  *same* seeded op stream:
  - **naive**: every read goes to the replica.
  - **gated**: after each write, capture `pg_current_wal_lsn()` per user; before a
    read, check `pg_last_wal_replay_lsn() >= that LSN`. Caught up → replica.
    Behind → fall back to the primary. (The per-user LSN dict stands in for the
    Redis you'd use in production.)

  Writes touch a small hot set of users; "own-recent" reads hit the user that was
  just written (the read-your-writes worst case), "browse" reads hit anyone. The
  headline is the contrast between the two modes on identical traffic.

These are laptop measurements demonstrating the mechanism, not capacity-planning
numbers. The apply delay is pinned to 250ms so the cliff lands in a predictable
place; on a real replica the lag is whatever your network, write volume, and
`hot_standby_feedback` make it, and it moves around.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/postgres-read-your-writes
docker compose up -d --wait          # primary on :55442, replica on :55444

python3 -m venv /tmp/ryw-venv && source /tmp/ryw-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary_console.txt
docker compose down -v
```

The replica container base-backups from the primary on first boot (see
`replica-entrypoint.sh`), writes `standby.signal` + `primary_conninfo` via
`pg_basebackup -R`, and pins `recovery_min_apply_delay`. `primary-init.sh` opens
a replication `pg_hba` line and creates the `replicator` role. Ports are bound to
`127.0.0.1` only.

Override hosts/ports/results with `PRIMARY_HOST`, `PRIMARY_PORT`, `REPLICA_HOST`,
`REPLICA_PORT`, `RESULTS_DIR`, `SEED`.

## Results

- `experiment_a_stale_window.csv` — stale% and observed lag per read-after-write delay.
- `experiment_b_gate.csv` — naive vs gated: stale%, replica-served%, primary-fallback%.
- `summary.txt` — human-readable key numbers.
- `run_metadata.csv` — postgres version, image digest, apply delay, trial counts, seed, docker/python versions, date.
- `attempts/` — non-reproducing or superseded runs, kept for honesty.

The mechanism (a replica applies WAL behind the primary, so a read right after a
write can miss it) is not specific to the 250ms number; that's just the knob that
makes the window land where the benchmark can see it every time.
