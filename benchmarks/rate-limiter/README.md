# Rate-limiter benchmark harness

This is the harness behind the measurements in [Everything I Got Wrong About Rate Limiting](../../collections/_posts/2026-07-18-rate-limiter-is-three-lines.md). It runs a Redis 7 primary and replica locally, then exercises the five behaviors discussed in the post.

The results compare strategies on one laptop under Docker. They are useful for the shape of a trade-off, not as production capacity numbers.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/rate-limiter
docker compose up -d --wait

python3 -m venv /tmp/rate-limiter-bench-venv
source /tmp/rate-limiter-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py all
docker compose down -v
```

The primary is exposed on host port `56380` and its replica on `56381`, so this stack can run beside the cache-aside benchmark. Override either URL without editing the harness:

```bash
export RATE_LIMIT_PRIMARY_URL='redis://127.0.0.1:56380/0'
export RATE_LIMIT_REPLICA_URL='redis://127.0.0.1:56381/0'
```

Each experiment can also run alone:

```bash
python benchmark.py boundary
python benchmark.py race
python benchmark.py replica
python benchmark.py smoothness
python benchmark.py throughput
```

Use `python benchmark.py all --help` for every workload control. The defaults are the settings used for the checked-in CSVs.

## What each experiment does

- `boundary` drives the same 100-request quota through a seam-aware burst and a uniform stream. It records the largest admitted count in any rolling two-second window for a fixed-window counter and a two-bucket sliding counter. No timing is widened here; the seam is part of fixed-window's shape.
- `race` forces 30 expired-window rollovers. At each rollover eight clients collide on a counter/reset pair, then the harness fills the rest of that window and counts how much quota the racing resets erased. The naive implementation is swept across a disclosed 0/5/10/25 ms gap between reading the reset and writing the new state. The Lua implementation is the atomic control.
- `replica` starts each cycle with an expired, full window, temporarily pauses replication by detaching the replica for 0/10/25/50 ms, then counts responses that reject while the primary's atomic result reports at least 80 of 100 requests remaining. Detaching is deliberate timing amplification for localhost and is plotted as a sweep. The replica is reattached and verified after every cycle.
- `smoothness` sends eight requests per 100 ms against a 50-per-second limit for 30 virtual seconds. It records admitted requests per 100 ms for fixed and sliding counters. The same 2,400-request stream then runs through the sliding counter and an exact sorted-set log; their per-request decision disagreement rate is the approximation metric.
- `throughput` uses 16 spawned Python processes and pipelines of 256 `EVALSHA` calls so one interpreter thread is not the load-generator ceiling. For the two-shard cases, the replica is temporarily promoted to a second primary. Spread keys are assigned by `crc32(key) % shards`; the hot-key case keeps every process on shard zero. The service is restored as a replica afterward.

`limiters.py` contains the implementations under test: aligned fixed window, deliberately non-atomic two-key fixed window, atomic fixed-window Lua, sliding counter Lua, and exact sliding log Lua.

## Results

The harness writes these files under `results/`:

- `boundary.csv`
- `race.csv`
- `race_gap_sweep.csv`
- `replica.csv`
- `smoothness_timeseries.csv`
- `sliding_accuracy.csv`
- `throughput.csv`
- `run_metadata.csv`

The checked-in files came from one complete `python benchmark.py all` run. Host timing moves between runs, especially throughput; `run_metadata.csv` records the machine and every important knob used by the article.
