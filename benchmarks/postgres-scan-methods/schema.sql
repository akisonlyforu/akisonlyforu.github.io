-- Identity marker so the harness can refuse to seed anything but the
-- dedicated scan_bench database (mirrors the pg-stats harness guard).
CREATE TABLE scan_benchmark_identity (
    marker TEXT PRIMARY KEY CHECK (marker = 'scan_bench_v1')
);

INSERT INTO scan_benchmark_identity (marker) VALUES ('scan_bench_v1');

-- The single large table every experiment reaches into three different ways:
-- Sequential Scan, Index Scan, and Index-Only Scan.
CREATE TABLE events (
    id          bigint PRIMARY KEY,   -- append-ordered surrogate key
    user_id     bigint NOT NULL,      -- high-cardinality lookup key (~5 rows each)
    status      smallint NOT NULL,    -- low-cardinality 0..4
    amount      integer NOT NULL,     -- payload column
    bucket      integer NOT NULL,     -- id % 1000: 1000 buckets for the selectivity sweep
    created_at  timestamptz NOT NULL
);

-- Autovacuum stays off so the visibility map is only ever set by the explicit
-- VACUUM the harness runs. That is what lets Experiment 2 honestly show the
-- "before VACUUM -> Heap Fetches > 0" versus "after VACUUM -> Heap Fetches: 0"
-- contrast instead of a background worker silently enabling index-only scans.
ALTER TABLE events SET (autovacuum_enabled = false);
