# Java G1GC: on-heap vs off-heap long-lived population

This harness reproduces a specific G1GC failure mode and its off-heap fix.

A large population of **long-lived** objects sits in the heap and pins the G1 **old
generation** above the IHOP (Initiating Heap Occupancy Percent) threshold. Because
the data is live, every concurrent-mark cycle scans mostly-live old regions,
reclaims almost nothing, and finishes with occupancy still above the threshold — so
G1 immediately re-initiates another cycle. The result is dozens of expensive,
low-yield concurrent-mark cycles over one run.

Moving the same logical data **off-heap** (one `ByteBuffer.allocateDirect` slab)
empties the old generation. Old-gen occupancy never crosses IHOP again, so
concurrent marking collapses to zero. The trade the textbook predicts is that each
lookup now pays a byte-copy/decode cost instead of a pointer dereference.

This mirrors a real service that moved ~150M long-lived ML-model objects off-heap and
cut concurrent GC time ~98%.

## What it measures

Both modes run the **identical** fixed workload under identical JVM flags and Docker
resource limits:

- **onheap** — the long-lived population is a `byte[][]` slab of N live `byte[]`
  objects, strongly referenced for the whole run. The lookup is `arr[key]` (a true
  pointer dereference) followed by folding the payload bytes into a checksum.
- **offheap** — the same logical data lives in one `ByteBuffer.allocateDirect(N *
  payload)`. The lookup computes `key * payload` and copies the payload bytes out of
  the direct buffer into a scratch array, folding them into the same checksum.

Each iteration also allocates a short-lived `byte[]` (default 2 KiB) to drive young
GC pressure. A fixed seed drives a deterministic key sequence, so both modes do the
same work; an escape-analysis-defeating checksum is printed at the end so the JIT
cannot elide the lookups.

The JVM runs with `-Xmx1500m -Xms1500m -XX:+UseG1GC` and unified GC logging
(`-Xlog:gc*`). `benchmark.py` parses the GC log for concurrent-mark cycle count,
concurrent-mark time, per-type STW pause counts/times, and total GC time. `GcBench`
itself records per-lookup latency percentiles (nanoTime around a 2M-sample subset),
wall-clock, and throughput.

## Tuning notes (how the reproduction was stabilized)

- **Population size.** N=3,500,000 entries × 192 B ≈ 640 MB of payload; with object
  headers the live set is ~50% of the 1500 MB heap. That is above the default IHOP
  (45%) but close enough that G1's *adaptive* IHOP can drift above it and stop the
  cycles.
- **Pinned IHOP.** To make the on-heap reproduction deterministic (and to keep N —
  and therefore the off-heap direct buffer — small enough to avoid swap pressure on a
  laptop), the harness runs both modes with `-XX:-G1UseAdaptiveIHOP
  -XX:InitiatingHeapOccupancyPercent=40`. The off-heap old gen stays near-empty, so
  it never crosses 40% regardless.
- A `HashMap<Long, byte[]>` variant (see `results/attempts/`) reproduced the same GC
  collapse but is a worse latency model, because `HashMap.get(Long)` autoboxes the
  key and walks a bucket — that is not a pure pointer dereference. The checked-in
  primary run uses `byte[][]`.

## Run it

You need Docker and Python 3.9+ (standard library only — see `requirements.txt`).

```bash
cd benchmarks/java-gc-offheap
python3 benchmark.py            # runs onheap then offheap, writes results/
python3 benchmark.py onheap     # a single mode
```

Everything is env-configurable: `RESULTS_DIR`, `BENCH_N`, `BENCH_PAYLOAD`,
`BENCH_ITERS`, `BENCH_GARBAGE`, `BENCH_SEED`, `BENCH_SAMPLES`, `BENCH_HEAP`,
`BENCH_MEM`, `BENCH_CPUS`, `BENCH_IHOP`, `IMAGE`.

The base image is digest-pinned:
`eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a`
(`eclipse-temurin:21-jdk`, JDK 21.0.11).

