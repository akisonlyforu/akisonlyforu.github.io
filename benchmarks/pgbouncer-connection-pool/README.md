# pgbouncer connection pool harness

This harness runs a digest-pinned Postgres 16 and a PgBouncer pooler in front of
it, then runs the same workload twice, once straight at Postgres and once through
PgBouncer, to measure what a connection pooler actually buys you.

Postgres forks a full backend process (and does auth) for every new connection,
and it has a hard `max_connections` ceiling. PgBouncer in **transaction pooling**
mode keeps a small warm set of server backends and multiplexes many short-lived
client connections over them. To make the ceiling reachable on a laptop, Postgres
here runs with `max_connections=25` (minus 3 superuser-reserved, so ~22 usable),
and PgBouncer with `max_client_conn=1000` but `default_pool_size=10`.

Three experiments, each run **WITHOUT** (direct to Postgres `:55432`) and **WITH**
(via PgBouncer `:56432`):

- **A. Short-lived connection latency & throughput** — 5000 workload units, each a
  fresh connection that runs one tiny query and closes, across 15 worker threads.
  This is the per-request connect-churn pattern. Reports throughput and
  p50/p95/p99. The pooler wins because it skips the per-unit backend fork + auth.
- **B. Connection exhaustion under a burst** — 100 simultaneous clients (well above
  `max_connections`), each connecting and doing a brief `pg_sleep(0.05)` so they
  overlap. Direct, a large chunk fails with the verbatim `FATAL: sorry, too many
  clients already`. Through the pooler, clients queue and 0 fail.
- **C. Backend process count & memory** — hold 20 clients doing brief active work
  and, from a separate direct admin connection, sample `pg_stat_activity` client
  backends plus total Postgres-process RSS (via `ps` inside the container). Direct
  spawns one backend per client; the pooler stays near `default_pool_size`.

These are laptop measurements demonstrating the mechanism, not production numbers.
The ratios matter, not the absolutes.

## Run it

Docker with Compose v2, plus Python 3.10+.

```bash
cd benchmarks/pgbouncer-connection-pool
docker compose up -d --wait          # postgres on :55432, pgbouncer on :56432

python3 -m venv /tmp/pgb-venv && source /tmp/pgb-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

Everything is env-configurable: `PG_HOST`, `DIRECT_PORT`, `POOLER_PORT`,
`PG_DB` / `PG_USER` / `PG_PASSWORD`, `UNITS`, `A_CONCURRENCY`, `B_CONCURRENCY`,
`C_HOLD`, `PG_CONTAINER`, and `RESULTS_DIR`.

The harness runs all three WITHOUT experiments before any WITH experiment, so the
pooler's warm server pool can never pollute the direct-phase measurements, and it
drains Postgres back to zero client backends between experiments.

## Results

Numbers below are from one laptop run (Postgres 16.14, PgBouncer 1.25.2). Yours
will differ; the shape is the point.

| Experiment | Metric | WITHOUT (direct) | WITH (pooler) |
| --- | --- | ---: | ---: |
| A | throughput (units/s) | 1590 | 3289 |
| A | p99 latency (ms) | 17.01 | 9.37 |
| B | success / fail (of 100) | 25 / 75 | 100 / 0 |
| B | error rate | 75% | 0% |
| C | client backends (20 held) | 20 | 10 |
| C | total Postgres RSS (KB) | 36573 | 36453 |

- `summary.txt` — the captured console run.
- `exp_a_latencies.csv` — per-unit latency (ms) for both cases (10000 rows).
- `exp_b_outcomes.csv` — per-worker success/fail + error string for both cases.
- `exp_c_backends.csv` — held clients, sampled client backends, and RSS per case.
- `run_metadata.csv` — versions, both image digests, all the knobs, and headline numbers.

### What reproduces cleanly, and what is lumpy

- **A** reproduces cleanly: the pooler is consistently ~2x throughput and roughly
  half the p99. Absolute numbers move with laptop load.
- **B** reproduces cleanly in shape (direct always loses a big chunk to `too many
  clients`, pooler always 0 failures), but the exact direct success count is lumpy
  (~15–25 of 100) because it depends on how many of the ~22 usable slots happen to
  be free at the instant of the burst.
- **C** backend *count* is the clean signal (20 direct vs 10 pooled = the
  `default_pool_size` cap). The RSS numbers barely move, which is honest rather
  than dramatic: `ps` RSS counts each backend's mapped `shared_buffers` (shared
  memory double-counted across processes), and an idle/sleeping backend's own
  private memory is tiny, so halving the backend count does not visibly halve the
  summed RSS at this scale. Count, not RSS, is the memory story here.

## Config

- `docker-compose.yml` — both services, digest-pinned, loopback-only ports, both healthchecked, PgBouncer `depends_on` Postgres healthy.
- `pgbouncer/pgbouncer.ini` — `pool_mode=transaction`, `max_client_conn=1000`, `default_pool_size=10`, `server_idle_timeout=5` (so an idle warm pool doesn't leak into the next experiment).
- `pgbouncer/userlist.txt` — trust auth, matches Postgres `POSTGRES_HOST_AUTH_METHOD=trust`.
- `initdb/01-seed.sql` — a one-row `bench_seed` table the per-unit query reads.
