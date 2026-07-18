# Attempts / tuning notes

## Experiment 3 — first design did not show the eviction contrast

The first cut of the calcification experiment (phase B = write 60,000 unique 8 KB
items once, then sleep) did NOT reproduce the intended "evictions drop under
automove" story, for two reasons:

1. **60,000 × 8 KB ≈ 480 MB into a 64 MB cache.** The large workload can't fit no
   matter how the pages are allocated, so *both* automove modes evict heavily —
   the eviction count is dominated by capacity, not by the allocator.
2. Global evictions actually went *up* under `automove=2`, because reassigning a
   page to the large class first evicts the ~5,461 stale small items living on it.
   Read as a global number it looks like automove made things worse.

Captured from that run (memcached 1.6.45, for the record):

```
automove=0  phase B: large pages 1,  large evicted 59,882, global evict since A  59,882
automove=2  phase B: large pages 63, large evicted 52,566, global evict since A 391,149
```

The page migration (1 → 63) was real and correct, but the eviction number told the
wrong story.

## What fixed it (the shipped version)

- Size the large working set to fit *after* a full rebalance (5,000 × 8 KB ≈ 44 MB,
  ~44 of 64 pages), so once the large class gets pages the set goes resident.
- Rewrite that working set repeatedly (15×, 1 s apart) to give the automove thread
  both the eviction pressure to react to and the wall-clock time to move pages —
  a single burst stalls the rebalance partway.
- Report **large-class** evictions (the new workload's own thrashing), not global
  evictions (which include the one-time reclamation of the dead small items).

Result: 74,882 large-class evictions frozen at 1 page vs 1,976 once rebalanced to
44 pages. That is the version in `../exp3_calcification.csv`.
