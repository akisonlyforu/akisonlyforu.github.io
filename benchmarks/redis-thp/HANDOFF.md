# Benchmark handoff: redis-thp (for Gemini)

You are building the harness behind the post **"The p99.9 That Only Spiked While We Were Scaling Up"**
(`collections/_posts/2026-07-20-the-p999-that-only-spiked-while-scaling.md`). The post is drafted with
placeholder tokens `[[BENCH:*]]`; produce the real measurements that replace them, plus the checked-in
evidence, following the conventions already used by `benchmarks/pg-stats/` and `benchmarks/cache-aside/`.

Read `benchmarks/pg-stats/README.md` first and match its structure, honesty rules, and file layout.

---

## 0. ENVIRONMENT CAVEAT — read before anything else

Transparent Huge Pages is a **host kernel** setting, not a per-container one. A container shares the
host's THP state via `/sys/kernel/mm/transparent_hugepage/enabled`. To run the THP-on vs THP-off arms of
this benchmark you must be able to write that file as root **on the host kernel**.

- Run this on a **Linux host you control**: a bare-metal Linux box, a cloud VM, or a local Linux VM.
- **macOS Docker Desktop will not work** for the THP toggle — Redis runs inside the LinuxKit VM whose
  THP state you can't reliably set. If you're on the user's Mac, either provision a Linux VM
  (multipass/lima/a cloud instance) or STOP and report that the THP arms can't be measured here.
