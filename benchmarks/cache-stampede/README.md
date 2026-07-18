# cache-stampede harness

This is the harness behind [Our Fix for the Thundering Herd Was a Lock](../../collections/_posts/2025-03-20-thundering-herd-lock.md).

64 concurrent workers hammer one hot key backed by a ~300ms recompute, against a
digest-pinned Redis 7.4. It measures four strategies for the thundering-herd
problem and writes their latency distributions:

- **herd** — plain TTL, everyone recomputes on the synchronized miss.
- **lock** — `SET NX` recompute lock: one holder computes, the rest wait.
- **lock_crash** — the same lock, but the holder is killed mid-recompute one time in five.
- **probabilistic** — XFetch-style early recomputation: a reader refreshes ahead of expiry, in the background, so no caller blocks.

Plus a jitter mini-experiment: 300 keys given a synchronized vs a ±50% jittered
TTL, counting how many expire in the same 250ms window.

These are laptop measurements demonstrating the mechanism, not production numbers.

## Run it

Docker with Compose v2, plus Python 3.9+ with `redis`.

```bash
cd benchmarks/cache-stampede
docker compose up -d --wait          # Redis on 127.0.0.1:6395

python3 -m venv /tmp/herd-venv && source /tmp/herd-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

## Results

- `summary.txt` — the captured console run used in the post.
- `latency_percentiles.csv` — p50/p95/p99/max per strategy.
- `p99_timeline.csv` — per-second p99 for each strategy (the sawtooth-vs-flat chart).
- `jitter.csv` — peak keys expiring in one window, synchronized vs jittered.
- `run_metadata.csv` — Redis version and workload parameters.

The checked-in run is Redis 7.4.9, 64 workers, 300ms recompute, 2s TTL. Because
the load is concurrent and the holder-crash timing is random, exact numbers move
run to run; the shapes (lock ≈ herd on p99, the crash spike, probabilistic flat)
are stable.
