# Redis OOM benchmark harness

This is the harness behind [Redis Said It Was Fine. The OOM Killer Didn't.](../../collections/_posts/2025-02-06-redis-said-it-was-fine.md). It runs a digest-pinned Redis 7.4 container under a hard cgroup memory limit (`mem_limit`) and reproduces the memory fragmentation gap between Redis `used_memory` and OS `used_memory_rss`, leading to cgroup OOM kills.

The benchmark loads a queue-shaped workload of 220,000 small keys (each with a 200-byte value) to establish a baseline memory footprint. It then bulk deletes all of them, showing that while Redis `used_memory` drops immediately to ~1MB, `used_memory_rss` remains pinned near its peak because the jemalloc allocator holds onto the freed pages. It demonstrates that Redis eviction (`maxmemory` eviction) does not fire because it compares against `used_memory` rather than RSS, resulting in an OOM kill under cgroup enforcement. Finally, it validates two fixes: enabling active defragmentation (`activedefrag yes` with low threshold overrides to allow scanning) and incremental deletes (`UNLINK` in batches).

These are laptop measurements for demonstrating the memory allocation and kernel cgroup rules. They are not production capacity sizing numbers.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/redis-oom
docker compose up -d --wait

python3 -m venv /tmp/redis-oom-bench-venv
source /tmp/redis-oom-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all --reset
docker compose down -v
```

Redis is bound to loopback on host port `56379`. Override the connection DSN user without editing the script:

```bash
export REDIS_OOM_BENCH_URL='redis://127.0.0.1:56379/0'
```

The checked-in defaults reproduce the exact numbers in the post. The `--reset` flag is mandatory for the main run to flush the database cleanly. The harness validates the database name and a custom marker `redis_oom_bench_marker` before performing the destructive flush.

`--keys`, `--value-bytes`, `--maxmemory`, and `--delete-batch` are available for tuning, but changing them changes the experiment shape.

## Results

The harness writes these files under `results/`:

- `memory_snapshots.csv` contains memory metrics captured at key checkpoints: `before` (loaded), `after` (post-bulk-delete), `defrag` (after active defrag), and `incremental` (after UNLINK delete).
- `memory_timeline.csv` contains a detailed time-series of metrics sampled every 50ms across all phases, including the OOM kill event.
- `run_metadata.csv` captures the host platform, runtime versions, Redis version, and workload tunables.
- `info_memory_*.txt` holds the raw, untouched `INFO memory` dumps from the Redis server at each checkpoint.
- `attempts/` contains files from unsuccessful test configurations (e.g., if a different container limit did not trigger the OOM).

The command fails at the end if the baseline does not demonstrate the RSS fragmentation gap, if active defrag fails to reclaim RSS, if incremental delete does not lower peak RSS, or if the container is not OOM killed in the final simulation phase.
