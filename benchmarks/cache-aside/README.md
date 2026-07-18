# Cache-aside benchmark harness

This is the harness behind the measurements in [Everything I Got Wrong About Cache-Aside](../../collections/_posts/2026-07-18-cache-aside-is-four-lines.md). It runs PostgreSQL 16 and Redis 7 locally, seeds 100,000 users, then exercises the four failure modes discussed in the post.

The numbers are useful for comparing the shape of each approach on the same machine. They are not capacity numbers for production hardware.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/cache-aside
docker compose up -d --wait

python3 -m venv /tmp/cache-aside-bench-venv
source /tmp/cache-aside-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all
docker compose down -v
```

PostgreSQL is exposed on host port `55432`, Redis on `56379`, so the harness does not take the usual development ports. Override either connection without editing the script:

```bash
export CACHE_BENCH_PG_DSN='dbname=cache_bench user=cache_bench password=cache_bench host=127.0.0.1 port=55432'
export CACHE_BENCH_REDIS_URL='redis://127.0.0.1:56379/0'
```

Each experiment can also run alone:

```bash
python benchmark.py race
python benchmark.py stampede
python benchmark.py jitter
python benchmark.py baseline
```

Use `python benchmark.py all --help` for workload controls. The defaults are the ones used for the checked-in results.

## What each experiment does

- `race` runs readers and a writer against one hot user. To make the unlucky overlap repeatable, the harness expires the key 2 ms before each write; it then sweeps a disclosed 0/5/20 ms delay between the reader's DB read and cache `SET`. The sampler reads the row twice through a fresh autocommit connection with the Redis read between those two queries. Samples where the database changed between reads are discarded; cache misses count as non-stale wall-clock time. The four runs compare the naive 1 second TTL, a 60 ms TTL, delayed double-delete, and a Redis version/CAS guard.
- `stampede` releases 500 reader threads after one hot key expires. The loader includes a deliberate 10 ms `pg_sleep`, and DB hits are counted in the application immediately before the PostgreSQL call. The database pool is capped at 32 connections so the naive case exhibits the same queueing pressure an application pool would.
- `jitter` populates 5,000 keys together with either a fixed 2 second TTL or ±10% jitter. Twenty-four reader threads scan in 100-key batches while keys that fall through to the DB loader are counted in 100 ms buckets. The PostgreSQL reads and Redis refills are batched so client round trips do not flatten the expiry wave before it can be measured. The short TTL keeps the run practical; the experiment is about the shape of that wave.
- `baseline` replays the same 20,000 Zipf-distributed user IDs with 32 workers, first uncached and then through Redis. The CSV contains hit rate, DB calls/QPS, request throughput, and p50/p99 latency.

## Results

The harness writes these files under `results/`:

- `race.csv`
- `stampede.csv`
- `ttl_jitter_timeseries.csv`
- `ttl_jitter_summary.csv`
- `baseline.csv`
- `run_metadata.csv`

The checked-in CSV files came from one full run on the machine described in `run_metadata.csv`. Run-to-run timing will move; the relative shape is the part the post uses.
