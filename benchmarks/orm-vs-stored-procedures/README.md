# ORM vs stored procedures harness

The harness behind a debunk-style post: **"ORM vs stored procedure" is not one
axis, it's several confounded ones.** When you hold the *generated SQL* constant,
the gap nearly vanishes. The scary numbers people attribute to "the ORM" actually
come from named, nameable issues — N+1, client-side materialization, and
non-parameterized ad-hoc SQL — none of which is "ORM" as a category.

It runs a digest-pinned Postgres 16, seeds a realistic `customers` / `orders`
schema (5,000 customers, ~50,000 orders, with the fk index a sane person adds),
and for every experiment writes a PL/pgSQL function that returns *exactly the same
result set* as the ORM/raw path it is compared against. Each experiment isolates
one axis.

## The four experiments

- **A. Identical SQL (the mirage)** — a parameterized single-join lookup (one
  customer + their most recent order), three ways: SQLAlchemy ORM, raw psycopg,
  and a PL/pgSQL function. Same SQL underneath. ORM adds only a small constant.
- **B. N+1** — fetch M customers then their orders: naive lazy-load (1 + M real
  round trips, both raw and ORM `lazy` relationship) vs an eager join (1) vs the
  SP (1). Measures latency *and* round-trip count. The naive path is the outlier;
  labelled as misuse, not "ORM loses".
- **C. Materialization** — fetch K rows: ORM full object hydration with the
  identity map / change tracking vs raw psycopg tuples vs SP tuples. Same SQL
  underneath — this isolates the client-side hydration cost. Latency + peak memory.
- **D. Plan cache / parameterization** — the same logical aggregate run many
  times: ad-hoc string-concatenated SQL (a new query text every call) vs a
  parameterized query (psycopg auto-prepares it) vs the SP. Reads
  `pg_stat_statements` for entry count and parse/plan time.

## Results (3 full runs, means over runs)

Postgres 16.14, SQLAlchemy 2.0.51, psycopg 3.3.4. p50 latencies in microseconds.

| Experiment | variant | p50 (µs) | note |
|---|---|---:|---|
| **A** identical SQL | raw psycopg | 189 | 1 round trip |
| | stored proc | 192 | 1 round trip |
| | ORM SQLAlchemy | 315 | 1 round trip — a ~125 µs constant, same SQL |
| **B** N+1 | raw naive (1+M) | 19,379 | **101 round trips** |
| | ORM naive lazy (1+M) | 35,095 | **101 round trips** |
| | ORM eager join | 6,012 | 1 round trip |
| | raw eager join | 704 | 1 round trip |
| | stored proc | 746 | 1 round trip |
| **C** materialization | raw tuples | 2,630 | 843 KiB peak |
| | SP tuples | 2,608 | 842 KiB peak |
| | ORM hydration | 9,669 | **4,444 KiB peak** — objects + identity map |
| **D** plan cache | ad-hoc concat | 249 | plan ~0.009–0.015 ms/call, **1** pgss entry |
| | parameterized | 214 | plan ~0.0004 ms/call, **1** pgss entry |
| | stored proc | 210 | plan ~0.0002 ms/call, **1** pgss entry |

### What each experiment actually shows

- **A** — ORM, raw, and SP are within a small constant of each other. The ORM
  overhead is Python-side object/statement machinery, not the database. This is
  the whole point of the post: with the SQL held constant there is almost nothing
  to argue about.
- **B** — the gap is enormous (naive is ~25–50× the eager/SP paths) and it is
  *entirely* the round-trip count. The eager ORM join and the SP both collapse to
  one round trip; the raw eager join and the SP are basically tied. N+1 is the
  villain, not the ORM.
- **C** — raw tuples ≈ SP tuples. ORM hydration is ~3.7× the latency and ~5× the
  memory, and that cost is 100% client-side (building mapped objects and
  registering them in the Session's identity map). Same query, same rows.
- **D — the honest one.** Two things people say about ad-hoc SQL are tested here,
  and they don't both hold:
  1. **"Ad-hoc SQL explodes `pg_stat_statements`."** On modern Postgres, *false.*
     `pg_stat_statements` normalizes literal constants, so 500 ad-hoc queries with
     500 different inlined ids collapse to **one** normalized entry — same as the
     parameterized and SP variants (all show `pgss_entries = 1`).
  2. **"Parameterizing / SPs cache the plan."** *True, but small, and it's
     parameterization doing the work, not the stored procedure.* Ad-hoc pays real
     parse+plan on every call (~0.009–0.015 ms), while the parameterized query
     (auto-prepared by psycopg after 5 uses) and the SP both drop to
     ~0.0004 ms/call — a ~20–30× planning gap. For a query this cheap that gap is
     a small slice of total latency, so the p50s barely separate. The SP has no
     plan-caching magic the parameterized query lacks.

## Reproducibility & honesty notes

- **Everything reproduced in direction across all 3 runs.** A, C are rock-solid
  (p50 CV < 6%). B's *ordering* is stable to orders of magnitude, though the SP/raw
  eager p50s are a bit lumpy on a laptop (CV up to ~12%). D's `pgss_entries` and
  the adhoc-vs-parameterized planning *ratio* are consistent every run; the
  *absolute* ad-hoc plan time is noisy (CV ~24%) because it's sub-15 µs and
  competing with laptop scheduler noise. Nothing was hidden or smoothed — see the
  `STABILITY` block in `results/summary.txt` for the per-run CV.
- **These are laptop numbers demonstrating mechanism, not capacity numbers.**
  Absolute microseconds depend on the machine, Docker's networking layer, and
  loopback latency. The *contrasts* (identical-SQL parity, the N+1 blowup, the
  hydration tax, parameterization vs ad-hoc planning) are what the post rests on,
  and those are what stay stable.

## Run it

Docker with Compose v2, plus Python 3.10+ (psycopg 3.3 dropped 3.9; tested on 3.13).

```bash
cd benchmarks/orm-vs-stored-procedures
docker compose up -d --wait          # postgres on 127.0.0.1:5433

python3.13 -m venv /tmp/orm-venv && source /tmp/orm-venv/bin/activate
pip install -r requirements.txt

python benchmark.py                  # seeds, runs all 4 experiments 3x, writes results/
docker compose down -v               # tear everything down
```

Everything is env-configurable: `PGHOST` `PGPORT` `PGDATABASE` `PGUSER`
`PGPASSWORD`, `RESULTS_DIR` (default `results/`), `RUNS` (default 3), and the
per-experiment iteration knobs (`A_N`, `B_M`, `B_N`, `C_K`, `C_N`, `D_N`, plus
`*_WARM`; see the top of `benchmark.py`). Warmup iterations are discarded; each
measured path reports p50/p95/p99/mean/stdev.

## Results files

- `summary.txt` — the captured console run (means over runs + the per-run stability table).
- `exp_a_identical_sql.csv` — per-run, per-variant latency percentiles for A.
- `exp_b_n_plus_1.csv` — B, including the `round_trips` column.
- `exp_c_materialization.csv` — C, including `peak_kib`.
- `exp_d_plan_cache.csv` — D, including `pgss_entries`, `pgss_calls`, and `plan_ms_per_call`.
- `run_metadata.csv` — Postgres version, image digest, library versions, iteration counts, seed sizes.
- `attempts/` — kept for any lumpy/non-reproducible shapes worth preserving (empty when the headline suite is clean).
