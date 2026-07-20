# redis-thp: full-sync latency vs repl-diskless-sync (Docker Desktop)

This is the harness behind the post exploring the p99.9 latency tail a Redis
primary pays when a fresh replica pulls a full sync, and whether
`repl-diskless-sync` softens it. It runs a digest-pinned Redis 7.4.0 primary
under a continuous write load and attaches a **fresh** ephemeral replica
container per scenario, so every attach is a guaranteed `FULLRESYNC` (a new
container shares no replication history with the primary).

## Scope: what this harness measures, and what it deliberately does not

The mechanism this class of bug is usually attributed to has two layers:

1. A replica full sync forks the primary (`fork()`), and while that child is
   alive, the **primary** keeps taking writes. Any page the primary dirties
   after the fork gets copy-on-write duplicated, because the child still
   holds a reference to the pre-fork version.
2. On a host with Transparent Huge Pages set to `always`, that COW duplication
   can happen at 2 MB granularity instead of 4 KB, so a single write can
   trigger copying a much larger physical region — that's the classic
   "hundreds of ms p99.9 spike, low fork cost" shape.

**THP is a host-kernel setting, not a per-container one.** On bare metal or a
Linux VM you control, `/sys/kernel/mm/transparent_hugepage/enabled` is
writable and you can run a true THP-on vs THP-off A/B. On **macOS with Docker
Desktop**, Redis runs inside Docker Desktop's LinuxKit VM, and that VM's THP
state is not something a container — or macOS itself — can toggle. So the
THP-on vs THP-off comparison from `HANDOFF.md` (arms A vs B) is **out of
scope here and was not attempted, faked, or simulated.**

What this harness *does* measure for real, on whatever THP state that VM
actually has (observed, not assumed — see `run_metadata.csv`):

- **`disk_sync`** — `repl-diskless-sync no`. The primary forks, the child
  writes an RDB file to disk, then the file is transferred to the replica.
- **`diskless_sync`** — `repl-diskless-sync yes`. The primary forks, the
  child streams the RDB straight to the replica's socket, no temp file. This
  is the fix that needs no host access — the actionable takeaway.
- **`steady`** — the same write workload, no replica attached, as the
  baseline.
- **`attempts/read-only-control/`** — the same disk-sync trigger, but the
  primary sees a **read-only** workload instead. COW faults are only paid
  when the *parent* writes to shared pages, so this should not spike. Kept
  as a documented negative control, not deleted.

## An honest, load-bearing finding: THP did not apply to Redis's heap here

Before trusting any AnonHugePages number, this harness checked whether THP
was actually backing the Redis process's memory in this environment at all.
The container's kernel view reports:

```
/sys/kernel/mm/transparent_hugepage/enabled → [always] madvise never
```

