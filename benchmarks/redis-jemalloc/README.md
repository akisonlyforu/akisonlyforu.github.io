# Redis Allocator Benchmark: jemalloc vs libc

This is the harness behind [Redis Brings Its Own malloc, and Here's Why](../../collections/_posts/2026-07-20-redis-brings-its-own-malloc.md). It compiles the exact same Redis 7.4 source code two ways (statically linked with jemalloc vs dynamically linked with glibc's `ptmalloc`) and runs an identical churn workload against both to compare fragmentation and active defragmentation capabilities.

The harness loads 50,000 keys of various size classes, then runs 5 rounds of churn (overwriting 20% of keys with different sizes, deleting 20% of keys, and inserting 20% new keys). It settles the databases and captures comparison snapshot metrics showing that while `used_memory` is roughly identical on both allocators, glibc accumulates significantly higher resident memory `used_memory_rss` and `mem_fragmentation_ratio` due to external fragmentation under keyspace churn. Finally, it tests `activedefrag yes` (with low ignore-bytes and threshold overrides), showing that jemalloc successfully compacts allocations to reclaim RSS, while libc either refuses the configuration command or acts as a no-op.

These are laptop measurements for demonstrating allocator memory layout and fragmentation behaviors. They are not production capacity sizing numbers.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/redis-jemalloc
docker compose up -d --build --wait

python3 -m venv /tmp/redis-je-bench-venv
source /tmp/redis-je-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all --reset
docker compose down -v
```

The jemalloc build binds to loopback on host port `56380`. The libc build binds to loopback on host port `56381`. You can override the connections using environment variables:

```bash
export REDIS_JE_URL='redis://127.0.0.1:56380/0'
export REDIS_LIBC_URL='redis://127.0.0.1:56381/0'
```

The `--reset` flag is mandatory to flush the database cleanly. The harness validates a custom marker `redis_jemalloc_bench_marker` before performing the destructive flush.

## Results

The harness writes these files under `results/`:

- `comparison.csv` contains settled memory metrics comparing both the `jemalloc` and `libc` builds after the churn phase.
- `memory_timeline_je.csv` and `memory_timeline_libc.csv` contain time-series of metrics sampled every 50ms during each run.
- `run_metadata.csv` captures runtime details, versions, the allocator per build, and workload tunables.
- `info_memory_jemalloc.txt` and `info_memory_libc.txt` hold raw, untouched `INFO memory` dumps from each server at the settled checkpoint.
- `info_memory_je_defrag.txt` holds raw `INFO memory` after active defragmentation on the jemalloc build.
- `attempts/` contains results from non-separating workload configurations.
