# memcached slab-allocator harness

A digest-pinned memcached and a `pymemcache` driver that measure what the slab
allocator actually does with your memory: it carves RAM into 1 MB pages, splits
each page into fixed-size chunks belonging to one slab class, and rounds every
item UP to the nearest chunk. The bytes you stored (`mem_requested`) and the
bytes it pinned (`used_chunks * chunk_size`) are two different numbers, and the
gap is internal fragmentation you paid for.

`benchmark.py` drives `docker run` directly (not compose) because each experiment
needs different memcached flags — `-m`, `-f`, `-o slab_automove=` — all against the
same pinned image.

Three experiments:

- **1. Slab rounding waste** — probe the real chunk-size ladder, then store the same
  400,000 items at a value size that lands *just over* a slab boundary (worst case)
  vs one that fills a chunk *snugly* (best case), in the same class. Per-class
  `chunk_size`, `used_chunks`, `mem_requested`, allocated, `waste_bytes`, `waste_pct`.
- **2. Growth-factor knob** — the worst-case size under default `-f 1.25` vs a tighter
  `-f 1.08` (finer classes). Total waste and the number of slab classes each creates.
- **3. Slab calcification** — fill a 64 MB cache with tiny items so every page lands
  in the small class, then switch the workload to large items. With `slab_automove=0`
  the large class is frozen at its lone page and thrashes; with `slab_automove=2`
  memcached reassigns pages to it. Measured as large-class evictions.

Stats come straight off the wire — `stats`, `stats slabs`, `stats items` parsed from
the raw text protocol. In memcached 1.6, `mem_requested` is reported by `stats items`,
not `stats slabs`, so the harness merges the two by class id.

## Results (captured, memcached 1.6.45)

| experiment | headline |
|---|---|
| 1 — rounding waste | same 400k items, chunk 1184: **20.2 % of allocated RAM wasted** (value just over the 944-byte boundary) vs **0.0 %** (snug fit) — 96 MB thrown away for identical data |
| 2 — growth factor | worst-case size: `-f 1.25` wastes **20.2 %** across **39** slab classes; `-f 1.08` wastes **2.4 %** across **63** classes |
| 3 — calcification | same large working set: `automove=0` frozen at **1 page → 74,882** large-class evictions; `automove=2` rebalanced to **44 pages → 1,976** evictions |

Files:

- `results/exp1_worst_case.csv`, `results/exp1_best_case.csv` — per-class waste + totals + global bytes.
- `results/exp2_growth_factor.csv` (+ `exp2_f1_25.csv`, `exp2_f1_08.csv`) — waste vs factor, class count.
- `results/exp3_calcification.csv` — automove 0 vs 2, per phase: global evictions, large-class pages, small free chunks, large-class evictions.
- `results/run_metadata.csv` — memcached version, image digest, pymemcache version, params.
- `results/summary.txt` — the captured console run.

These are laptop numbers demonstrating the mechanism, not a capacity plan. The
absolute byte counts scale with the value sizes and item counts chosen here; what
generalizes is the shape — rounding up to a chunk wastes up to nearly a full growth
step, a tighter factor trades that waste for more classes, and a class that owns all
the pages doesn't give them back on its own.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/memcached-slabs

python3 -m venv /tmp/mc-venv && source /tmp/mc-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt   # ~2-3 min; it starts/stops its own containers
```

The script pins the image by digest
(`memcached:1.6@sha256:dc561d52…`), owns the full container lifecycle, and tears
everything down on exit (including on error). `docker-compose.yml` is only a
convenience for poking a single instance by hand:

```bash
docker compose up -d --wait                      # memcached on 127.0.0.1:11311
printf 'stats slabs\r\n' | nc 127.0.0.1 11311
docker compose down -v
```

Env knobs: `MC_HOST` (127.0.0.1), `MC_PORT` (11311), `RESULTS_DIR` (`./results`),
`MC_IMAGE` (the pinned digest), `N_ITEMS` (400000, experiments 1 and 2).

## Notes on what reproduced cleanly

Experiments 1 and 2 are deterministic — the chunk ladder, the boundary, and the
waste percentages come out the same every run (worst-case waste ≈ one growth step,
so ~20 % at `-f 1.25`, ~2.4 % at `-f 1.08`). Experiment 3's page migration (1 → 44
pages) is robust; the exact eviction counts wobble a little run to run because they
depend on how fast the automove thread reassigns pages under load. The large working
set is deliberately sized to fit *after* a full rebalance and is rewritten under
sustained pressure so `automove=2` has both the eviction signal and the time to move
pages — without that pressure the rebalance stalls partway. Global evictions are
*higher* under `automove=2` (reassigning a page evicts the stale small items sitting
on it); the number that matters for the new workload is the large-class eviction
count, which is what drops.