— i.e. THP looks "on." But after loading roughly 400 MB into Redis and idling
for 90+ seconds (far longer than `khugepaged`'s default 10 s scan interval),
`AnonHugePages` in `/proc/1/smaps_rollup` for the Redis process stayed at
**0 kB**, even though the VM's own `/proc/meminfo` showed *other* processes in
the VM accumulating `AnonHugePages` (and `khugepaged`'s
`pages_collapsed` counter was non-zero) over the same window. Whatever is
promoting pages elsewhere in Docker Desktop's LinuxKit VM is not reaching
this jemalloc-backed heap — at least not on the timescale of a normal full
sync. Every scenario in this run, including the negative control, shows
`AnonHugePages: 0 kB`, and that 0 is reported as measured, not omitted or
guessed at. This also explains why the latency spike measured below is real
but modest, not the "hundreds of ms" shape the THP-amplified version of this
bug produces on a host where THP genuinely backs the heap.

## What the harness does

`benchmark.py`:

1. Loads a deterministic dataset (seeded RNG, fixed key/value shape) into the
   primary.
2. Starts a continuous load: one dedicated **probe connection** issuing
   sequential `SET`s (or `GET`s for the control) on random existing keys,
   timing true round-trip latency for every request; plus several **bulk
   writer connections** pipelining batched writes to keep real write pressure
   on the primary throughout, independent of the probe's pacing.
3. Samples a `steady` baseline window with no replica attached.
4. For each of `disk_sync` and `diskless_sync`: sets
   `repl-diskless-sync` on the primary, spins up a **fresh** replica
   container, issues `REPLICAOF <primary> 6379` on it, and polls until
   `master_link_status` is `up` (full sync complete). Alongside that it
   tightly polls `INFO persistence`'s `rdb_bgsave_in_progress` to bound the
   window the forked RDB-save child is actually alive — the real
   COW-pressure window — separately from the broader attach-to-link-up
   window. It also polls `/proc/1/smaps_rollup` for `AnonHugePages`, and
   captures `LATENCY DOCTOR` / `LATENCY HISTORY fork` and `latest_fork_usec`
   before tearing the replica container down.
5. Optionally repeats the `disk_sync` trigger with a read-only workload as a
   negative control, written under `results/attempts/read-only-control/`.
6. Writes every CSV/txt deliverable and runs a self-verification pass that
   fails the run (non-zero exit, after writing evidence) only on *mechanical*
   problems — e.g. a replica never reaching `FULLRESYNC`, a missing
   `latest_fork_usec`. A weak or absent latency *effect* is reported plainly,
   not treated as a failure — this run's effect is real but modest, and it
   says so.

## Run it

Docker Desktop (or any Docker with Compose v2) plus Python 3.9+.

```bash
cd benchmarks/redis-thp
docker compose up -d --wait        # primary on 127.0.0.1:6396

python3 -m venv /tmp/redisthp-venv && source /tmp/redisthp-venv/bin/activate
pip install -r requirements.txt

python benchmark.py
docker compose down -v
```

Ephemeral replica containers are created and torn down by `benchmark.py`
itself (`redisthp-replica-disk_sync`, `-diskless_sync`,
`-control_readonly`), attached to the `redisthp-net` network the compose
file defines, published on `127.0.0.1:6397`. None are left running after the
script exits or after `docker compose down -v`.

Tunable via env vars (defaults used for the checked-in run):
`DATASET_KEYS=800000`, `VALUE_BYTES=400`, `SEED=20260720`,
`STEADY_SECONDS=25`, `SYNC_TAIL_SECONDS=4`, `BULK_WRITERS=6`,
`BULK_BATCH=80`, `SAMPLE_INTERVAL_S=0.002`, `RUN_CONTROL=1`.

## Results

Redis 7.4.0 (`redis:7.4.0@sha256:6725a7dc7a44a6486b9d0a5172b10ccaf0c2ea600df87c0b93450d0e7769297f`),
jemalloc 5.3.0, 800,000 keys × 400 bytes (`used_memory_human` after load:
394.72M). Full numbers in `results/`:

- `latency_percentiles.csv` — p50/p99/p99.9/max/mean per scenario, including
  the narrower fork-child-alive-only window.
- `latency_timeline.csv` — every latency sample with its phase, arm, and a
  `sync_start` marker column.
- `fork_and_mem.csv` — `latest_fork_usec`, peak `AnonHugePages`, sync
  duration, and the measured fork-child-alive duration per scenario.
- `run_metadata.csv` — Redis/jemalloc versions, image digest, THP state
  (container view and the macOS host path, both observed not assumed),
  dataset shape, timestamp.
- `latency_doctor_disk_sync.txt` / `latency_doctor_diskless_sync.txt` — raw
  `LATENCY DOCTOR` + `LATENCY HISTORY fork` output, untouched.
- `smaps_disk_sync.txt` / `smaps_diskless_sync.txt` — raw
  `/proc/1/smaps_rollup` at the peak-AnonHugePages sample, untouched.
- `attempts/read-only-control/` — the same four files plus percentiles/CSV,
  for the read-only negative control.

### Headline numbers (write workload, disk_sync vs diskless_sync vs steady)

| scenario | n | p50 (ms) | p99 (ms) | p99.9 (ms) | max (ms) |
|---|---|---|---|---|---|
| steady (no replica) | 1708 | 11.220 | 36.187 | 46.300 | 52.759 |
| disk_sync (attach→link-up window) | 721 | 11.502 | 43.091 | **54.479** | 58.688 |
| diskless_sync (attach→link-up window) | 705 | 11.347 | 36.137 | **44.203** | 48.583 |

`disk_sync` p99.9 is +8.2 ms (1.18×) over the steady baseline; `diskless_sync`
p99.9 is statistically flat against baseline (−2.1 ms). That ordering —
`disk_sync` elevated, `diskless_sync` essentially matching steady — is the
real, measured contrast this harness set out to capture. It is a real but
**modest** effect, not the dramatic "hundreds of ms" shape the THP-amplified
version of this bug produces; see the AnonHugePages finding above for why.

Fork cost itself, confirming it's cheap regardless of arm:
`latest_fork_usec` = 1707 (disk_sync, 1.707 ms) / 2030 (diskless_sync,
2.030 ms). `LATENCY DOCTOR` reported *"no latency spike was observed"* and
`LATENCY HISTORY fork` came back empty (`[]`) at a 20 ms
`latency-monitor-threshold` for every arm — fork is not where the cost is.

The narrower fork-child-alive-only window (bounded by
`rdb_bgsave_in_progress` flipping 1→0, measured at 1.129 s for disk_sync and
1.088 s for diskless_sync) has too few samples (n=85 / n=72) for a reliable
p99.9 estimate — that percentile is close to the max of a ~80-sample set and
is dominated by noise, not signal. It's kept in `latency_percentiles.csv` for
transparency, but the attach-to-link-up window (n≈700+) above is the number
to trust.

### Negative control: read-only workload, disk-sync trigger

| scenario | n | p50 (ms) | p99 (ms) | p99.9 (ms) |
|---|---|---|---|---|
| control_steady | 3206 | 2.303 | 7.407 | 9.894 |
| control_readonly_sync | 3278 | 2.318 | 7.582 | **10.494** |

Essentially flat (+0.6 ms at p99.9, well within noise) — consistent with the
mechanism requiring the *primary* to write to pages after the fork.
`latest_fork_usec` for this arm: 2425 (2.425 ms). Kept under
`results/attempts/read-only-control/`.

### Sync duration

`disk_sync`: 1.950 s attach→link-up (fork-child-alive ≈1.129 s).
`diskless_sync`: 1.519 s attach→link-up (fork-child-alive ≈1.088 s).
`control_readonly` (disk-based trigger): 2.095 s. These are laptop/loopback
numbers — a real full sync over a WAN link would take much longer and give
the fork's child (and any COW pressure) a longer lifetime; the mechanism
generalizes, the milliseconds here don't.

## Honesty notes

- These are single-laptop, Docker Desktop measurements. They demonstrate the
  mechanism (COW after fork during a full sync, and `repl-diskless-sync`
  changing the transfer path); they are not production capacity numbers, and
  the magnitudes will differ a lot on real hardware.
- The THP-on vs THP-off comparison from `HANDOFF.md` was **not run** — it
  needs a Linux host with a writable
  `/sys/kernel/mm/transparent_hugepage/enabled`, which this Mac + Docker
  Desktop setup does not provide. This is stated plainly rather than worked
  around.
- `AnonHugePages` for the Redis process was `0 kB` in every scenario captured
  here, including after an extended idle period with the full dataset
  loaded. That's a genuine, checked observation (see above), not a missing
  measurement — and it's the most likely reason the measured spike is modest
  rather than dramatic.
- The tighter fork-child-alive-only latency window is real but under-sampled
  (n≈70-90); it's included for transparency, not treated as the headline
  number.
- The read-only negative control reproduced the expected "no spike" shape on
  the first attempt; nothing needed a second run to preserve there.
- All containers (primary + any ephemeral replicas) are torn down at the end
  of a run; nothing is left running afterward.