- This limitation is not a bug to work around — it is literally the subject of the post (the standard
  fix needs host access most readers don't have). If you can only measure the diskless-replication arm
  and not the THP toggle, that is still a partial, honest result: capture what you can, mark the THP
  arms as not-run in the README, and say why.

Record in `run_metadata.csv`: kernel version, the THP `enabled` and `defrag` values seen, whether the
host was writable, and the allocator (`mem_allocator` from INFO — must be jemalloc).

---

## 1. The claim you must reproduce (or honestly fail to)

A write-served Redis primary, when a fresh replica pulls a **full sync**, shows a p99.9 latency spike
that p50/p99 hide, and the spike is caused by copy-on-write page faults amplified by THP, not by the
fork itself. Specifically:

1. Steady state: p50/p99/p99.9 all low and close.
2. During a replica full sync with **THP=always + disk sync** (`repl-diskless-sync no`): p99.9 spikes
   hard (target the ~hundreds-of-ms shape), while p50/p99 barely move.
3. `latest_fork_usec` is **small** — the fork is cheap. The spike is post-fork COW, not fork.
4. `/proc/<redis-pid>/smaps` shows `AnonHugePages` dominating RSS (THP is backing the heap).
5. **THP=never + disk sync**: the same sync's p99.9 drops sharply — direct fix.
6. **THP=always + `repl-diskless-sync yes`**: p99.9 is softened (shorter fork-child window) without any
   host change — the fix available when you don't own the kernel.

If steady vs sync doesn't diverge in the p99.9, or THP=never doesn't fix it, preserve that under
`results/attempts/` and say so, exactly like pg-stats kept the shapes that didn't trip the planner.

**Workload must be write-heavy on the primary during the sync.** COW faults are only paid when the
*parent* writes to shared pages. A read-only load will not reproduce the spike — that's a good negative
control to keep under attempts/.

---

## 2. Deliverables (match pg-stats layout)

```
benchmarks/redis-thp/
├── docker-compose.yml       # digest-pinned redis primary + replica; --save "" --appendonly no
├── benchmark.py             # loader + steady write load + latency sampler + sync trigger + self-verify
├── requirements.txt         # pin redis-py (cache-aside style)
├── README.md                # pg-stats shape: env caveat, run steps, results desc, honesty
└── results/
    ├── latency_percentiles.csv   # one row per scenario: p50/p99/p99.9 for steady and during-sync
    ├── latency_timeline.csv      # per-sample latency over time, with the scale-out marker timestamp
    ├── fork_and_mem.csv          # latest_fork_usec, AnonHugePages, sync duration, per scenario
    ├── run_metadata.csv          # machine, kernel, THP state, redis+jemalloc versions, dataset shape
    ├── latency_doctor_thp_on.txt # raw LATENCY DOCTOR + LATENCY HISTORY output, untouched
    ├── smaps_thp_on.txt          # raw /proc/<pid>/smaps_rollup (or grep AnonHugePages), untouched
    ├── smaps_thp_off.txt
    └── attempts/                 # non-reproducing shapes (read-only load, tiny dataset, etc.)
        └── <shape-name>/ ...
```

### Scenario matrix (run each; one primary reused, replica re-attached per arm)
| Arm | THP (host) | repl-diskless-sync | Expectation |
|---|---|---|---|
| A | always | no | p99.9 spike (the bug) |
| B | never | no | p99.9 flat (textbook fix) |
| C | always | yes | p99.9 softened (managed-cluster fix) |
| control | always | no | read-only load → no spike (keep under attempts/) |

### benchmark.py requirements
- Deterministic: fixed dataset size, fixed value size, seeded RNG, fixed write rate.
- Load a dataset big enough that the forked child lives long enough to accumulate a visible COW storm
  (size it up until arm A reproduces; record the size that worked, and keep undersized attempts).
- Drive a **steady write workload** against the primary and sample round-trip latency continuously into
  `latency_timeline.csv`; compute p50/p99/p99.9 over the steady window and over the sync window.
- Trigger the full sync by attaching the replica (`REPLICAOF`/`REPLICAOF NO ONE` to force a fresh
  FULLRESYNC), and mark the timestamp in the timeline.
- Capture per arm: `latest_fork_usec` (INFO stats), `LATENCY DOCTOR` + `LATENCY HISTORY fork`,
  `AnonHugePages` from `/proc/<pid>/smaps_rollup`, and the full-sync duration.
- Toggle THP on the host between arms A/C and B via the script (needs root; if not root, skip B and mark
  it). Reset `LATENCY RESET` and warm up before each captured window, like pg-stats' warm-up.
- Self-verify and exit non-zero (after writing evidence) if arm A's sync p99.9 is not materially above
  its steady p99.9, or if arm B doesn't flatten it. A story that didn't happen locally doesn't ship.

---

## 3. Token map — what fills each `[[BENCH:*]]` in the post

Replace tokens in `collections/_posts/2026-07-20-the-p999-that-only-spiked-while-scaling.md` from these.

| Token | Source |
|---|---|
| `[[BENCH:redis_version]]` | run_metadata redis_version |
| `[[BENCH:kernel]]` | run_metadata kernel (uname -r) |
| `[[BENCH:dataset_keys]]` | run_metadata key count that reproduced arm A |
| `[[BENCH:dataset_bytes]]` | dataset footprint (used_memory_human after load) |
| `[[BENCH:ops_rate]]` | steady write rate driven (ops/sec) |
| `[[BENCH:p50_steady]]` / `[[BENCH:p99_steady]]` / `[[BENCH:p999_steady]]` | latency_percentiles, arm A steady window |
| `[[BENCH:p50_sync]]` / `[[BENCH:p99_sync]]` / `[[BENCH:p999_sync_thp_on]]` | latency_percentiles, arm A sync window |
| `[[BENCH:fork_ms]]` | `latest_fork_usec` (arm A) converted to ms — show it's small |
| `[[BENCH:latency_doctor]]` | latency_doctor_thp_on.txt (paste the relevant lines verbatim) |
| `[[BENCH:anon_hugepages]]` | AnonHugePages line from smaps_thp_on.txt |
| `[[BENCH:p999_sync_thp_off]]` | latency_percentiles, arm B sync window p99.9 |
| `[[BENCH:p999_diskless]]` | latency_percentiles, arm C sync window p99.9 |
| `[[BENCH:sync_duration_thp_on]]` | full-sync duration, arm C (diskless) — the shorter window |
| `[[BENCH:sync_duration_thp_off]]` | full-sync duration, arm B (kept for completeness) |
| `[[BENCH:failed_shape_note]]` | one honest sentence on what didn't reproduce (from attempts/), matching pg-stats' "first 5 million rows behaved perfectly." If everything reproduced first try, say that. |

**Figures.** Two `<figure class="cache-bench">` blocks carry placeholder geometry:
- Timeline SVG: recompute `p50` and `p999` polyline `points` from `latency_timeline.csv` (x → 90..600,
  y so peak→30, zero→210). p50 flat and low; p999 jumps at the marker and decays over the sync window.
- The three-arm bar figure: set each `--value:NN%` from the real p99.9 of each arm relative to arm A
  (arm A = 100%).

When done: remove the `<!-- DRAFT ... -->` comment, delete the `published: false` line, and confirm
`grep -n 'BENCH:' <post>` is empty.

---

## 4. Honesty rules (inherited from pg-stats)

- Numbers from one Linux host you control. README + post both say the mechanism transfers, the ms don't.
- Keep non-reproducing shapes (read-only control, undersized dataset) under `results/attempts/`.
- Raw `LATENCY DOCTOR`, `LATENCY HISTORY`, and `smaps` captured untouched.
- Digest-pin the redis image. Record kernel, THP state, allocator, versions in run_metadata.
- The self-verification gate must fail the run if arm A didn't spike or arm B didn't fix it.
- If the environment can't toggle THP (section 0), that's a real outcome — report the partial result,
  never fake the THP arms.

---

## 5. Run it (Gemini invocation)

From the repo root, on a Linux host you control:

```bash
gemini "Read benchmarks/redis-thp/HANDOFF.md, heed section 0's environment caveat, and build the full
harness it specifies: docker-compose.yml, benchmark.py, requirements.txt, README.md, and the results/
tree. Run arms A/B/C plus the read-only control, capture real measurements into results/, then fill
every [[BENCH:*]] token in collections/_posts/2026-07-20-the-p999-that-only-spiked-while-scaling.md
using the token map in section 3, recompute both figure geometries from the CSVs, and remove the DRAFT
comment and the published:false line. Preserve non-reproducing shapes under results/attempts/. Do not
fabricate any number — if the host can't toggle THP or a value wasn't measured, say so and mark that arm
not-run rather than inventing it."
```

Hand back: the populated post, the `benchmarks/redis-thp/` tree, and a one-paragraph note on what
reproduced, what didn't, and whether the THP toggle was available in your environment.
