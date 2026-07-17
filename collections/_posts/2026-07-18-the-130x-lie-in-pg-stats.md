---
layout:     post
title:      The 130x Lie in pg_stats
date:       2026-07-18
description:    A query that used to run in under a millisecond started taking 4 seconds. The index it needed was right there. Postgres wouldn't use it. Here's why, and what fixed it.
categories: postgres query-planner databases performance
---

A query that used to run in under a millisecond started taking 4 seconds. The index it needed was right there, built for exactly this lookup. Postgres wouldn't use it. Here's why, and what fixed it.

## Problem Statement

This is from the admin audit trail service — every state change in the system gets logged: who did it, when, from where. The table's grown to roughly 500 million rows (~1.9 TB). It's fed by two kinds of writers: human admins working in a support console, and automated systems (a CDC pipeline, a job scheduler) that write audit events with no human session attached.

One admin-console feature — "jump to the start of this session," given a session ID, show the first event in it — was fine for months. Then, for some sessions but not all, it started taking 3–5 seconds instead of single-digit milliseconds, and it was hammering the connection pool hard enough to show up in monitoring.

## The Data

```sql
CREATE TABLE audit_events (
    id           BIGSERIAL PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    entity_id    BIGINT NOT NULL,
    session_id   UUID,              -- NULL for system/automated events
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_events_session_id ON audit_events USING btree (session_id);
```

Two things about this data matter, and both fall directly out of how the table is written:

- **`session_id` is NULL about 82% of the time.** Every audit event the CDC pipeline emits when replicating a database change, and every event the job scheduler emits when a scheduled job runs, has no human session attached — only events triggered directly by an admin in the console carry a `session_id`. The column is sparse.
- **Rows sharing a `session_id` are physically clustered.** When a support engineer works a session, they generate a burst of audit events in a tight time window — open a record, edit a few fields, add a note, all within a minute or two. The table is append-only, ordered by insertion time, so one session's rows land on the same handful of physical pages.

Real numbers: ~5,000,000 distinct non-null `session_id` values across the table, averaging ~18 events per session.

## The Query

```sql
SELECT ae.*, s.admin_email, s.ip_address
FROM audit_events ae
JOIN admin_sessions s ON s.id = ae.session_id
WHERE ae.session_id = '3f9a1c2e-77b4-4e1a-9c2d-8a441b0f6a10'
ORDER BY ae.id ASC
LIMIT 1;
```

## Root Cause Analysis

### 1. Look at the plan.

```sql
EXPLAIN (ANALYZE, BUFFERS) <the query above>;
```

```
Limit  (cost=0.99..1847.32 rows=1) (actual time=0.089..4213.664 rows=1 loops=1)
  ->  Nested Loop  (cost=0.99..4930291.05 rows=2670) (actual time=0.088..4213.660 rows=1 loops=1)
        ->  Index Scan using audit_events_pkey on audit_events ae
              (cost=0.57..4820104.88 rows=2670) (actual time=0.061..4213.601 rows=1 loops=1)
              Filter: (session_id = '3f9a1c2e-...'::uuid)
              Rows Removed by Filter: 17,483,206
        ->  Index Scan using admin_sessions_pkey on admin_sessions s
              (cost=0.42..0.44 rows=1) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=112 read=982,441
Planning Time: 0.412 ms
Execution Time: 4213.701 ms
```

`Rows Removed by Filter: 17,483,206` says it all: Postgres walked 17.5 million rows in primary-key order, checking `session_id` on each one, before it got lucky. The index built for exactly this query was never touched.

### 2. Why did the planner pick that plan?

Postgres discounts the cost of a `LIMIT n` scan by roughly `n_needed / rows_available` — the fewer rows you need relative to how many are expected to match, the cheaper the scan looks. The planner believed `session_id = X` would match 2,670 rows, so scanning in primary-key order looked cheap — surely it'd hit a match almost immediately. There were actually ~18 matches, spread thin across the entire table. The estimate was off by nearly three orders of magnitude, and the query paid for the full mistake in wall-clock time.

