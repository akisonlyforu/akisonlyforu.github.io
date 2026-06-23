# Benchmark handoff: redis-oom (for Gemini)

You are building the benchmark harness behind the post **"Redis Said It Was Fine. The OOM Killer Didn't."**
(`collections/_posts/2025-02-06-redis-said-it-was-fine.md`). The post is drafted with placeholder
tokens `[[BENCH:*]]`; your job is to produce the real measurements that replace them, plus the
checked-in evidence, following the exact conventions already used by `benchmarks/pg-stats/`.

Read `benchmarks/pg-stats/README.md` and `benchmarks/cache-aside/` first. Match their structure,
their honesty rules, and their file layout. Do not invent a house style; copy this one.

---

## 1. The claim you must reproduce (or honestly fail to)

A queue-shaped Redis workload that mass-deletes millions of small keys shows all four of these at once:

1. After the bulk delete, `used_memory` drops sharply but `used_memory_rss` stays near its peak
   (jemalloc holds freed-but-dirty pages instead of returning them to the kernel).
2. `mem_fragmentation_ratio` (= rss/used) spikes well above 1 right after the delete.
3. `maxmemory` is enforced against `used_memory`, so Redis evicts **nothing** (`evicted_keys` stays 0)
   even as real RSS climbs toward the container's memory limit.
4. Under a hard cgroup memory limit, RSS reaching the limit triggers an OOM kill — while `used_memory`
   still looks calm.

Then the fix must also reproduce:

5. `activedefrag yes` brings RSS back down after the same delete (fragmentation ratio settles).
6. Deleting the same keys **incrementally** with `UNLINK` in batches keeps the RSS peak far lower than
   the single bulk delete.

If any of 1–4 does not happen locally, that is a real result: preserve it under `results/attempts/`
and say so in the README, exactly like pg-stats kept the 5M/20M shapes that didn't trip the planner.
A story that didn't happen locally does not go in the post.

---

## 2. Deliverables (match pg-stats layout exactly)

```
benchmarks/redis-oom/
├── docker-compose.yml       # digest-pinned redis, hard mem_limit, jemalloc allocator
├── benchmark.py             # deterministic workload + INFO sampler + self-verification
├── requirements.txt         # pin redis-py (see cache-aside for the pin style)
├── README.md                # same shape as pg-stats/README.md (run steps, results desc, honesty)
└── results/
    ├── memory_snapshots.csv     # one row per named checkpoint (before/after/defrag/incremental)
    ├── memory_timeline.csv      # time-series sampled every N ms across the whole run
    ├── run_metadata.csv         # machine, redis version, jemalloc version, config, seed shape
    ├── info_memory_before.txt   # raw INFO memory dump, untouched
    ├── info_memory_after.txt    # raw INFO memory dump right after bulk delete, untouched
    ├── info_memory_defrag.txt   # raw INFO memory dump after activedefrag settled
    ├── info_memory_incremental.txt
    └── attempts/                # shapes that did NOT reproduce the RSS spike, preserved
        └── <shape-name>/ ...    # same CSVs + raw dumps per attempt
```

### Config requirements (docker-compose.yml)
- Pin the redis image by digest (`redis:7.x@sha256:...`), same discipline as pg-stats' postgres pin.
- `--save "" --appendonly no` so persistence doesn't muddy RSS (cache-aside already does this).
- Set an explicit `--maxmemory` and `--maxmemory-policy allkeys-lru` so eviction *could* fire — the
  point is that it doesn't, because it's measured against used_memory.
- Baseline runs with `--activedefrag no`; the fix run flips it to `yes` (do it via CONFIG SET in the
  script so one container serves both, or document two compose profiles).
- Apply a hard container memory limit (`mem_limit:` under compose, or `deploy.resources.limits.memory`)
  set close enough above the loaded footprint that the post-delete RSS climb can actually reach it.
  This is what makes the OOM real instead of theoretical. Bind Redis to loopback on a non-default host
  port (cache-aside uses 56379).
- Confirm the build uses jemalloc: `INFO memory` → `mem_allocator` must contain `jemalloc`. If the
  image ships libc malloc, the whole experiment is void — record `mem_allocator` in run_metadata.csv.

### benchmark.py requirements
- Deterministic: fixed key namespace, fixed value size, fixed count, seeded RNG if any randomness.
- Sub-commands mirroring pg-stats' CLI feel (`all --reset`, plus tunables `--keys`, `--value-bytes`,
  `--maxmemory`, `--mem-limit`, `--delete-batch`). Changing them = a different experiment; say so.
- `--reset` mandatory before a run (FLUSHALL on the dedicated DB only; refuse if the DB looks foreign,
  same defensive check pg-stats does with its identity marker).
- Sample `INFO memory` on a fixed interval into `memory_timeline.csv` for the whole run, and capture
  named snapshots into `memory_snapshots.csv` at: loaded/settled, immediately-after-bulk-delete,
  after-activedefrag-settled, and after-incremental-delete.
- Also read the OS/cgroup side (container `memory.current` or `/sys/fs/cgroup/memory.*`, and process
  RSS) so the timeline correlates Redis's own RSS figure with what the kernel is enforcing.
- Give every measured phase one warm-up / settle wait before snapshotting, like pg-stats' unrecorded
  warm-up before each captured EXPLAIN.
