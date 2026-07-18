# logging hot-path harness

A digest-pinned, stdlib-only benchmark for what Python's `logging` costs when it
sits on a hot path. Three experiments, no third-party deps, run inside a pinned
`python:3.12-slim` container.

Three experiments:

- **1. disabled-debug-cost** — a `DEBUG` line that never emits (logger at `INFO`),
  where the argument is expensive to build (a structured order payload rendered
  via `json.dumps`). Three ways to write the same discarded line: `eager` f-string
  (arg built every call, then thrown away), `guarded` (`if logger.isEnabledFor(...)`),
  and `lazy` (`%`-style deferred formatting). Measures ns/call and the ratios.
- **2. sync-vs-async** — real `INFO` lines to a **durable** file sink (flush +
  `os.fsync` per record). `sync` = the `FileHandler` runs on the calling thread;
  `async` = `QueueHandler` on the caller + `QueueListener` draining to the same
  handler on a background thread. Per-call latency is timed on the calling thread;
  reports p50/p99/p999/max in microseconds and wall time.
- **3. sampling** — log an `INFO` line for every event vs ~1 in 100 (a counter).
  Reports lines written, bytes written, and workload throughput (ops/sec).

These are laptop measurements demonstrating the mechanism, not capacity planning.
Absolute numbers depend on the machine, the storage, and the Docker VM; the
**ratios** are the point.

## Why experiment 2 uses an fsync sink

A plain buffered `FileHandler` is absorbed by the OS page cache and does not
actually block the caller, so on fast storage moving it to a background thread
made tail latency *worse*, not better (queue hop + a contending thread). That
first run is preserved under `results/attempts/` with a note. To isolate the
variable the experiment is about — the same I/O cost paid on the caller thread
vs a background thread — the main run makes the sink genuinely block by flushing
and `fsync`ing every record (a durable / audit log). Now `sync` pays the fsync on
the hot path and `async` pays it off the hot path.

## Run it

Docker with Compose v2. No Python needed on the host (it runs in the container).

```bash
cd benchmarks/logging-hot-path
docker compose run --rm bench | tee results/summary.txt
```

The container mounts the repo dir and writes CSVs + `summary.txt` to `results/`.
It runs with `network_mode: none` (no server, no ports, loopback-only). Iteration
counts and seed are env vars in `docker-compose.yml`; override per-run, e.g.:

```bash
docker compose run --rm -e EXP2_CALLS=100000 -e EXP1_ITERS=2000000 bench
```

There are no containers left running (`run --rm` removes it on exit).

## Results (captured run, python 3.12.13, arm64)

Image: `python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`

**Experiment 1 — disabled DEBUG line, 1,000,000 iters/variant (median of 5):**

| variant  | ns/call | ops/sec |
|----------|--------:|--------:|
| eager    | 5260.39 | 190,099 |
| guarded  |   58.55 | 17,078,033 |
| lazy     | 1419.64 | 704,404 |

`eager` is **3.7x** slower than `lazy` and **89.8x** slower than `guarded`, for a
line that produces zero output. The eager cost is almost entirely `json.dumps` +
f-string building an argument that `logging` immediately discards.

**Experiment 2 — per-call latency on the caller, durable fsync sink, 50,000 calls/mode:**

| mode  | p50 (us) | p99 (us) | p999 (us) | max (us) | wall (s) |
|-------|---------:|---------:|----------:|---------:|---------:|
| sync  | 276.375  | 645.084  | 5752.167  | 31203.04 | 14.9447  |
| async |   4.167  |  10.417  |   24.625  |  5976.00 |  0.2347  |

`sync` blocks the caller on every fsync (p99 645us, p999 5.75ms). `async` turns
the hot-path call into a queue enqueue — p99 **10.4us**, p999 **24.6us**, ~62x
lower at p99. The fsync cost doesn't vanish; it moves to the background thread
(the caller loop finishes in 0.23s while the listener keeps draining).

**Experiment 3 — log-everything vs sample 1-in-100, 1,000,000 events/mode:**

| mode    | lines     | bytes      | ops/sec    |
|---------|----------:|-----------:|-----------:|
| full    | 1,000,000 | 75,667,678 |    217,325 |
| sampled |    10,000 |    756,666 | 10,561,711 |

Sampling wrote **100x** fewer lines and bytes, and ran **~49x** the throughput,
because most events skip the logging call entirely.

## Files

- `benchmark.py` — the harness (stdlib only; env-configurable).
- `docker-compose.yml` — digest-pinned `python:3.12-slim`, `network_mode: none`.
- `requirements.txt` — none; comment-only, kept for convention.
- `results/summary.txt` — captured console output of the run above.
- `results/exp1_disabled_debug.csv` — variant, iterations, total_ns, ns_per_call, ops_per_sec.
- `results/exp2_sync_vs_async.csv` — mode, calls, p50/p99/p999/max_us, wall_s.
- `results/exp2_latency_samples.csv` — mode, latency_us; downsampled to ~2000/mode
  (every Nth call) so a distribution can be charted without a giant file.
- `results/exp3_sampling.csv` — mode, events, lines_written, bytes_written, ops_per_sec.
- `results/run_metadata.csv` — python version, image digest, seed, iteration counts, headline numbers.
- `results/attempts/` — the buffered (no-fsync) exp2 run that didn't show the
  async win, plus a note explaining why.

## Reproducibility notes

Warm-up runs precede every timed section; timing uses `time.perf_counter_ns()`;
exp1 keeps the **median** of 5 repeats; any randomness is seeded (`SEED=1234`).
Experiments 1 and 3 reproduce cleanly (ratios stable across runs; exp1 absolute
ns/call drifts a few percent with machine noise). Experiment 2's absolute
microseconds depend heavily on how the Docker VM backs `fsync`, so treat its
numbers as a shape (async p99/p999 an order of magnitude below sync), not a
fixed figure.
