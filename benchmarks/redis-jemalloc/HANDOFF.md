# Benchmark handoff: redis-jemalloc (for Gemini)

You are building the benchmark harness behind the post **"Redis Brings Its Own malloc, and Here's Why"**
(`collections/_posts/2026-07-20-redis-brings-its-own-malloc.md`). The post is drafted with placeholder
tokens `[[BENCH:*]]`; your job is to produce the real measurements that replace them, plus the
checked-in evidence, following the exact conventions already used by `benchmarks/redis-oom/` and
`benchmarks/pg-stats/`.

Read `benchmarks/redis-oom/README.md`, `benchmarks/redis-oom/HANDOFF.md`, and
`benchmarks/pg-stats/README.md` first. Match their structure, their honesty rules, and their file
layout. Do not invent a house style; copy this one.

The core difference from redis-oom: that harness ran **one** Redis and watched a delete. This harness
runs the **same Redis compiled two ways** and compares them on identical work. The variable under test
is the allocator, nothing else.

---

## 1. The claim you must reproduce (or honestly fail to)

Build Redis twice from the same source and version:
- **jemalloc build** — the default Linux build (`make` / `make MALLOC=jemalloc`), bundled `deps/jemalloc`.
- **libc build** — `make MALLOC=libc`, linking glibc `ptmalloc`.

Run the identical churny workload against each, then after the churn settles the claim is:

