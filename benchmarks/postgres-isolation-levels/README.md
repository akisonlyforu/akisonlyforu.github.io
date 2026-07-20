# postgres transaction isolation harness

This is the harness behind the transaction-isolation post.

It runs one digest-pinned PostgreSQL 17 and measures what the isolation levels
actually buy you. READ COMMITTED is the default in Postgres, and it is not a
correctness guarantee — it only promises you never read uncommitted data. A
naive read-modify-write under RC *silently* loses writes and raises nothing.
REPEATABLE READ and SERIALIZABLE do not make that pattern correct either; they
make it **loud**, by aborting one side with SQLSTATE `40001` and handing you the
retry loop.

Four experiments, all against the same container:

- **A. Lost update** — one `accounts` row, 8 workers x 50 iterations, each doing
  `SELECT balance` → `balance + 1` in Python → `UPDATE ... SET balance = <n>`.
  No retries, on purpose: the whole point is what happens when you don't have
  them. Run at all three levels, plus a fourth row for the *correct* RC pattern
  (`UPDATE accounts SET balance = balance + 1`, atomic, no app-side read).
- **B. Non-repeatable read + phantom** — one transaction reads a row's value and
  a `count(*)` over a predicate; a second connection commits an `UPDATE` and an
  `INSERT` in between; the first transaction re-reads both. RC vs RR.
- **C. Write skew** — the classic on-call doctors invariant. Two doctors, both on
  call. Two concurrent transactions each check `count(*) WHERE on_call >= 2` and
  then take *themselves* off call. Different rows, so no lock conflict, so no
  first-writer-wins to save you. 200 trials at RR and at SERIALIZABLE. The two
  transactions are barrier-synchronised so both read before either writes —
  without that the second one just sees the first one's commit and behaves.
- **D. Cost of serializable** — same read-modify-write workload with a proper
  retry-on-`40001` loop (up to 5 attempts), swept over concurrency
  {2, 4, 8, 16} x keyspace {8 rows, 128 rows} x all three levels. This is the
  throughput and latency price of the safety.

These are laptop measurements demonstrating the mechanism, not capacity-planning
numbers. Everything runs on one machine over loopback with `fsync=off`,
`synchronous_commit=off` and `full_page_writes=off`, so the absolute txn/s
figures are "how fast can Postgres do this with no disk in the way" — several
thousand per second, which no real deployment will match. The *ratios* between
levels and the correctness counts are the point.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/postgres-isolation-levels
docker compose up -d --wait          # postgres 17 on 127.0.0.1:55446

python3 -m venv /tmp/iso-venv && source /tmp/iso-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary_console.txt
docker compose down -v
```

Override with `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `RESULTS_DIR`, `SEED`.

## Results

PostgreSQL 17.10, image `postgres:17@sha256:a426e44b…8508d`, arm64.

### A. Lost update (400 intended increments, no retries)

| level | pattern | final balance | lost | silently lost | 40001 |
|---|---|---|---|---|---|
| READ COMMITTED | naive SELECT-then-UPDATE | 64 | 336 | **336** | 0 |
| REPEATABLE READ | naive SELECT-then-UPDATE | 81 | 319 | 0 | 319 |
| SERIALIZABLE | naive SELECT-then-UPDATE | 84 | 316 | 0 | 316 |
| READ COMMITTED | `SET balance = balance + 1` | **400** | 0 | 0 | 0 |

Read that top row again: 400 transactions committed successfully, every one of
them returned OK, and 336 of the increments are gone. RR and SER lose a similar
*number* of increments, but every single one of those is an error your
application was told about. And the bottom row is the real lesson — RC is fine
if you never round-trip the value through your app.

### B. Read stability inside one transaction

| level | val (1st → 2nd read) | count (1st → 2nd) | non-repeatable | phantom |
|---|---|---|---|---|
| READ COMMITTED | 100 → 999 | 10 → 11 | yes | yes |
| REPEATABLE READ | 100 → 100 | 10 → 10 | no | no |

Postgres RR is snapshot isolation, so it kills phantoms too — the standard only
requires it to prevent non-repeatable reads.

### C. Write skew (200 trials)

| level | trials ending with ZERO doctors on call | 40001 aborts |
|---|---|---|
| REPEATABLE READ | **200 / 200 (100%)** | 0 |
| SERIALIZABLE | **0 / 200 (0%)** | 200 |