- Self-verify at the end and exit non-zero (after writing evidence) if the baseline did NOT show the
  RSS-stays-high / evicted_keys==0 behavior, or if activedefrag did NOT recover RSS. Mirror pg-stats'
  "fail the command if the planner story didn't happen."

### INFO / metrics fields to capture (every snapshot + timeline row)
`used_memory`, `used_memory_human`, `used_memory_rss`, `used_memory_rss_human`,
`mem_fragmentation_ratio`, `mem_allocator`, `maxmemory`, `maxmemory_human`, `maxmemory_policy`,
`evicted_keys`, `allocator_allocated`, `allocator_active`, `allocator_resident`,
`allocator_frag_ratio`, `allocator_frag_bytes`, `number_of_keys` (via DBSIZE), plus the OS-side
cgroup `memory.current` and the OOM outcome (killed? at what RSS? exit reason).

---

## 3. Token map — what fills each `[[BENCH:*]]` in the post

Replace tokens in `collections/_posts/2025-02-06-redis-said-it-was-fine.md` from these sources.
Use `_human` forms for prose tokens (e.g. `812.44M`), raw integers only where noted.

| Token | Source |
|---|---|
| `[[BENCH:redis_version]]` | `run_metadata.csv` redis_version |
| `[[BENCH:jemalloc_version]]` | `INFO memory` `mem_allocator` (jemalloc x.y.z) |
| `[[BENCH:keys_loaded]]` | snapshot `number_of_keys` at loaded/settled |
| `[[BENCH:value_bytes]]` | run_metadata value-size arg |
| `[[BENCH:maxmemory]]` | `maxmemory_human` (configured value) |
| `[[BENCH:cgroup_limit]]` | container mem_limit from run_metadata |
| `[[BENCH:used_before]]` | snapshot "before" `used_memory_human` |
| `[[BENCH:rss_before]]` | snapshot "before" `used_memory_rss_human` |
| `[[BENCH:frag_before]]` | snapshot "before" `mem_fragmentation_ratio` |
| `[[BENCH:used_after]]` | snapshot "after-bulk-delete" `used_memory_human` |
| `[[BENCH:rss_after]]` | snapshot "after-bulk-delete" `used_memory_rss_human` |
| `[[BENCH:frag_after]]` | snapshot "after-bulk-delete" `mem_fragmentation_ratio` |
| `[[BENCH:evicted_keys]]` | snapshot "after-bulk-delete" `evicted_keys` (expect 0) |
| `[[BENCH:rss_after_defrag]]` | snapshot "after-activedefrag" `used_memory_rss_human` |
| `[[BENCH:frag_after_defrag]]` | snapshot "after-activedefrag" `mem_fragmentation_ratio` |
| `[[BENCH:rss_incremental]]` | peak RSS during the incremental-UNLINK run |
| `[[BENCH:frag_incremental]]` | fragmentation ratio at that peak |
| `[[BENCH:oom_outcome]]` | one honest sentence: did the container OOM-kill at the limit, at what RSS, or did decay/defrag stop it first. Do not dramatize — report what happened. |
| `[[BENCH:failed_shape_note]]` | one sentence on which loaded shape(s) did NOT move RSS (from attempts/), matching pg-stats' "first 5 million rows behaved perfectly" honesty. If everything reproduced first try, say that instead. |

**Figures.** Both `<figure class="cache-bench">` blocks carry placeholder geometry:
- Timeline SVG: recompute the `used` and `rss` polyline `points` from `memory_timeline.csv`
  (x = time normalized to 90..600, y = memory normalized so peak→30, zero→210). The `used` line
  drops at the delete marker; the `rss` line stays flat and high.
- The maxmemory-vs-cgroup bar figure: set each `--value:NN%` from the real ratio of the value to its
  limit (used_memory/maxmemory on the left, rss/cgroup_limit on the right).

When done, delete the `<!-- DRAFT ... -->` manifest comment at the top of the post and remove this line
of instruction; leave no `[[BENCH:*]]` token behind. Grep to confirm: `grep -n 'BENCH:' the post` = empty.

---

## 4. Honesty rules (non-negotiable, inherited from pg-stats)

- Laptop numbers only. The README and the post both say the mechanism transfers, the megabytes do not.
- Keep every non-reproducing shape under `results/attempts/` with its raw dumps. Do not delete failures.
- Raw `INFO memory` dumps are captured untouched — no trimming to make the point land harder.
- Digest-pin the image. Record `mem_allocator`, versions, and the cgroup limit in run_metadata.csv.
- The self-verification gate must actually fail the run if the core behavior didn't happen.

---

## 5. Run it (Gemini invocation)

From the repo root:

```bash
gemini "Read benchmarks/redis-oom/HANDOFF.md and build the full harness it specifies:
docker-compose.yml, benchmark.py, requirements.txt, README.md, and the results/ tree.
Run it, capture real measurements into results/, then fill every [[BENCH:*]] token in
collections/_posts/2025-02-06-redis-said-it-was-fine.md using the token map in section 3,
recompute both figure geometries from the CSVs, and remove the DRAFT manifest comment.
Preserve any non-reproducing shapes under results/attempts/. Do not fabricate any number —
if a value wasn't measured, the run isn't done."
```

Hand back: the populated post, the `benchmarks/redis-oom/` tree, and a one-paragraph note on what
reproduced and what didn't.