### 3. Where did 2,670 come from?

```sql
SELECT null_frac, n_distinct FROM pg_stats
WHERE tablename = 'audit_events' AND attname = 'session_id';
```

```
 null_frac | n_distinct
-----------+------------
      0.82 |      38500
```

Postgres estimates matching rows for an equality filter as:

```
rows ≈ total_rows × (1 − null_frac) / n_distinct
     ≈ 500,000,000 × 0.18 / 38,500
     ≈ 2,338      (close to the planner's 2,670 — the rest is plan-specific rounding)
```

Real `n_distinct` for `session_id` is close to 5,000,000, not 38,500. That's a 130x miss. It inflates the assumed match rate by 130x, which is exactly what convinced the `LIMIT` discount that a full scan-by-primary-key would terminate almost instantly.

### 4. Why is n_distinct so wrong?

At the default `default_statistics_target = 100`, `ANALYZE` samples roughly 30,000 pages and estimates distinct values from that sample using the Haas–Stokes estimator. That estimator already has a known negative bias, and clustering makes it much worse: because a session's ~18 rows sit on a handful of adjacent pages, a random-page sample keeps re-hitting the same few sessions over and over instead of seeing a representative spread across the true 5 million distinct values.

## Solution

Raise statistics on the column, re-analyze.

```sql
ALTER TABLE audit_events ALTER COLUMN session_id SET STATISTICS 2000;
ANALYZE audit_events;
```

`n_distinct` improved but still landed short — around 1.1M against a real ~5M. Sampling more pages doesn't undo the clustering; you're just sampling more clustered pages.

Go further, and run `ANALYZE` more than once.

```sql
ALTER TABLE audit_events ALTER COLUMN session_id SET STATISTICS 5000;
ANALYZE audit_events;
ANALYZE audit_events;  -- each run resamples independently, reducing variance
```

At `STATISTICS 5000`, `pg_stats` showed `n_distinct ≈ 4,760,000` and `null_frac = 0.82` — close enough for the planner to make a sane call.

Verify the plan flipped.

```
Limit  (cost=1.13..29.87 rows=1) (actual time=0.041..0.052 rows=1 loops=1)
  ->  Nested Loop  (cost=1.13..534.02 rows=18) (actual time=0.040..0.051 rows=1 loops=1)
        ->  Index Scan using idx_audit_events_session_id on audit_events ae
              (cost=0.56..478.21 rows=18) (actual time=0.031..0.038 rows=18 loops=1)
              Index Cond: (session_id = '3f9a1c2e-...'::uuid)
        ->  Index Scan using admin_sessions_pkey on admin_sessions s
              (cost=0.42..0.44 rows=1) (actual time=0.003..0.003 rows=1 loops=1)
  Planning Time: 0.298 ms
  Execution Time: 0.081 ms
```

Estimated rows (18) matches reality, the planner picks the dedicated index, and execution drops from ~4.2 seconds to ~80 microseconds — roughly 50,000x faster, with shared-buffer reads dropping from ~980K page reads to single digits.

## Operational Notes

- **Not free.** A higher `STATISTICS` target means slower `ANALYZE` runs and more data sampled. If the `ALTER TABLE` runs inside a transaction, that's longer lock hold time too.
- **Statistics reset on major-version upgrades.** After a `pg_upgrade`, custom per-column `STATISTICS` targets need to be reapplied and re-`ANALYZE`d. Easy to forget, and the bug comes back silently after a maintenance window.
- **This isn't Postgres-specific.** Any cost-based optimizer relying on sampled cardinality estimates (MySQL included) is vulnerable to the same failure whenever a column's values are physically clustered instead of uniformly scattered. Rule of thumb: whenever a column's write pattern clusters same-value rows together — anything append-only, grouped by a foreign key — be suspicious of the planner's default row estimates for that column, and check `pg_stats` before assuming the index just isn't being used for some other reason.