This is the one that should scare you. RR blocked the non-repeatable read and
the phantom in experiment B and then let the invariant break in *every single
trial*, quietly, with both transactions committing. SERIALIZABLE's SSI spots the
rw-antidependency cycle and aborts one side every time.

### D. Cost of serializable (100 txns/worker, retry up to 5x)

Hot keyspace (8 rows):

| workers | level | committed | retry % | gave up | txn/s | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| 2 | RC | 200 | 0.0 | 0 | 2314 | 0.75 | 2.04 |
| 2 | RR | 200 | 6.1 | 0 | 2051 | 0.75 | 3.06 |
| 2 | SER | 200 | 5.2 | 0 | 2253 | 0.74 | 2.76 |
| 4 | RC | 400 | 0.0 | 0 | 2975 | 1.05 | 2.70 |
| 4 | RR | 399 | 16.4 | 1 | 2337 | 1.27 | 4.77 |
| 4 | SER | 400 | 18.4 | 0 | 2143 | 1.41 | 7.89 |
| 8 | RC | 800 | 0.0 | 0 | 3871 | 1.93 | 4.85 |
| 8 | RR | 785 | 34.5 | 15 | 2561 | 2.08 | 9.79 |
| 8 | SER | 784 | 34.0 | 16 | 2460 | 2.15 | 9.87 |
| 16 | RC | 1600 | 0.0 | 0 | 5654 | 2.55 | 5.08 |
| 16 | RR | 1512 | 50.6 | 88 | 2785 | 3.04 | 14.37 |
| 16 | SER | 1517 | 49.6 | 83 | 2910 | 2.97 | 14.18 |

Wide keyspace (128 rows), same workload:

| workers | level | retry % | txn/s | p99 ms |
|---|---|---|---|---|
| 16 | RC | 0.0 | 6116 | 4.63 |
| 16 | RR | 6.3 | 4989 | 7.59 |
| 16 | SER | 5.9 | 5476 | 6.43 |

Contention, not the isolation level, is what costs you. At 128 keys and 16
workers SERIALIZABLE is within ~10% of READ COMMITTED. At 8 keys and 16 workers
it's roughly half the throughput and 2.8x the p99 — but READ COMMITTED "won"
that row by cheating. `serializable_cost.csv` carries a `counter_sum` column,
the actual sum of the counters at the end, which should equal the number of
committed transactions:

| keys | workers | level | committed | counter_sum |
|---|---|---|---|---|
| 8 | 16 | READ COMMITTED | 1600 | **823** |
| 8 | 16 | REPEATABLE READ | 1512 | 1512 |
| 8 | 16 | SERIALIZABLE | 1517 | 1517 |

RC's 5654 txn/s is 1600 committed transactions that produced 823 increments.
RR and SER match exactly in all 16 non-RC configurations. That's the trade —
you're not paying for slowness, you're paying for the arithmetic to be right.

## Files

- `lost_update.csv` — experiment A, per level + the atomic-UPDATE control row.
- `read_stability.csv` — experiment B, first/second read values and counts.
- `write_skew.csv` — experiment C, invariant violations and 40001 aborts.
- `serializable_cost.csv` — experiment D, 24 rows (3 levels x 4 concurrencies x 2 keyspaces).
- `summary.txt` — human-readable key numbers.
- `summary_console.txt` — the captured console run.
- `run_metadata.csv` — `version()`, image digest, and every parameter.

## Reproducibility notes

A and C reproduce cleanly — the shapes (RC silently loses, RR/RC report 40001,
RR permits write skew 100% of the time, SER catches it 100% of the time) were
identical across runs. The exact *counts* in A drift a few percent run to run,
because how many of the 400 racing transactions collide depends on thread
scheduling; only the RC-silent-vs-RR-loud contrast and the atomic control row's
exact 400 are stable. B is fully deterministic.

D is the lumpy one. The correctness columns (`counter_sum` vs `committed_txns`)
are rock solid, but throughput on 24 back-to-back configs on a laptop moves
around by 10-20% between runs, and RC occasionally posts a weird p99 outlier
(one earlier run showed 81 ms at 8 workers / 8 keys) from a scheduler hiccup.
Trust the retry-rate and correctness columns; treat the txn/s column as an
order-of-magnitude comparison within a single run, not across runs.
