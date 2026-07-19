# Java high-CPU debugging lab: 3 bugs, real flame graphs

A reproduction lab for a blog post about diagnosing high CPU in Java with flame
graphs. Three classic "CPU pegged, throughput collapsed" bugs, each deliberately
built, each run for real, each profiled with [async-profiler](https://github.com/async-profiler/async-profiler)
— no synthetic numbers, no hand-waving about what a flame graph "would" look like.

- **Bug 1 — unanchored regex** (`RegexBug`): a hot loop that wants "does this log
  line contain ERROR" and gets it via `line.matches(".*ERROR.*")`. `String.matches()`
  already requires a full-string match, so the leading/trailing `.*` are redundant —
  but Java's regex engine is a backtracking NFA, not a DFA, and for every line that
  does *not* contain "ERROR" (the common case) it has to try anchoring the literal at
  every offset before it can give up. `regex-fixed` does the identical semantic check
  with `String.contains("ERROR")`, an O(n) single pass.
- **Bug 2 — busy-spin queue poll** (`SpinBug`): Kafka-consumer-idle-poll style — one
  consumer thread per queue, queues almost always empty. `spin-bad` calls the
  non-blocking `queue.poll()` in a hot `while(true)` with no sleep/backoff.
  `spin-fixed` calls the blocking `queue.poll(50, TimeUnit.MILLISECONDS)` instead.
- **Bug 3 — Hibernate flush/dirty-checking trap** (`HibernateBug`): the Krzysztof
  Ślusarski `existsById()` story. A single Session accumulates 8,000 managed entities
  (a big persistence context — think a long batch job or a chunky request handler),
  then the workload repeatedly runs a one-row indexed lookup query.
  `hibernate-bad` leaves Hibernate's default `FlushMode.AUTO` in place, so *every*
  query first triggers a full dirty-check scan of the whole persistence context —
  O(N) work before every one-row SELECT. `hibernate-fixed` sets
  `session.setHibernateFlushMode(FlushMode.COMMIT)` once, which skips that pre-query
  auto-flush entirely; the query goes back to costing what it looks like it costs.

Everything runs directly on the host JDK, not in Docker — async-profiler needs
host-level JVMTI/signal access that doesn't play nicely through Docker Desktop's
Linux VM on a Mac, and the whole point of this lab is that you can run it exactly as
written on your own laptop.

## Run it

Requirements: JDK 21+, Maven 3, Python 3 (standard library only — see
`requirements.txt`), and `asprof` on your `PATH`.

**macOS (arm64 or x86_64):**
```bash
brew install async-profiler
```
This installs the `asprof` CLI, which attaches to a running JVM by pid — no agent,
no restart, no special entitlements needed for the CPU profiler used here.

**Linux (x86_64, aarch64):** Homebrew's formula is macOS-only. Grab the matching
release tarball from the [async-profiler releases page](https://github.com/async-profiler/async-profiler/releases)
for your arch (e.g. `async-profiler-4.4-linux-x64.tar.gz`), untar it somewhere, and
put its `bin/` on your `PATH` (or reference `bin/asprof` with a full path). On some
distros/kernels you may also need `sysctl kernel.perf_event_paranoid=1` and
`sysctl kernel.kptr_restrict=0` for the CPU profiler to see all frames — see the
async-profiler README's "Basic Usage" / troubleshooting section if `asprof` reports
permission errors.

Then:

```bash
cd benchmarks/java-high-cpu-debugging
mvn -q package
python3 benchmark.py            # builds (again, harmless), runs all 6 modes, writes results/
```

`benchmark.py` builds the jar, then for each of the 6 modes: launches
`java -cp target/java-high-cpu-debugging.jar lab.Main <mode>` as a subprocess, waits
2s for JIT/JVM warmup, grabs its pid, and runs
`asprof -d 30 -e cpu -f results/flame-<mode>.html <pid>` concurrently with the
workload (a self-contained interactive HTML flame graph, no external JS/CSS
dependencies — open it straight in a browser). Each workload runs for a fixed 35s and
samples its own process CPU load once a second; the driver waits for the process to
finish, then moves to the next mode.

You can also run any single mode by hand, exactly as the driver does:
```bash
java -cp target/java-high-cpu-debugging.jar lab.Main regex-bad results
# in another terminal, once you have its pid:
asprof -d 30 -e cpu -f results/flame-regex-bad.html <pid>
```

Everything is env-configurable: `RESULTS_DIR` (default `./results`),
`WORKLOAD_DURATION_SEC` (default 35), `PROFILE_DURATION_SEC` (default 30),
`WARMUP_SEC` (default 2), `HIBERNATE_N` (default 8000, the persistence-context size
for bug 3). A fast smoke run:

```bash
WORKLOAD_DURATION_SEC=8 PROFILE_DURATION_SEC=5 HIBERNATE_N=2000 python3 benchmark.py
```

## Results (this run)

JDK 25.0.1 (Homebrew OpenJDK, aarch64), async-profiler 4.4, Maven 3.9.11, macOS
15.7.3 arm64, 10 cores. Full numbers in `results/summary.txt`; raw data in
`results/*.csv`. `getProcessCpuLoad()` is normalized across *all* cores (1.0 = every
core saturated), so a single busy thread on this 10-core host reads as ~10%, not
~100% — that matters for reading bugs 1 and 3 below.

| bug | variant | avg CPU% | throughput | vs. fixed |
| --- | --- | ---: | ---: | --- |
| 1. unanchored regex | bad | 9.9% | 1,857,476 lines/sec | — |
| | fixed | 10.1% | 14,790,202 lines/sec | **8.0x** |
| 2. busy-spin poll | bad | 91.8% | — | **417x more CPU** |
| | fixed | 0.2% | — | (near-idle) |
| 3. Hibernate AUTO-flush | bad | 10.5% | 1,371 checks/sec | — |
| | fixed | 10.5% | 878,355 checks/sec | **641x** |

Bugs 1 and 3 are both single-threaded tight loops, so process CPU load looks nearly
identical bad-vs-fixed on this 10-core host (~10% either way — one core fully busy,
normalized across ten) — CPU% alone would tell you nothing is wrong. The real signal
is throughput collapsing by 8x and 641x respectively for the *same* CPU budget, and
the flame graph shows exactly where that budget went. Bug 2 is the one built to show
a dramatic CPU% swing on its own: it uses one consumer thread per core (10 here), so
the aggregate process CPU load itself goes from ~92% to ~0.2%.

### What dominates each "bad" flame graph

- **regex-bad**: `java.util.regex.Pattern$CharPropertyGreedy.match` (~46%) and
  `java.util.regex.Pattern$Slice.match` (~42%) — the greedy `.*` backtracking and the
  literal-slice matcher together account for ~88% of samples. `regex-fixed` doesn't
  touch the regex engine at all.
- **spin-bad**: `java.util.concurrent.locks.AbstractQueuedSynchronizer.signalNext`
  (~77%), plus `AbstractQueuedSynchronizer.getState`/`setState`/
  `compareAndSetState` and `ReentrantLock$Sync.lock`/`tryRelease`. The actual loop
  body (`SpinBug.lambda$run$0`) is only ~15% — most of the burned CPU isn't "doing
  nothing", it's hammering `ArrayBlockingQueue`'s internal lock billions of times a
  second, which is arguably a more useful lesson than "spinning is just idle".
- **hibernate-bad**: `java.lang.reflect.Field.get` (~16%), `java.lang.Long.equals`
  (~9.5%), `AbstractEntityPersister.getPropertyValues` (~6%),
  `AbstractFlushingEventListener.prepareEntityFlushes` (~5%),
  `Cascade.cascade` (~4%), `DefaultFlushEntityEventListener.performDirtyCheck`
  (~4%), `DirtyHelper.findDirty` (~3%) — textbook flush/dirty-checking machinery,
  confirming the bug is exactly the O(N)-scan-per-query trap it was built to be, not
  something else.

### Files

- `regex_cpu.csv`, `spin_cpu.csv`, `hibernate_cpu.csv` — `mode,epoch_ms,cpu_load`
  rows sampled once/sec, bad and fixed variants sharing one file per bug so they're
  easy to diff/plot together.
- `throughput.csv` — one row per mode: total ops, elapsed seconds, ops/sec, and a
  free-form note (match count, items processed, persistence-context size, etc).
- `flame-<mode>.html` — 6 self-contained interactive CPU flame graphs (regex-bad,
  regex-fixed, spin-bad, spin-fixed, hibernate-bad, hibernate-fixed).
- `summary.txt` — plain-text bad-vs-fixed table with one takeaway line per bug.
- `run_metadata.csv` — JDK version/vendor, async-profiler version, Maven version,
  host OS/arch, workload durations, hibernate N, timestamp.
- `stdout_<mode>.txt` — raw stdout from each Java run (pid, per-run throughput line,
  Hibernate bootstrap logging).
- `results/attempts/` — reserved for non-reproducing tuning attempts. Empty on this
  run: all three bugs reproduced cleanly on the first real run at the parameters
  above, nothing needed retuning or a fallback shape.

## Honesty notes

These are laptop numbers demonstrating the mechanism, not capacity planning. All
three bugs reproduced cleanly and immediately at the sizes/durations checked into
this repo — bug 3 in particular (the Hibernate flush trap) was the one most likely to
need retuning, since dirty-check cost scales with persistence-context size and entity
shape, but N=8,000 managed entities gave a clean, dramatic (~641x) throughput
collapse on the first try, so no fallback to a plain N+1 shape was needed.

Read the CPU% column with the "normalized across all cores" caveat above in mind —
on a machine with a different core count, bugs 1 and 3's CPU% numbers will land at a
different single-core-equivalent percentage even though the underlying throughput
story (backtracking regex / O(N) flush) is not host-specific. Bug 2's near-100%-vs-
near-0% contrast, by design (one consumer thread per core), is the most
hardware-independent of the three.