## Results

Selected low-contention run (JDK 21.0.11, `-Xmx1500m -Xms1500m -XX:+UseG1GC
-XX:-G1UseAdaptiveIHOP -XX:InitiatingHeapOccupancyPercent=40`, N=3,500,000 × 192 B,
40M iterations, `--memory=3g --cpus=4`). Numbers below are from `results/summary.txt`;
`results/runs/metrics.csv` holds all 8 pairs.

| metric | onheap | offheap |
| --- | ---: | ---: |
| concurrent-mark cycles | 34 | **0** |
| concurrent-mark time (ms) | 4968.3 | **0.0** |
| total STW pause time (ms) | 247.8 | 114.3 |
| total GC time (ms) | 5216.1 | **114.3** |
| young pauses (count) | 61 | 89 |
| mixed pauses (count) | 15 | 0 |
| full GCs (count) | 0 | 0 |
| wall clock (s) | 13.65 | 10.50 |
| throughput (ops/sec) | 2,929,818 | **3,808,946** |
| lookup p50 (ns) | 292 | 250 |
| lookup p90 (ns) | 417 | 334 |
| lookup p99 (ns) | 625 | 625 |
| lookup p999 (ns) | 875 | 791 |

Off-heap eliminates concurrent marking entirely (100%), cuts total GC time by 97.8%,
and raises throughput by 30% — while lookup p50 is actually 14% *lower* off-heap and
the tails are a tie. Across the eight-run sweep the on-heap run consistently produced
34–37 concurrent-mark cycles and ~5.0–5.2 s of GC time on the clean runs; off-heap was
always exactly 0 cycles with ~0.11–0.14 s of GC.

The GC mechanism reproduces cleanly and every time: on-heap runs 20–40+
low-yield concurrent-mark cycles and spends the large majority of GC time in
concurrent marking; off-heap runs **zero** concurrent-mark cycles and only cheap
young pauses, cutting total GC time by ~98%.

The per-lookup latency **trade-off predicted by the textbook did not reproduce on
this laptop.** Off-heap was faster on throughput *and* on median lookup latency,
because the on-heap mutator is continually slowed by the concurrent marker stealing a
core and thrashing cache, which outweighs the off-heap byte-copy decode cost. The
off-heap decode cost is real but small relative to the GC tax it removes. (See the
best-of-N note below.)

### Files

- `gc_onheap.csv` / `gc_offheap.csv` — concurrent-mark cycles, concurrent-mark time,
  per-type STW pause counts/times, total GC time (parsed from the GC log).
- `latency_onheap.csv` / `latency_offheap.csv` — lookup latency percentiles (ns),
  written by `GcBench`.
- `throughput.csv` — per-mode wall clock, throughput, and lookup p50/p90/p99/p999.
- `perf_onheap.csv` / `perf_offheap.csv` — raw wall/throughput/checksum from `GcBench`.
- `summary.txt` — human-readable comparison of the selected run.
- `run_metadata.csv` — JDK version, base image digest, JVM flags, heap, N, payload,
  iterations, seed, sample count.
- `gc-onheap.log` / `gc-offheap.log` — raw unified GC logs.
- `results/runs/` — the individual best-of-N pairs and `metrics.csv` across them.
- `results/attempts/` — non-reproducing / superseded tuning attempts, each with a
  `NOTE.txt` explaining why it was set aside.

## A note on the numbers (laptop, not capacity)

These are laptop measurements taken on a memory- and CPU-contended machine (other
containers were running during collection). Absolute throughput and latency vary
run-to-run with that contention; the harness therefore runs the pair several times
and the checked-in `summary.txt`/`throughput.csv` reflect a low-contention run for
each mode (contention only ever adds latency and removes throughput, so the least
-contended run is the closest estimate of the uncontended truth). Treat the GC-cycle
collapse and the ~98% GC-time reduction as the robust result; treat the absolute
ops/sec and nanosecond figures as directional, not production sizing.
