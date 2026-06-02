# postgres sharding routing harness

A reproducible harness for the routing economics behind horizontal Postgres
sharding (the Figma-style model). A query engine ("DBProxy") routes a query to
**one** shard when the query carries the shard key, but must **fan out to all**
shards (scatter-gather) when it doesn't. The whole story: single-shard queries
stay cheap and flat as you add shards; scatter-gather queries get linearly more
expensive. Plus colocation: two tables sharded on the *same* key join on one
shard; tables sharded on *different* keys force a cross-shard fan-out.

Logical shards are modeled as **N separate databases** (`shard_0 .. shard_{N-1}`)
inside one digest-pinned Postgres 16 instance — faithful to Figma's "many logical
shards colocated on one physical host" model. A tiny router hashes the shard key
(`md5(key) % N`, stable across runs — *not* Python's builtin `hash`) to pick the
shard.

**Connections are persistent per shard** (a dict of one autocommit connection per
shard DB, reused across all iterations). So the scatter-gather cost we measure is
**N query round-trips**, not N TCP handshakes — the honest routing story. If you
wanted to count connection-setup cost you'd measure something else; we
deliberately don't.

Everything is indexed (`file_key` and `created_by` on `objects`, `object_id` on
comments) so the fan-out cost comes from *touching every shard*, not from a
missing index. This is a routing-cost story, not a seq-scan story.

## Experiments

- **A. Routing (N=8).** Q1 single-shard `SELECT ... FROM objects WHERE file_key=$1`
  → router picks 1 shard, runs on 1 DB. Q2 scatter-gather
  `SELECT ... FROM objects WHERE created_by=$1` (no shard key) → run on all 8
  shard DBs and merge. Reports p50/p99/mean, shards_touched (1 vs 8), physical
  shard-queries issued, and rows returned.
- **B. Scaling.** Rebuild the same ~50k objects redistributed at N = 1, 2, 4, 8.
  For each N, p99 and mean of single-shard vs scatter-gather. Expectation:
  single-shard flat across N; scatter-gather grows ~linearly with N.
- **C. Colocation (N=8).** Colocated join: `objects JOIN comments` on the same
  `file_key` → both rows live on the same shard → 1-shard join. Cross-shard join:
  a second `comments2` table sharded by `author` (a *different* key), so an
  object's comments are scattered — reconstructing object+comments requires
  reading the object's own shard, then fanning out to all shards on `object_id`.

### The cross-shard shape, honestly

`comments` is sharded by `file_key` (colocated with `objects`); `comments2` holds
the same logical comments but is sharded by `author`. Sharding comments by author
is a plausible real choice (e.g. "all of a user's activity on one shard"), and it
is exactly what breaks object-centric reads: to list a given object's comments you
no longer know which shard they're on, so you scatter-gather all N. That is the
contrivance and it is the point — the same data, sharded on a different key, turns
a 1-shard read into an N-shard read.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/postgres-sharding
docker compose up -d --wait          # postgres 16 on 127.0.0.1:55432

python3 -m venv /tmp/pgshard-venv && source /tmp/pgshard-venv/bin/activate
pip install -r requirements.txt

python benchmark.py                  # writes results/ ; console mirrors it
docker compose down -v
```

Env knobs (defaults in parens): `PGHOST`(127.0.0.1) `PGPORT`(55432)
`PGPASSWORD`(shardbench) `TOTAL_ROWS`(50000) `ITERATIONS`(200) `WARMUP`(10)
`SEED`(1234) `RESULTS_DIR`(results/). The captured run used `ITERATIONS=500
WARMUP=25`.

## Results

- `summary.txt` — the structured headline numbers per experiment.
- `console.log` — the full console output of the captured run.
- `exp_a_routing.csv` — per-iteration latency, shards_touched, shard_queries, rows.
- `exp_b_scaling.csv` — `n_shards, single_p99_ms, scatter_p99_ms, single_mean_ms, scatter_mean_ms`.
- `exp_c_colocation.csv` — colocated vs cross-shard p50/p99/mean and shards_touched.
- `run_metadata.csv` — postgres version, image digest, params, headline numbers.

## What reproduced cleanly, what was lumpy

A and C reproduce the mechanism sharply on every run: single-shard / colocated
queries touch 1 shard and sit well under a millisecond at p50; scatter-gather /
cross-shard touch all 8 and run several times slower, with p99 ratios of roughly
5–10x. In experiment B the **mean** tells the honest linear story —
`scatter_mean` scales close to linearly with N (~1x → ~2x → ~4x → ~6–7x from N=1
to N=8) while `single_mean` stays flat. The **p99** in B is lumpy and sometimes
even decreases as N grows: these queries are sub-millisecond, so the p99 tail is
dominated by OS/GC jitter on a laptop, not by routing cost. We report both and
lean on the mean for the scaling claim; that's why the CSV carries both columns.

These are laptop numbers. The point is the **mechanism and the ratio** — one shard
vs N shards — not absolute throughput or capacity. Absolute latencies drift run to
run with background load; the shape (flat single-shard, linearly-growing
scatter-gather, 1-shard vs N-shard joins) does not.
