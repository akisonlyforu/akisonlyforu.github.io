# Latency Numbers Every Programmer Should Know — remeasured

A native microbenchmark harness that reproduces Jeff Dean's famous
"Latency Numbers Every Programmer Should Know" table on **this** machine
(Apple M4, arm64, 24 GB, macOS, Apple clang 17).

Every number in `results/` is measured on the host. Nothing is fabricated.
Where a measurement had to fall back (e.g. the OS refused to bypass its cache),
the fallback is recorded honestly in the CSV `note` column, in `summary.txt`,
and here.

## Why native, not Docker

Latency microbenchmarks measure the **host** memory hierarchy (L1/L2/SLC/DRAM)
and the **host** SSD. Docker on macOS does not run containers on the host — it
runs them inside a lightweight Linux VM. That VM adds its own page cache, a
virtualized block layer, and a virtual-memory abstraction that completely
destroys cache/DRAM/SSD latency fidelity. A "DRAM latency" measured inside that
VM is meaningless.

So this harness runs **natively**: `clang` compiles the C microbenchmarks and
the system `python3` runs the compress experiment, both directly on macOS.
This is an intentional, documented deviation from the usual "everything in a
pinned container" approach. For this specific class of benchmark, native is the
only honest way to measure.

**There is deliberately no `docker-compose.yml` for this harness.** Adding one
would invite people to run it in the VM and get wrong numbers.

## What it measures

| Experiment | File | What |
|---|---|---|
| A | `results/mem_latency.csv` | Memory latency ladder — random-permutation pointer chase across 4 KB → 256 MB working sets. Reveals L1 / L2 / SLC / DRAM plateaus. |
| B | `results/seq_vs_random.csv` | Sequential vs random cache-line access over a 256 MB buffer. The gap is the hardware prefetcher's speedup. |
| C | `results/canonical_table.csv` | The canonical table, remeasured, next to Jeff Dean's 2012 reference values. |
| — | `results/compress.csv` | "Compress 1 KB" row, via `compress_bench.py` (Python zlib). |
| — | `results/run_metadata.csv` | Host, toolchain, and exact iteration/buffer sizes per experiment. |
| — | `results/summary.txt` | Human-readable digest of everything. |

### Exp A — memory latency ladder (pointer chase)

Classic lmbench `lat_mem_rd`. For each working-set size we build the array as a
**single random-permutation cycle** of pointers and chase it with a fully
dependent load chain (each load's address is the previous load's value). The
random permutation is critical: a sequential stride would be caught by the
hardware prefetcher and would **not** show true DRAM latency. Each size runs
60M–300M dependent accesses, median of 5 trials, after a warm-up pass. The final
pointer is consumed into a `volatile` sink so the compiler can't delete the chain.

### Exp B — sequential vs random

Over a 256 MB buffer (larger than any cache) we touch one byte per 64-byte cache
line, first in sequential order, then in a random permutation of line indices.
Same number of accesses both ways; the difference is what the prefetcher buys you.

### Exp C — the canonical table

Each row is measured on this host (median of many trials):

- **L1 cache reference** — the 4 KB point from Exp A.
- **Branch mispredict** — sorted-vs-unsorted branch benchmark; the per-branch
  time delta (scaled for the ~50% mispredict rate in the unsorted case)
  isolates the misprediction penalty.
- **Mutex lock/unlock** — uncontended `pthread_mutex` lock+unlock loop.
- **Main memory reference** — the 256 MB DRAM plateau from Exp A.
- **Compress 1 KB (zlib)** — from `compress_bench.py`.
- **SSD random 4 KB read** — 4 KB `pread` at random offsets in a 2 GB file with
  the OS page cache bypassed via `fcntl(fd, F_NOCACHE, 1)` (macOS).
- **Read 1 MB sequentially from memory** — sum a 1 MB `malloc` buffer.
- **Read 1 MB sequentially from SSD** — 1 MB `pread` from a 512 MB file with
  `F_NOCACHE`.
- **Localhost socket round trip** — TCP loopback ping-pong (1 byte each way,
  `TCP_NODELAY`), median over 200k round trips.

## How to run

```bash
cd benchmarks/latency-numbers
./run.sh                       # builds, runs everything, writes results/
RESULTS_DIR=/tmp/lat ./run.sh  # override output dir
```

Requires: `clang`, `make`, `python3` (stdlib only — see `requirements.txt`),
`zlib` (system). Run on the physical Mac, not over a slow SSH FS or in a VM.

`make` alone just builds the binary:
`clang -O2 -o latency latency.c -lpthread -lz`.

## Results (this run)

See `results/summary.txt` and `results/canonical_table.csv` for the numbers from
the latest run on this host. The `canonical_table.csv` puts each measured value
next to Jeff Dean's 2012 reference so you can see how a 2024/2025-era laptop
compares to the 2012 mental model.

### Caveats / honesty notes

- **SSD numbers depend on `F_NOCACHE`.** If macOS or the sandbox refuses to
  bypass the page cache, the harness records the number anyway and marks it
  `FALLBACK-page-cache-F_NOCACHE-failed` in the `note` column — in that case the
  number reflects the page cache (RAM), **not** the SSD, and will be far below
  the real device latency. Check the `note` column before trusting the SSD rows.
- **The socket round trip is same-host loopback.** It never leaves the machine,
  so it is an **underestimate** of a real datacenter RTT. Jeff Dean's
  "round trip within same datacenter = 500 µs" is a network number; our loopback
  measures kernel + scheduler overhead only.
- **Branch mispredict is an approximation.** It's derived from a sorted-vs-
  unsorted timing delta, not a hardware performance counter, so treat it as an
  order-of-magnitude figure.
- **E-core / P-core scheduling adds noise.** On Apple silicon the OS may migrate
  the benchmark between performance and efficiency cores, which can make the
  ladder or the tail latencies lumpy. We report medians to blunt this.

### Laptop numbers, not capacity planning

These are numbers from **one laptop on one afternoon**. They're for building
intuition about the shape of the memory hierarchy — the orders of magnitude and
the ratios between tiers — not for sizing a production system. Do not paste these
into a capacity-planning spreadsheet.

## Files

- `latency.c` — native C microbenchmarks (Exp A, B, and the C-measured Exp C rows).
- `Makefile` — `make` builds `latency`.
- `compress_bench.py` — the "compress 1 KB" experiment (Python zlib).
- `run.sh` — builds, runs everything, assembles all CSVs + summary into `results/`.
- `requirements.txt` — no third-party Python deps (stdlib only).
- `results/` — per-experiment CSVs, `summary.txt`, `run_metadata.csv`,
  and `attempts/` for any failed/alternate runs.
