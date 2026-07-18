# redis heavy-hitters / robotic ad-click harness

Real-time "robotic ad-click" invalidation on a click firehose. Some sources
(IPs / device fingerprints) are bots clicking at abnormally high frequency, and a
fraction of click events are exact replays of an earlier click-id. To flag bots
and drop replays in real time you'd naively keep (a) an exact per-source click
counter and (b) an exact set of every seen click-id. Both grow **unbounded** with
unique sources / unique ids.

RedisBloom does the same job in **fixed** memory: a Count-Min Sketch for
per-source frequency, a Top-K for the heavy-hitter list, and a Bloom filter for
id dedup. The whole point: exact structures climb into the hundreds of MB and keep
growing; the probabilistic ones stay a few MB fixed and still flag every planted
bot and catch every replay.

One deterministic synthetic stream (fixed seed `1337`) drives four experiments:

- **Exp 1 — exact counting grows unbounded.** Feed the stream into a Python dict
  `source -> count`, a Redis `HASH`, and a Python `set` of seen click-ids. Record
  memory at 100k / 250k / 500k / 1M unique sources → the growth curve.
- **Exp 2 — Count-Min Sketch.** Same stream through a fixed-width Redis CMS.
  Compare CMS estimate vs true count for the planted bots and a sample of humans
  (overestimate mean / median / p99 / max, reported separately for bots and humans).
- **Exp 3 — Top-K.** Same stream through Redis TOPK. Pull `TOPK.LIST` and check
  recall: did all planted bots land in the top-K, and did any human rank above a bot?
- **Exp 4 — Bloom dedup vs exact set.** Detect replayed click-ids. Compare an exact
  Python set against a Redis Bloom filter sized for the id volume: memory of each,
  Bloom's measured false-positive rate on a held-out set of genuinely-new ids, and
  dedup recall.

## Stack

Redis Stack (bundles RedisBloom: `CMS.*`, `TOPK.*`, `BF.*`), digest-pinned and bound
to loopback only. Client is `redis-py` 5.3.0 (`r.cms()`, `r.topk()`, `r.bf()`).

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/redis-heavy-hitters
docker compose up -d --wait          # redis-stack on 127.0.0.1:6399

python3 -m venv /tmp/hh-venv && source /tmp/hh-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

The full run feeds ~2.43M clicks and takes ~80s on a laptop. Every stream knob is
env-overridable (`N_HUMANS`, `BOT_CLICKS`, `REPLAY_P`, `CMS_WIDTH`, `TOPK_K`,
`BLOOM_CAP`, …) and `RESULTS_DIR` redirects the CSV output; the seed is fixed so
the numbers are stable across runs.

## Results (Redis 7.4.7, this machine)

Stream: 1,000,000 human sources + 20 bots × 50,000 clicks = **2,430,865 clicks**,
2,138,760 unique click-ids, 292,105 replay events.

**Exp 1 — exact grows unbounded**

| unique sources | dict (source→count) | Redis HASH | id-set | unique ids |
|---:|---:|---:|---:|---:|
| 100,000 | 13.6 MB | 8.2 MB | 20.5 MB | 213,596 |
| 250,000 | 31.6 MB | 20.1 MB | 47.1 MB | 534,522 |
| 500,000 | 63.4 MB | 40.2 MB | 94.4 MB | 1,068,707 |
| 1,000,000 | 126.8 MB | 80.4 MB | 190.0 MB | 2,138,714 |

Source counter + id set together reach **~317 MB at 1M sources and keep climbing**.

**Exp 2 — Count-Min Sketch (width 20000 × depth 5 = 0.80 MB, fixed)**

| items | overestimate mean | median | p99 | max | true |
|---|---:|---:|---:|---:|---|
| 20 bots | 57 | 59 | 65 | 65 | ~50,000 |
| 2000 humans | 58.3 | 59 | 75 | 80 | 1–4 |

CMS overestimates every item by roughly the same ~58-count collision floor:
negligible on a bot (0.11%), but larger than a human's entire true count. Fine for
finding heavy hitters, useless for exact small counts — which is the point.

**Exp 3 — Top-K (k=50, width 1000 × depth 8 = 0.067 MB, fixed)**

20/20 planted bots recalled; bots occupy ranks 1–20 at count ~50,000; **0 humans
ranked above any bot**. The remaining 30 slots of the k=50 list are ordinary humans
at true count 4 — four orders of magnitude below the bots.

**Exp 4 — Bloom dedup vs exact set**

| structure | memory | false-positive rate | dedup recall |
|---|---:|---:|---:|
| exact Python set | 190.0 MB | 0.000% | 100% |
| Redis Bloom (cap 2.5M, err 0.1%) | 4.9 MB | 0.011% (22/200k) | 100% |

**Headline:** exact source-counter + id-set climb to ~317 MB at 1M sources and keep
growing; **CMS + Top-K + Bloom = 5.8 MB fixed**, flagged 100% of planted bots and
caught 100% of replays at 0.011% false positives.

## Laptop numbers, not a capacity statement

These are single-machine measurements demonstrating the *mechanism* — exact
structures grow linearly with unique keys while the probabilistic ones stay fixed,
and the sketches still separate bots from humans and catch replays. They are not a
throughput or capacity benchmark. Memory for the Python dict/set is a `sys.getsizeof`
deep sum; the Redis HASH / CMS / TopK / Bloom sizes are `MEMORY USAGE` (an estimate
for large collections). Bloom's measured FP rate (0.011%) sits below its 0.1% design
target because 2.14M ids were inserted into a filter sized for 2.5M — a Bloom filter
runs under its nominal error until it fills to capacity.

## Result files

- `summary.txt` — the captured console run used above.
- `exp1_exact_growth.csv` — unique sources vs dict / Redis HASH / id-set bytes.
- `exp2_cms_error.csv` — per sampled source: true_count, cms_estimate, abs_error.
- `exp3_topk_list.csv` — the returned top-K list with counts, flagged planted vs human.
- `exp4_dedup.csv` — structure, bytes, false_positive_rate, dedup_recall.
- `run_metadata.csv` — Redis version, image digest, redis-py version, and all params.