1. `used_memory` comes out roughly the same on both builds (it's just the data — allocator-independent).
2. `used_memory_rss` is **higher** on the libc build than the jemalloc build after the same churn.
3. `mem_fragmentation_ratio` (= rss/used) is **higher** on the libc build than the jemalloc build.
4. `mem_allocator` reads `jemalloc-x.y.z` on the first build and `libc` on the second — proof the flag
   actually changed the linked allocator, not just a label.
5. `activedefrag yes` is honored on the jemalloc build and reclaims RSS; on the libc build Redis either
   refuses it or it is a no-op (jemalloc's defrag hint is unavailable). Capture whatever Redis actually
   does on the libc build — do not assume.

If (2)/(3) do **not** come out in jemalloc's favor on this workload, that is a real, publishable result.
Preserve it under `results/attempts/` and say so plainly. The post already commits, in writing, to
reporting a null result if the allocator turns out not to matter here — so a tie or a libc win is not a
failure of the harness, it's the finding. Do not tune the workload until jemalloc wins.

**Workload shape.** The point is churn, not a clean load-then-measure. Load a large set of small keys
across a few value sizes, then run repeated rounds of: overwrite a fraction, delete a fraction, insert
fresh keys — the constant reshaping of a keyspace a real Redis lives through. Same seed, same sequence,
same counts for both builds. Settle, then snapshot. A pure load with no churn will understate the
difference and is the wrong experiment; if you also run a no-churn control, keep it under `attempts/`.

---

## 2. Deliverables (match redis-oom layout)

```
benchmarks/redis-jemalloc/
├── docker-compose.yml       # builds BOTH redis variants (two services or two build args), pinned base
├── Dockerfile               # ARG MALLOC={jemalloc,libc}; builds redis from a pinned source tag
├── benchmark.py             # deterministic churn workload + INFO sampler + self-verification
├── requirements.txt         # pin redis-py (copy redis-oom's pin style)
├── README.md                # same shape as redis-oom/README.md (run steps, results desc, honesty)
└── results/
    ├── comparison.csv           # one row per build (jemalloc / libc) with the settled metrics
    ├── memory_timeline_je.csv   # time-series across the churn run, jemalloc build
    ├── memory_timeline_libc.csv # time-series across the churn run, libc build
    ├── run_metadata.csv         # machine, redis version, source tag, allocator per build, seed shape
    ├── info_memory_jemalloc.txt # raw INFO memory dump, jemalloc build, settled — untouched
    ├── info_memory_libc.txt     # raw INFO memory dump, libc build, settled — untouched
    ├── info_memory_je_defrag.txt# raw INFO memory after activedefrag on the jemalloc build
    └── attempts/                # no-churn control, any shape that didn't separate the two builds
        └── <shape-name>/ ...
```

### Build requirements (Dockerfile + docker-compose.yml)
- Build Redis **from source at a pinned tag** (e.g. `7.4.0`), not from two prebuilt images, so the only
  difference between the two variants is the `MALLOC` flag. Pin the builder base image by digest.
- Same `redis.conf` for both: `--save "" --appendonly no` so persistence doesn't muddy RSS; an explicit
  `--maxmemory` set high enough that eviction never fires (this experiment is about fragmentation, not
  eviction — no OOM here); `--activedefrag no` at baseline, flipped via `CONFIG SET` for the defrag
  probe on the jemalloc build only.
- Bind each Redis to loopback on distinct non-default host ports (e.g. jemalloc 56380, libc 56381).
- **Verify the allocator per build.** `INFO memory` → `mem_allocator` must contain `jemalloc` on the
  first and `libc` on the second. If both report the same allocator, the build flag didn't take and the
  whole experiment is void — stop and fix the Dockerfile before collecting numbers.

### benchmark.py requirements
- Deterministic: fixed key namespace, fixed value sizes, fixed counts, seeded RNG for the churn order.
- Runs the **same** workload object against whichever build's DSN it's pointed at, so jemalloc and libc
  get byte-identical sequences. Drive both from one invocation (`all`) or one build per call — document.
- `--reset` mandatory before a run (FLUSHALL on the dedicated DB only; refuse if the DB looks foreign,
  same defensive marker check redis-oom does).
- Sample `INFO memory` on a fixed interval into the per-build timeline CSV for the whole churn run.
  Capture one settled snapshot per build into `comparison.csv` after a warm-up/settle wait (mirror
  pg-stats' unrecorded warm-up before each captured measurement).
- On the jemalloc build only, after the settled snapshot: `CONFIG SET activedefrag yes` (with the low
  thresholds needed to force a scan, as redis-oom does), let it settle, snapshot into
  `info_memory_je_defrag.txt`. On the libc build, attempt the same and record what Redis returns.
- Self-verify at the end and exit non-zero (after writing evidence) if `mem_allocator` didn't differ
  between builds, or if the two builds produced byte-different `used_memory` beyond a small tolerance
  (that would mean the workloads weren't actually identical). Do **not** fail the run just because
  jemalloc didn't win — that outcome is allowed and must still be recorded.

### INFO / metrics fields to capture (every snapshot + timeline row)
`used_memory`, `used_memory_human`, `used_memory_rss`, `used_memory_rss_human`,
`mem_fragmentation_ratio`, `mem_allocator`, `maxmemory_human`, `evicted_keys`,
`allocator_allocated`, `allocator_active`, `allocator_resident`, `allocator_frag_ratio`,
`allocator_frag_bytes` (jemalloc build only — libc won't report the `allocator_*` block, note that),
`number_of_keys` (via DBSIZE), plus process RSS from the OS side for cross-check.

---

## 3. Token map — what fills each `[[BENCH:*]]` in the post

Replace tokens in `collections/_posts/2026-07-20-redis-brings-its-own-malloc.md` from these sources.
Use `_human` forms for the value labels (e.g. `71.61M`); the `_pct` tokens are bar widths in percent
(e.g. `66%`).

| Token | Source |
|---|---|
| `[[BENCH:redis_version]]` | `run_metadata.csv` redis_version (e.g. `7.4.0`) |
| `[[BENCH:jemalloc_version]]` | jemalloc build `mem_allocator` (e.g. `jemalloc-5.3.0`) |
| `[[BENCH:used_je]]` | jemalloc build settled `used_memory_human` |
| `[[BENCH:rss_je]]` | jemalloc build settled `used_memory_rss_human` |
| `[[BENCH:frag_je]]` | jemalloc build settled `mem_fragmentation_ratio` |
| `[[BENCH:used_libc]]` | libc build settled `used_memory_human` |
| `[[BENCH:rss_libc]]` | libc build settled `used_memory_rss_human` |
| `[[BENCH:frag_libc]]` | libc build settled `mem_fragmentation_ratio` |
| `[[BENCH:result_note]]` | 2–4 sentence honest paragraph: which build held lower RSS and by how much, what the two ratios actually were, and — if jemalloc did NOT win — say that instead. Report, don't dramatize. This replaces the placeholder paragraph directly after the "Where I expect the difference to land" section. |

**Bar-width tokens (figure).** Normalize so the visual comparison is honest:
- `used_*_pct` and `rss_*_pct`: percent of the **larger RSS across both builds** (that build's RSS bar
  becomes ~100%, everything else scales to it). Using one shared denominator for both used and rss bars
  keeps the four memory bars visually comparable.
- `frag_*_pct`: percent of the **higher of the two fragmentation ratios** (higher ratio → ~100%).
- Round to whole percents. Example: if libc RSS is the larger at 92M and jemalloc RSS is 66M, then
  `rss_libc_pct: 100%`, `rss_je_pct: 72%`.

When done, leave no `[[BENCH:*]]` token behind. Grep to confirm:
`grep -n 'BENCH:' collections/_posts/2026-07-20-redis-brings-its-own-malloc.md` = empty.

---

## 4. Honesty rules (non-negotiable, inherited from pg-stats / redis-oom)

- Laptop numbers only. The README and the post both say the mechanism transfers, the megabytes do not.
- The null result is allowed and must be reported. If jemalloc doesn't win, fill `[[BENCH:result_note]]`
  with that truth and set the bars to the real widths regardless of which way they lean.
- Keep every non-separating or control shape (esp. the no-churn control) under `results/attempts/`.
- Raw `INFO memory` dumps captured untouched — no trimming to make the point land harder.
- Build both variants from one pinned source tag; the `MALLOC` flag is the only difference. Record the
  per-build `mem_allocator`, versions, and source tag in run_metadata.csv.
- The self-verification gate fails the run if the two builds didn't actually link different allocators
  or didn't get identical workloads — but NOT merely because of which allocator came out ahead.

---

## 5. Run it (Gemini invocation)

From the repo root:

```bash
gemini "Read benchmarks/redis-jemalloc/HANDOFF.md and build the full harness it specifies:
Dockerfile (ARG MALLOC), docker-compose.yml building both redis variants, benchmark.py,
requirements.txt, README.md, and the results/ tree. Verify mem_allocator differs between the
two builds before trusting any number. Run the identical churn workload against both, capture
real measurements into results/, then fill every [[BENCH:*]] token in
collections/_posts/2026-07-20-redis-brings-its-own-malloc.md using the token map in section 3,
including the normalized figure bar widths. Preserve the no-churn control and any non-separating
shapes under results/attempts/. Do not fabricate any number. Report the honest outcome even if
jemalloc does not win — the post is written to accept a null result."
```

Hand back: the populated post, the `benchmarks/redis-jemalloc/` tree, and a one-paragraph note on which
build held lower RSS, the two fragmentation ratios, and whether the allocator mattered on this workload.
