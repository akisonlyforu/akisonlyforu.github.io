# JVM GC / heap-tuning harness

A small Java "service" allocation workload run under different garbage collectors
and heap sizes inside a digest-pinned `eclipse-temurin:21-jdk` container, measuring
what each choice does to **request-latency tails** and **throughput**.

The workload (`Bench.java`) keeps a bounded in-memory cache (the live working set
that sits in the old generation) and runs a request loop. Each request allocates a
few KB of short-lived garbage, touches a cache entry, and every Nth request replaces
a cache entry with a fresh payload — that replacement is what promotes objects into
the old generation and eventually triggers old/mixed/full collections.

Every request is timed with `System.nanoTime()`. A stop-the-world GC pause lands on
top of whatever request is running, so it shows up as a latency spike in the tail.
Percentiles come from a fixed microsecond-bucket histogram (no per-request
allocation, so the measurement doesn't perturb the heap). Separately, the driver
parses the JVM's own `-Xlog:gc` output to get real STW pause counts / totals / max,
and cross-checks them against the in-app tail.

## Two experiments

- **1. Collector comparison** (`collector_comparison.csv`) — the same workload at a
  fixed `-Xmx` under `-XX:+UseParallelGC`, `-XX:+UseG1GC`, and generational ZGC
  (`-XX:+UseZGC -XX:+ZGenerational`). The live set is deliberately large so a full/old
  collection has a lot to compact. Story: ParallelGC (a throughput collector) takes
  big stop-the-world pauses that wreck the tail; G1 is bounded but still visible;
  ZGC's pauses are sub-millisecond, so the tail stays flat — at some throughput cost.

- **2. Heap sizing thrash** (`heap_sizing.csv`) — collector fixed at G1, a smaller
  live set, and `-Xmx` swept from comfortable down to barely-fits. Story: undersize
  the heap and GC frequency and *% time in GC* explode while throughput collapses.

The two experiments use different workload scales on purpose (large live set for the
collector story, small live set for the heap-sizing story) — a 2.5 GB live set would
simply OOM at 256m. Within each experiment the workload is identical across every
config (same seed, same live set, same op count); only the JVM flag under test
changes.

## Run it

Docker with Compose v2, plus Python 3.9+ (standard library only — nothing to `pip
install`).

```bash
cd benchmarks/java-gc-tuning
docker compose pull                 # fetch the digest-pinned JDK image

python benchmark.py                 # runs both experiments, writes results/
```

`benchmark.py` drives everything with `docker run` (compile `Bench.java` with
`javac`, then run `java` with the GC flags under test). There is no long-lived
service and no network — the workload is purely in-process CPU + heap, so
`docker-compose.yml` only exists to pin/fetch the image.

Every knob is an environment variable (see the top of `benchmark.py`): `RUNS`
(repeats per config, median reported), `COLLECTOR_HEAP`, `HEAP_SIZES`, and per-
experiment `COL_*` / `HEAP_*` workload sizes. Example — a faster smoke run:

```bash
RUNS=1 COL_OPS=500000 HEAP_OPS=1000000 python benchmark.py
```

## Results

- `summary.txt` — human-readable headline table for both experiments.
- `collector_comparison.csv` — per collector: GC pause count / total / max / p99,
  request-latency p50/p99/p99.9/max (µs), throughput (ops/s), % time in GC.
- `heap_sizing.csv` — per `-Xmx`: % time in GC, GC count, total pause, p99/p99.9
  request latency, throughput.
- `run_metadata.csv` — `java -version`, image + sha256 digest, arch, run count, and
  both workload profiles (op counts, live-set sizes, heaps).
- `attempts/` — parked runs that didn't reproduce a clean story, with notes.

## Honesty notes

These are laptop measurements demonstrating the mechanism, not capacity planning.
GC pause magnitudes depend heavily on core count and memory bandwidth: this host has
10 cores, so ParallelGC's parallel mark-compact is fast, and the dramatic
hundred-millisecond full-GC pauses only appear once the live set is a couple of GB
(a smaller heap gives tens-of-ms pauses, still an order of magnitude worse than
ZGC). Runs are repeated `RUNS` times and the median is reported; the collector
comparison is stable run-to-run, the small-heap thrash point is the noisiest number
because it lives right at the edge of fitting. The *shape* — big STW pauses on the
throughput collectors versus flat sub-millisecond tails on ZGC, and throughput
collapse when the heap is undersized — is not hardware-specific.
