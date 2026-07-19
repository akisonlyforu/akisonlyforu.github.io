# SQL Server lock-escalation harness

It runs a digest-pinned SQL Server 2022 and reproduces lock escalation: the point
where SQL Server stops taking one lock per row and instead grabs a single
TABLE-level lock. Once that table lock is exclusive, other transactions can't
touch *any* row of the table — even rows the updater never looked at — until it
commits.

Three experiments against a 200,000-row `orders` table
(`id INT PRIMARY KEY CLUSTERED, status, amount, filler`):

- **A. Escalation blocks the whole table** — update 8000 rows in an open
  transaction and watch the row (KEY) locks collapse into one `OBJECT` X lock in
  `sys.dm_tran_locks`. Then time a point `SELECT` on an *untouched* row (id 150000)
  from another connection: it blocks until the updater commits.
- **B. The ~5000-lock cliff** — sweep the update size and find where an `OBJECT` X
  lock appears and a concurrent point `SELECT` flips from sub-millisecond to
  blocked/timed-out.
- **C. The fix: batching** — update 50,000 rows as one big statement (escalates →
  table lock → readers block) vs in 2000-row committed batches (no escalation →
  readers get through), plus a `LOCK_ESCALATION = DISABLE` contrast arm. Each
  transaction holds its locks for ~3s so the effect isn't drowned out by how fast
  50k rows update under emulation.

Escalation is detected from a *separate* monitor connection querying
`sys.dm_tran_locks WHERE request_session_id = <updater SPID>` — an `OBJECT` lock in
X mode (not the normal IX intent lock) with the KEY count collapsed to 0 means it
escalated.

These are laptop measurements demonstrating the mechanism, not production capacity
numbers. The image is amd64-only and runs under emulation on arm64 hosts (slow to
start; give the healthcheck time).

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/sqlserver-lock-escalation
docker compose up -d --wait          # SQL Server on 127.0.0.1:11434

python3 -m venv /tmp/mssql-venv && source /tmp/mssql-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

Env overrides: `MSSQL_HOST` (127.0.0.1), `MSSQL_PORT` (11434), `MSSQL_SA_PASSWORD`,
`RESULTS_DIR`.

## Results

- `summary.txt` — the captured console run.
- `a_escalation_blocking.csv` — lock counts by resource type/mode, plus baseline vs
  blocked point-`SELECT` latency on an untouched row.
- `b_threshold_sweep.csv` — per update size: KEY/PAGE lock counts, OBJECT lock mode,
  escalated flag, and the concurrent point-`SELECT` latency + blocked flag.
- `c_batching.csv` — naive vs batched vs escalation-disabled: updater duration and
  concurrent-reader p50/p99/max latency and blocked count.
- `run_metadata.csv` — SQL Server version, image digest, row count, observed
  escalation threshold, host/port.
- `attempts/` — anything that didn't reproduce cleanly, with a note.

The checked-in run is SQL Server 2022 (16.0.4265.3), 200,000 rows. Escalation first
appeared between a 6000-row and a 7000-row update (6000 KEY locks held without
escalating) — a bit north of the documented ~5000-lock threshold. Below it the
concurrent point `SELECT` on an untouched row returned in under 1 ms; at/above it
the same `SELECT` blocked until it hit its 800 ms lock timeout.
