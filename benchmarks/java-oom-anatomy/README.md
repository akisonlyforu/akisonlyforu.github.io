# java-oom-anatomy: four ways a JVM runs out of memory, one error name


A reproduction lab for a blog post about `java.lang.OutOfMemoryError`. The point
isn't "the JVM ran out of memory" — it's that the *same* error class is thrown for
several *different* failures, that a leak is diagnosed by **what GC reclaims** (the
post-collection live set) rather than by peak usage, and that not every OOM is even
a heap problem. Every number here is captured from a real JVM dying in a
digest-pinned container — no synthetic curves, no hand-waving about what a GC log
"would" show.

Five scenarios, each a real OOM (or, for the contrast, a real *survival*):

- **A — leak → `Java heap space`** (`Main.leak`): allocate 32 KB blocks and *retain*
  every one in a `static List` under `-Xmx256m`. The post-GC live set climbs toward
  the ceiling and Full-GC pauses widen as the collector works harder for less, until
  it gives up. This is the anchor.
- **B — healthy → no OOM** (`Main.healthy`): the *same* allocation rate and the same
  `-Xmx256m`, but each block is released instead of retained. It runs to completion;
  post-GC heap stays flat. Same allocation as A — retention is the only difference.
- **C — GC overhead → `GC overhead limit exceeded`** (`Main.gcOverhead`): a near-full
  heap under Parallel GC (`-Xmx80m`, fill to 80%), where the collector spends nearly
  all wall-clock time collecting and reclaims almost nothing. This one is timing-
  sensitive; see "Lumpiness" below.
- **D — metaspace → `Metaspace`** (`Main.metaspace`): load thousands of distinct
  runtime classes — a fresh classloader per iteration `defineClass`-ing the same
  `Leak` bytecode, each producing a distinct klass — under `-XX:MaxMetaspaceSize=64m`.
  Heap stays flat while metaspace fills to the wall. An OOM that heap tuning can't fix.
- **E — direct buffer → `Cannot reserve ... direct buffer memory`** (`Main.directBuffer`):
  retain 512 KB `ByteBuffer.allocateDirect` wrappers under `-XX:MaxDirectMemorySize=64m`.
  The bytes live in native memory freed only when the wrapper's `Cleaner` runs, so
  retaining the wrappers pins memory that never appears in a heap dump. Note JDK 21
  no longer emits the short `Direct buffer memory` string; it reports the arithmetic.

## Run it

Requirements: Docker, Python 3 (standard library only — see `requirements.txt`).
Everything runs in a container, so no host JDK is needed.

```bash
cd benchmarks/java-oom-anatomy
python3 benchmark.py
```

The driver builds the image from the pinned base, runs each scenario as its own
container, parses the unified GC logs / `jcmd` samples, and writes CSVs + raw logs +
a `summary.txt` into `results/`. Env knobs (all optional):

| var | default | what |
|-----|---------|------|
| `RESULTS_DIR` | `./results` | where CSVs / logs / summary land |
| `XMX_HEAP` | `256m` | heap cap for the leak + healthy runs |
| `XMX_GCO` | `80m` | heap cap for the GC-overhead run |
| `MAX_META` | `64m` | metaspace cap for the metaspace run |
| `MAX_DIRECT` | `64m` | direct-buffer cap for the direct-buffer run |

## Results (this machine)

`openjdk 21.0.11 Temurin-21.0.11+10-LTS`, base image
`eclipse-temurin:21-jdk@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a`,
macOS arm64.

| scenario | OOM message | key numbers |
|----------|-------------|-------------|
| A leak | `java.lang.OutOfMemoryError: Java heap space` | post-GC heap 11 MB → 254 MB (cap 256 MB); Full-GC pause 4.5 ms → 12.7 ms; `[B` = 97% of top-15 bytes; ~33 s |
| B healthy | *(none — exit 0)* | 250 MB churned; post-GC heap flat at 10 MB; no OOM |
| C GC overhead | `java.lang.OutOfMemoryError: GC overhead limit exceeded` | final window 94.0% time-in-GC, 1.5% reclaimed; ~1 s |
| D metaspace | `java.lang.OutOfMemoryError: Metaspace` | metadata 1 MB → 26 MB to the 64m wall over ~10,600 classes; heap flat ≤24 MB; ~22 s |
| E direct buffer | `java.lang.OutOfMemoryError: Cannot reserve 524288 bytes of direct buffer memory (allocated: 66584626, limit: 67108864)` | direct memory 0 → 63 MB into the 64m cap over 128 retained buffers; heap flat at 11-12 MB of 256 MB; ~3 s |

Exact GC logs, `jcmd` samples, and the verbatim OOM stack traces are under
`results/logs/`.

## Lumpiness

Scenario C is the one that isn't deterministic. `GC overhead limit exceeded` is
thrown only when the Parallel collector's own heuristic (>98% of recent time in GC,
<2% reclaimed) trips *before* the heap is flatly exhausted. At a higher fill fraction
the heap simply runs out first and you get plain `Java heap space` instead — same
wall, different label. Filling to 80% reproduces the overhead-limit message reliably
here; the driver retries a few times and files any near-miss under `results/attempts/`.
That the label depends on timing is itself part of the story.

## These are laptop numbers

Every figure is the mechanism reproduced on one machine with tiny heaps chosen to
fail fast, not a capacity or tuning recommendation. A 256 MB heap dies in half a
minute here so the death spiral fits in a GC log you can read; production heaps are
orders of magnitude larger and the *shape* is what carries over, not the seconds.
