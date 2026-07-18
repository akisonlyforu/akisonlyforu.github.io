# postgres logical-shards resharding harness

A reproducible harness for the cost of **resharding**. If you place a workspace's
rows with `physical = hash(workspace_id) % P`, then adding one machine (`P -> P+1`)
changes the modulus, so almost every key rehashes and almost every row has to
physically move. The fix (what Notion did) is a fixed, large pool of **logical**
shards mapped to physical machines through a **lookup table**:
`logical = hash(workspace_id) % 480` never changes, and `physical = lookup[logical]`
is the only thing you touch on a rebalance. Rescaling then re-points whole logical
shards at new machines — a known subset of rows — with **zero** application-side
rehash.

A physical shard is modeled as a Postgres **schema** (`shard_p0 .. shard_p{P-1}`)
inside one digest-pinned Postgres 16 instance. Moving a logical shard = moving its
rows between schemas with `INSERT INTO ... SELECT` + `DELETE` in one transaction,
and we count the rows actually moved in SQL. A physical shard = a schema; a data
move = a real Postgres row move, counted by real row counts. No N-container theater.

**Hash.** `blake2b(str(workspace_id), digest_size=8) -> int`, used identically for
`% P` (physical modulo) and `% L` (logical). Not Python's builtin `hash()`.

**Data.** ~200,000 workspaces, each with a skewed 1–20 row count (cubic skew toward
1), keyed by `workspace_id` so a workspace's rows co-locate. The captured run
generated **1,202,279 rows** (avg 6.01 rows/workspace). Fixed seed `1234`.

## Experiments

- **A. The modulo resharding tax.** Placement is `hash % P`. For each transition
  `P_old -> P_new` in {4→5, 4→6, 4→8, 8→12} we count the rows whose
  `(hash%P_old) != (hash%P_new)` — they must physically move. Counted over the
  dataset's real per-workspace row counts. Writes `exp_a_modulo.csv`.
- **B. Logical shards + lookup table (real data movement).** `L=480` logical shards,
  `logical = hash % 480` (invariant). We actually **load** all rows into `P_old`
  schemas via the lookup table, then rebalance to `P_new` by re-pointing the minimum
  set of logical shards (target `L/P_new` per machine), moving their rows between
  schemas in one transaction and counting the moved rows in SQL. Done for 4→6 and
  8→12. Key→logical churn is **0%** by construction; we verify `(count, sum(row_id))`
  is identical before and after so no row is lost or duplicated. Writes
  `exp_b_logical.csv`.
- **C. Why the logical count should be highly composite.** For `P` in
  {3,4,5,6,8,10,12,15,16,24,32} we distribute `L` logical shards across `P` machines
  (contiguous groups) and measure per-machine row-load imbalance. We compare
  `L=480` (highly composite, `2^5·3·5`) vs `L=479` (prime) vs `L=500`. A subset
  (L∈{480,479}, P∈{6,16}) is **measured** by real per-schema row counts (loaded and
  counted in SQL); the rest is shard-count arithmetic over the same real
  per-logical-shard row counts. The `source` column labels which is which. Writes
  `exp_c_composite.csv`.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/postgres-logical-shards
docker compose up -d --wait          # postgres 16 on 127.0.0.1:55442

python3 -m venv /tmp/pglogical-venv && source /tmp/pglogical-venv/bin/activate
pip install -r requirements.txt

python benchmark.py                  # writes results/ ; console mirrors it
docker compose down -v
```

Env knobs (defaults in parens): `PGHOST`(127.0.0.1) `PGPORT`(55442)
`PGPASSWORD`(shardbench) `N_WORKSPACES`(200000) `MAX_ROWS_PER_WS`(20) `L`(480)
`SEED`(1234) `RESULTS_DIR`(results/).

## Results (captured run: PostgreSQL 16.14, 1,202,279 rows)

**A — modulo tax (rows that must move):**

| transition | rows moved | pct rows | pct workspaces |
|-----------|-----------:|---------:|---------------:|
| 4→5  | 961,006 | 79.9% | 79.9% |
| 4→6  | 802,972 | 66.8% | 66.7% |
| 4→8  | 599,328 | 49.8% | 49.9% |
| 8→12 | 800,977 | 66.6% | 66.6% |

**B — logical shards + lookup (real moves):**

| transition | logical shards moved | rows moved | pct rows | key→logical churn | checksum identical |
|-----------|---------------------:|-----------:|---------:|------------------:|:------------------:|
| 4→6  | 160 | 401,550 | 33.4% | 0.0% | yes |
| 8→12 | 160 | 403,506 | 33.6% | 0.0% | yes |

Same `4→6` rescale: **66.8%** of rows move under plain modulo vs **33.4%** under the
lookup table — and the lookup table moves *whole logical shards* (a known set) with
0% key churn, while modulo rehashes two-thirds of every key.

**C — why 480:** `480 = 2^5·3·5` divides evenly (shards-per-machine spread = 0) for
**every** `P` tested; `479` (prime) never does (spread = 1 for all `P`); `500` only
for `P ∈ {4,5,10}`. See `exp_c_composite.csv` for the full table and measured vs
derived row loads.

- `summary.txt` — structured headline numbers per experiment.
- `console.log` — full console output of the captured run.
- `exp_a_modulo.csv` / `exp_b_logical.csv` / `exp_c_composite.csv` — per-experiment data.
- `run_metadata.csv` — postgres version, image digest, hash function, all params.

## What reproduced cleanly, what was lumpy

A and B reproduce sharply and are essentially exact: the modulo tax lands right on
the textbook `(N-1)/N`-style fractions (4→5 ≈ 80%, 4→8 ≈ 50%, 4→6 and 8→12 ≈ 66.7%),
and the lookup table moves exactly `160/480 = 33.3%` of the data with a bit-for-bit
identical id checksum and 0% key churn. The **lumpy** part is C's *row-ratio* column:
because 480 logical shards over ~1.2M rows put only ~2,500 rows in a shard, one extra
shard on a machine (the prime/`500` case, spread = 1) is under ~1% of that machine's
load, so the row ratios for 480 vs 479 look close. The crisp, honest signal in C is
the **`divides_evenly` / `shards_spread`** column — the arithmetic of divisibility —
not the row ratio; the row ratio only widens noticeably at large `P` (e.g. `L=500,
P=32` hits 1.14x vs 480's 1.06x).

These are laptop numbers. The point is the **mechanism and the ratio** — two-thirds
of rows moved and every key rehashed under modulo vs a third of rows moved and zero
key churn under a lookup table — not absolute throughput or a capacity statement.
