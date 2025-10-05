-- Tiny seed table so the per-request query hits a real row instead of a bare
-- SELECT 1. A single row is enough for this benchmark.
CREATE TABLE IF NOT EXISTS bench_seed (id int PRIMARY KEY, note text);
INSERT INTO bench_seed (id, note) VALUES (1, 'ok') ON CONFLICT (id) DO NOTHING;
