# Java memory leak: per-request thread pool vs shared singleton

This harness reproduces a classic production JVM memory leak and its one-line fix.

A resource-holding object -- a `JobExecutor` that owns an HTTP-client-like buffer
plus a `ThreadPoolExecutor` with a small, **prestarted** core pool -- was changed by
a refactor from a startup singleton to a per-request `new JobExecutor()` that is
never `shutdown()`. Because the pool's core worker threads are alive, they are GC
roots, so each per-request executor's whole object graph stays reachable. Heap-after
-GC climbs monotonically until the JVM dies with `OutOfMemoryError: Java heap space`.

The fix is one shared `JobExecutor` built at startup and reused for every request.

## Why bare `new JobExecutor()` leaks even with no reference kept

The leak does not need any application-level reference to the executor. A live
thread retains it through the pool's own plumbing:

```
live worker Thread
  -> Runnable target = ThreadPoolExecutor$Worker   (non-static inner class of TPE)
    -> enclosing ThreadPoolExecutor                (Worker.this$0)
      -> TPE.threadFactory = NamedFactory          (non-static inner class of JobExecutor)
        -> enclosing JobExecutor                   (NamedFactory.this$0)
          -> JobExecutor.buffer (byte[])           <-- the leaked payload
```

The pool is built with a **non-static inner** `ThreadFactory` (`NamedFactory`),
exactly as a real client that names its threads after itself would be. Every thread
that factory produces keeps an implicit back-reference to its `JobExecutor`. With
`prestartAllCoreThreads()` and `allowCoreThreadTimeOut(false)`, those core threads
are alive from construction and never exit -- so the whole chain is a permanent GC
root. `fixed` mode builds the chain exactly once; `leaky` mode builds a new one per
request and drops it on the floor.

The harness verifies the retention empirically rather than asserting it: in `leaky`
the heap-after-GC and the live-thread count both climb in lockstep with the
instance counter (heap +~1 MB and threads +2 per instance), and the process dies of
`Java heap space`. In `fixed` the identical workload holds at 1 instance / 2 threads
/ flat heap.

## What it measures

Both modes run the identical request loop under identical JVM flags and Docker
limits. `LeakBench` emits a CSV row every N requests with:

`requests_served, heap_used_after_gc_mb, live_thread_count, jobexecutor_instances, gc_count, elapsed_s`

- **heap_used_after_gc_mb** -- `MemoryMXBean` heap-used, sampled immediately after a
  `System.gc()`-triggered full GC at each report interval. That is the *retained
  live set* after a collection: the honest "is this leaking" number, and it works
  even in `fixed` mode, where the workload allocates so little that a natural GC may
  never fire on its own. (The raw `-Xlog:gc` logs are kept too, in `gc_leaky.log` /
  `gc_fixed.log`, and show the same story from the JVM's side.)
- **live_thread_count** -- `Thread.getAllStackTraces()` filtered by the pool thread
  name prefix (`jobexec-pool-`).
- **jobexecutor_instances** -- a static `AtomicInteger` incremented in the
  constructor and **never decremented** (they leak), mirroring the "how many piled
  up" number a heap dump of the real incident showed.

`leaky` runs until OOM; `fixed` runs at least 3x the leaky death-request-count.

## Run it

Docker and Python 3.9+ (standard library only -- see `requirements.txt`).

```bash
cd benchmarks/java-memory-leak
python3 benchmark.py            # runs leaky (to OOM) then fixed (3x that many requests)
python3 benchmark.py leaky      # a single mode
```

Env-configurable: `RESULTS_DIR`, `BUFFER_KB`, `POOL_CORE`, `REPORT_EVERY`,
`REQ_SLEEP_MS`, `LEAKY_MAX_REQUESTS`, `FIXED_MIN_REQUESTS`, `FIXED_MULTIPLE`,
`BENCH_HEAP`, `BENCH_XSS`, `BENCH_MEM`, `BENCH_CPUS`, `BENCH_GC`, `IMAGE`.

The base image is digest-pinned:
`eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a`
(`eclipse-temurin:21-jdk`, JDK 21.0.11).

## Tuning notes (how the reproduction was chosen)

- **Buffer size = 512 KB.** This stands in for an HTTP client plus its
  connection-pool and TLS buffers/state -- real clients retain far more than a
  single 64 KB socket buffer. At 512 KB the heap fills at a tidy ~190 leaked
  instances / ~378 leaked threads, which keeps the Java heap unambiguously the
  binding constraint and the timeline readable. The literal-64 KB variant
  reproduces the *same* `Java heap space` OOM, just at ~2700 instances / ~5400
  threads; it is kept under `results/attempts/buffer-64kb-unpaced/` with a NOTE.
- **`-Xss256k`.** Small thread stacks keep native memory modest so the Java heap,
  not native thread memory, is what runs out. We never observed a native-thread OOM
  ("unable to create new native thread") in any run -- stacks commit lazily, so the
  heap always filled first -- and the harness does not claim one.
- **300 ms/request pacing.** Unpaced, the leaky run OOMs in ~0.1 s, which is
  faithful but gives a coarse time series. Pacing spreads the death across ~57 s and
  ~38 evenly spaced CSV samples.

## Results

Selected run (JDK 21.0.11, `-Xmx192m -Xms192m -Xss256k -XX:+UseG1GC`, 512 KB buffer,
poolCore=2 prestarted, 300 ms/request, `--memory=1g --cpus=2`). From
`results/summary.txt`:

| metric | LEAKY | FIXED |
| --- | ---: | ---: |
| outcome | **OOM: Java heap space** | survived, never died |
| requests at death / served | 189 | 600 |
| heap after GC (end) | **190.9 MB** (of 192) | **2.7 MB** (flat) |
| live pool threads | **378** | **2** |
| JobExecutor instances | **190** | **1** |
| time | 57.2 s to death | 181 s, still alive |
| heap climb | 6.6 MB @ 5 req -> 190.9 MB @ 189 req | 2.57 / 2.66 MB min/max |

LEAKY's heap-after-GC climbs monotonically (0 non-monotonic steps across 38 samples),
~1.0 MB per leaked instance, until full GCs start reclaiming nothing
(`Pause Full 191M->191M(192M)` in `gc_leaky.log`) and the JVM dies. FIXED runs the
same workload 3.2x longer at a constant 1 instance / 2 threads and a flat ~2.7 MB
heap -- it would run forever.

### Files

- `leaky.csv` / `fixed.csv` -- the per-interval time series.
- `gc_leaky.log` / `gc_fixed.log` -- raw `-Xlog:gc` output.
- `summary.txt` -- headline numbers for both modes.
- `run_metadata.csv` -- JDK version, base image digest, heap, GC, pool size, buffer,
  request pacing, and the measured death/served counts.
- `stdout_leaky.txt` / `stdout_fixed.txt` -- full program console output.
- `results/attempts/buffer-64kb-unpaced/` -- the 64 KB variant (same OOM, higher
  thread count) with a `NOTE.txt`.

## A note on the numbers (laptop, not capacity)

These are laptop measurements demonstrating a mechanism, not capacity planning. The
absolute request-to-death count is a direct function of the artificially small
`-Xmx192m` heap and the 512 KB buffer -- a production service with a multi-GB heap
leaks for far longer before it dies, which is exactly why this bug reaches
production. Treat the *shape* as the robust result: heap-after-GC climbs
monotonically in lockstep with a per-request instance count that never comes back
down, versus a dead-flat heap and a constant single instance once the executor is a
reused singleton.
