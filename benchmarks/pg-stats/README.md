# pg_stats benchmark harness

This is the harness behind [The Lie in pg_stats](../../collections/_posts/2025-01-06-the-130x-lie-in-pg-stats.md). It runs a digest-pinned PostgreSQL 16.14 image locally and reproduces the `ORDER BY id LIMIT 1` planner trap discussed in the post.

The seed is deterministic: 20,000,000 append-ordered audit events, 82% with a NULL `session_id`, and 20,000 non-null sessions with 180 physically adjacent events apiece. A 5,000,000-row seed with 18-event bursts did not reproduce the bad plan, and neither did 20,000,000 rows with 18-event bursts, so the checked-in run follows the post's disclosed escalation and tightens the clustering naturally. The benchmark measures the same query at per-column statistics targets 100, 2000, and 5000. `ANALYZE` sampling is random, and both target-5000 samples are kept in the CSV instead of pretending the seed makes statistics sampling deterministic.

These are laptop measurements for demonstrating the planner mechanism. They are not production capacity numbers.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/pg-stats
docker compose up -d --wait

python3 -m venv /tmp/pg-stats-bench-venv
source /tmp/pg-stats-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all --reset
docker compose down -v
```

PostgreSQL is bound to loopback on host port `55433`. Override the connection without editing the script:

```bash
export PG_STATS_BENCH_DSN='dbname=pg_stats_bench user=stats_bench password=stats_bench host=127.0.0.1 port=55433'
```

The checked-in defaults are the ones used for the post. The 20-million-row table and its indexes use about 2.1 GB in the checked-in run; leave extra Docker disk space for index construction and temporary work. `--reset` is mandatory because the command truncates the dedicated benchmark tables. The harness validates every workload argument first, then verifies the database name and an identity marker before it does that work. It refuses result paths outside this benchmark's `results/` directory and stages a complete run before replacing older evidence files.

`--rows`, `--block-size`, `--events-per-session`, `--target-session`, and `--statistics-targets` are available for investigation, but changing them produces a different experiment.

## Results

The harness writes these files under `results/`:

- `statistics.csv` contains `null_frac`, raw and resolved `n_distinct`, the real distinct count, and the miss ratio after every `ANALYZE` pass.
- `query_results.csv` contains the chosen plan, estimated and actual matching rows, rows removed by the filter, shared-buffer counts, and execution time.
- `explain_target_*.txt` is the untouched PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)` output used by the post.
- `run_metadata.csv` records the machine, database version, seed shape, targets, and planner settings.
- `attempts/5m-18-events/` and `attempts/20m-18-events/` preserve the CSVs and plans from the two smaller shapes where target 100 correctly kept using the session index.

Every measured query gets one unrecorded warm-up before its captured `EXPLAIN`, so the latency comparison does not put the first plan alone on a cold database. The command fails after preserving its evidence if target 100 does not choose the primary-key scan, or if target 5000 does not flip to the `session_id` index. That is deliberate: a planner story that did not happen locally does not belong in the post.
