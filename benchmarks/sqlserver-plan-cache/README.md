# SQL Server plan-cache harness

This is the harness behind [The Query Plans That Only Ran Once](../../collections/_posts/2025-03-13-sql-plan-cache-parameterization.md).

It runs a digest-pinned SQL Server 2022 and reproduces what unparameterized
queries do to the plan cache, and what parameterizing them costs you back.

Three experiments against a 2,000,000-row `orders` table:

- **A. Plan-cache bloat** — 500 lookups with baked-in literals vs 500 runs of the same query parameterized with `sp_executesql`. Counts cached plans and cache size from `sys.dm_exec_cached_plans`.
- **B. Optimize for ad hoc workloads** — the same ad-hoc flood with the server setting off vs on, measuring plan-cache size.
- **C. Parameter sniffing** — a parameterized plan compiled for a rare `status` value, then reused for the common one, vs the same query with `OPTION (RECOMPILE)`.

These are laptop measurements demonstrating the mechanism, not production capacity
numbers. The image is amd64-only and runs under emulation on arm64 hosts.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/sqlserver-plan-cache
docker compose up -d --wait          # SQL Server on 127.0.0.1:11433

python3 -m venv /tmp/mssql-venv && source /tmp/mssql-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

## Results

- `summary.txt` — the captured console run used in the post.
- `plan_cache.csv` — cached plans, cache KB, and wall time for ad-hoc vs parameterized.
- `optimize_adhoc.csv` — plan-cache size with "optimize for ad hoc workloads" off vs on.
- `sniffing.csv` — runtimes for the primed-rare, sniffed-common, and recompiled-common cases.
- `run_metadata.csv` — SQL Server version, query count, row count.

The checked-in run is SQL Server 2022 (16.0.4265.3), 2,000,000 rows, 500 queries.
