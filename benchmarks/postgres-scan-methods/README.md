# Postgres scan-methods benchmark harness

This harness reproduces the three ways PostgreSQL 16 reaches a row -- **Sequential Scan**, **Index Scan**, and **Index-Only Scan** -- on a digest-pinned `postgres:16.14` image, and captures real numbers for each. It is the harness behind a blog post; the post is written separately from these measurements.

The table is one deterministically seeded `events` table: 5,000,000 append-ordered rows, `user_id` spread across ~1,000,000 distinct values (~5 rows each) via a fixed multiply-mod so there is no `random()` anywhere, `status = id % 5`, `bucket = id % 1000`. After the load the harness runs `ANALYZE`, and `VACUUM` at the exact points where the visibility map matters. About 616 MB on disk with its indexes.

These are laptop measurements for demonstrating the planner and executor mechanics. They are not production capacity numbers.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/postgres-scan-methods
docker compose up -d --wait

python3 -m venv /tmp/scan-bench-venv
source /tmp/scan-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all --reset
docker compose down -v
```

PostgreSQL is bound to loopback on host port `55434` (pg-stats uses `55433`; these do not clash). Override the connection with environment variables instead of editing the script -- `PGHOST` (default `127.0.0.1`), `PGPORT` (default `55434`), `PGUSER`/`PGDATABASE`/`PGPASSWORD` (default `scan_bench`). `RESULTS_DIR` overrides where results land (default `./results`).

`--reset` is mandatory because the run truncates the dedicated `events` table; the harness verifies the database name and an identity marker before it seeds. `--rows`, `--reps`, `--reps-exp3`, `--range-width`, and `--sweep` are available for investigation, but changing them produces a different experiment. The full run takes roughly two minutes.

### Method and pinned settings

Every query is warmed once, untimed, before it is measured, so the comparison is steady-state plan behaviour rather than cold-cache IO. Latency distributions (min/median/p95/mean over the reps) come from Python `perf_counter` around `cur.execute`; plan node types, buffers, and heap fetches come from `EXPLAIN (ANALYZE, BUFFERS)`.

Parallelism is pinned off (`max_parallel_workers_per_gather = 0`, `max_parallel_maintenance_workers = 0`) and `jit = off`, so the numbers isolate the scan method rather than core count or JIT warmup. `work_mem = 64MB`, `track_io_timing = on`. All of this is recorded in `run_metadata.csv`. The container is given `shm_size: 1gb` so `VACUUM` and index builds do not hit the default 64 MB `/dev/shm` limit.

## The three experiments

**Experiment 1 -- point lookup: Seq Scan vs Index Scan.** `SELECT * FROM events WHERE user_id = k`, measured with no index (forced Seq Scan) and then with a b-tree on `user_id` (Index Scan).

**Experiment 2 -- covering query: Index Scan vs Index-Only Scan.** `SELECT id FROM events WHERE user_id BETWEEN a AND b`. With a plain `events(user_id)` index the executor must fetch each `id` from the heap (Index Scan). With a covering `events(user_id) INCLUDE (id)` index it can answer from the index alone -- but only after `VACUUM` sets the visibility map. Bitmap scans are disabled inside this experiment so the planner picks the pure Index Scan vs Index-Only Scan contrast; a Bitmap Heap Scan neither reports a `Heap Fetches` line nor lets the index-only path show through, so it would hide the exact mechanism this experiment isolates. The "before VACUUM" attempt is kept under `results/attempts/` on purpose: it shows an Index-Only Scan node that still does `Heap Fetches: 5005`, which is the honest proof that `VACUUM` is what makes an index-only scan actually skip the heap.

**Experiment 3 -- selectivity crossover: when the planner drops the index.** `SELECT sum(amount) FROM events WHERE bucket < n`, sweeping `n` so selectivity runs 0.1% -> 90%. `amount` is deliberately *not* in the `bucket` index, so the index path must fetch every matching row from the heap -- that per-row random heap access is what eventually loses to a Seq Scan. (A `count(*)` here would be an Index-Only Scan that never touches the heap and never loses, so it would show no crossover at all; that is exactly why the query sums a non-indexed column.) For each `n` the harness records the planner's own free choice, then forces a pure Index Scan and a Seq Scan and times both.

## Results

These are the checked-in numbers (Apple Silicon, PostgreSQL 16.14). Full detail is in `summary.txt`, the per-experiment CSVs, and the untouched `EXPLAIN` text files.

### Experiment 1 -- 530x on a point lookup

| plan | median | `EXPLAIN` exec | shared buffers | rows |
|------|-------:|---------------:|---------------:|-----:|
| Seq Scan (no index)  | 134.03 ms | 131.9 ms | 41,667 (hit 12,401 / read 29,266) | 5 |
| Index Scan (b-tree)  | 0.22 ms | 0.02 ms | 8 (hit 3 / read 5) | 5 |

The seq scan reads the whole 41,667-page table every time and filters 4,999,995 rows away to return 5. The index scan touches eight buffers. Same answer, ~530x apart in median latency.

### Experiment 2 -- VACUUM is what turns a heap fetch into zero

| plan | median | `Heap Fetches` | shared buffers |
|------|-------:|---------------:|---------------:|
| plain index (Index Scan, heap fetch) | 1.76 ms | -- (implicit) | 5,015 |
| covering index, **before** VACUUM (Index Only Scan) | -- | **5,005** | 5,027 |
| covering index, **after** VACUUM (Index Only Scan) | 1.26 ms | **0** | 2,024 |

Same query, same covering index. The only difference between the second and third rows is one `VACUUM`. Before it, the visibility map is empty and the "index-only" scan quietly fetches all 5,005 rows from the heap anyway. After it, `Heap Fetches: 0` and the buffers touched drop from ~5k to ~2k.

### Experiment 3 -- the crossover

| n | selectivity | planner's choice | forced Index Scan | forced Seq Scan | faster |
|--:|------------:|------------------|------------------:|----------------:|:------:|
| 1   | 0.1%  | Bitmap Heap Scan | 1.09 ms   | 149.2 ms | index |
| 10  | 1.0%  | Bitmap Heap Scan | 11.79 ms  | 171.5 ms | index |
| 100 | 10.0% | Bitmap Heap Scan | 109.5 ms  | 159.1 ms | index |
| 200 | 20.0% | Bitmap Heap Scan | 221.4 ms  | 165.9 ms | **seq** |
| 500 | 50.0% | Bitmap Heap Scan | 607.2 ms  | 191.0 ms | seq |
| 900 | 90.0% | **Seq Scan**     | 1528.6 ms | 265.1 ms | seq |

A forced pure Index Scan wins up to ~10% selectivity and a Seq Scan overtakes it by ~20% -- that is the crossover. The planner never actually picks the pure random-access Index Scan here: it reaches for a **Bitmap Heap Scan** as the middle ground (which sorts heap access into physical order and stays much faster than the forced index scan), and only switches all the way to a Seq Scan at 90%. Bitmap heap scan showing up across most of the range is itself the point -- it is the planner hedging between the two extremes.

## Files under `results/`

- `summary.txt` -- the human-readable rollup printed at the end of a run.
- `exp1_point_lookup.csv`, `exp2_covering.csv`, `exp3_crossover.csv` -- one row per plan/predicate with plan type, buffers, heap fetches, and the latency distribution.
- `exp1_latency_*.csv`, `exp2_latency_*.csv` -- the raw per-rep `perf_counter` samples behind the distributions.
- `explain_exp1_*.txt`, `explain_exp2_*.txt` -- the untouched `EXPLAIN (ANALYZE, BUFFERS)` output.
- `run_metadata.csv` -- machine, Postgres version, image digest, row counts, table size, and every pinned planner setting.
- `attempts/` -- the before-VACUUM index-only scan (`Heap Fetches: 5005`) and every Experiment 3 `EXPLAIN` (planner choice, forced index, forced seq, per `n`).

The run fails, after preserving its evidence, if Experiment 1 does not produce a Seq Scan without the index and an index scan with it, or if Experiment 2 does not reach an Index-Only Scan with `Heap Fetches: 0` after `VACUUM`. A plan story that did not actually happen locally does not belong in the post.
