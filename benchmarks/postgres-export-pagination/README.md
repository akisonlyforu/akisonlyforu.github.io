# postgres export pagination harness

A reproducible harness for the classic **deep `LIMIT/OFFSET` collapse**. An export
service streams a whole table out one page at a time — a CSV export of every issue
in a project. The naive implementation pages with:

```sql
SELECT ... FROM issues ORDER BY id LIMIT 1000 OFFSET :k
```

As `:k` grows deep, Postgres has to walk the index/heap from the start and **throw
away the first `k` rows on every page** to reach the window you asked for. Page 1
discards nothing; the last page discards ~all of the table. Summed over the whole
export that is **O(N²)** work: per-page latency climbs linearly with the offset,
and total export time blows up.

Two fixes, both measured here:

- **Keyset (seek) pagination** — remember the last id you saw and seek past it:

  ```sql
  SELECT ... FROM issues WHERE id > :last_id ORDER BY id LIMIT 1000
  ```

  The B-tree seeks straight to the boundary, so every page reads exactly 1000 rows.
  Flat per-page latency, O(N) total.

- **Server-side cursor** — one `DECLARE ... CURSOR` + repeated `FETCH 1000`. A
  single plan, a single scan, streamed in batches. Also O(N), single-pass.

Everything runs against one digest-pinned Postgres 16 instance. The table is
indexed on its primary key (`id`), so the OFFSET cost comes purely from **scanning
and discarding the offset rows**, not from a missing index. This is an
`O(N²)`-work story, not a seq-scan story.

## The table

One `issues` table, ~200 bytes of text per row:

```
id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
project_id integer
status     text
title      text   (~40 chars)
body       text   (~150 chars)
created_at timestamptz
```

Loaded via `COPY` with a fixed seed. Because `id` is gapless identity `1..N`, the
row at `ORDER BY id OFFSET k` is exactly `id = k+1` — that lets exp4 line up the
OFFSET and keyset `EXPLAIN`s on the *identical physical rows*, so the only
difference measured is the discard work.

## Experiments

1. **exp1_offset_pages.csv** — OFFSET export end-to-end (offset 0, 1000, 2000, …).
   Per page: `page_index, offset, page_latency_ms, rows`. Plus total wall-clock.
2. **exp2_keyset_pages.csv** — keyset export, same page size, same columns/order.
   Per page: `page_index, last_id, page_latency_ms, rows`. Plus total wall-clock.
3. **exp3_cursor.csv** — server-side named cursor: one `DECLARE`, repeated
   `FETCH 1000`. Per batch: `batch_index, batch_latency_ms, rows`. Plus wall-clock.
4. **exp4_explain.csv** — `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for the OFFSET
   query at a shallow offset (0) vs a deep offset (990000), and the keyset query at
   the equivalent deep boundary. Captures `rows_scanned`, `rows_discarded`,
   `actual_total_time_ms`, and `shared_hit_blocks + shared_read_blocks`. This is
   the mechanism evidence.

Each export runs `REPEATS` times; the reported **total wall-clock is the median**
of the repeats, and the per-page detail is the run whose wall time is that median.
The cache is **warmed with one full keyset pass before timing** so results aren't
dominated by cold reads (noted in `run_metadata.csv`).

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/postgres-export-pagination
docker compose up -d --wait          # postgres 16 on 127.0.0.1:55433

python3 -m venv /tmp/pgexport-venv && source /tmp/pgexport-venv/bin/activate
pip install -r requirements.txt

python benchmark.py                  # writes results/ ; console mirrors it
docker compose down -v
```

Env knobs (defaults in parens): `PGHOST`(127.0.0.1) `PGPORT`(55433)
`PGPASSWORD`(exportbench) `TOTAL_ROWS`(1000000) `PAGE_SIZE`(1000) `REPEATS`(3)
`SEED`(1234) `DEEP_OFFSET`(990000) `RESULTS_DIR`(results/).

## Results

See `results/`:

- `summary.txt` — the structured headline numbers.
- `console.log` — full console output of the captured run.
- `exp1_offset_pages.csv` / `exp2_keyset_pages.csv` / `exp3_cursor.csv` — per-page
  (per-batch) latency.
- `exp4_explain.csv` — rows scanned/discarded, actual time, buffers, per query/position.
- `run_metadata.csv` — postgres version, image digest, params, headline numbers.

### Captured run (1,000,000 rows, page size 1000)

Straight from the captured run's `summary.txt`:

```
PostgreSQL 16.14 (Debian 16.14-1.pgdg13+1) on aarch64-unknown-linux-gnu
params: total_rows=1000000 page_size=1000 repeats=3 seed=1234 deep_offset=990000
table: 1000000 rows, ids 1..1000000, size 274 MB, load 3.8s
cache warmed with one full keyset pass before timing

TOTAL EXPORT WALL-CLOCK (median of repeats)
  OFFSET  : median=35.35s  runs(s)=[35.35, 35.43, 35.1]
  keyset  : median=1.50s   runs(s)=[1.53, 1.48, 1.5]
  cursor  : median=1.54s   runs(s)=[1.53, 1.54, 1.8]
  OFFSET/keyset wall ratio = 23.6x

PER-PAGE LATENCY (single recorded run)
  OFFSET  : p99=74.229ms  max=101.552ms  deep(offset>=990000) max=88.713ms
  keyset  : p99=2.972ms   max=8.740ms    deep(last_id>=990000) max=2.430ms
  cursor  : p99=3.440ms   max=6.856ms (per FETCH 1000)
  deep-page OFFSET/keyset max ratio = 37x

EXPLAIN (ANALYZE, BUFFERS)  -- the mechanism
  offset  pos=0       scan=Index Scan  scanned=1000     discarded=0        time=0.158ms  buffers(hit+read)=76
  offset  pos=990000  scan=Index Scan  scanned=991000   discarded=990000   time=121.322ms buffers(hit+read)=69310
  keyset  pos=990000  scan=Index Scan  scanned=1000     discarded=0        time=0.098ms  buffers(hit+read)=78
```

## Laptop numbers, not capacity

These are laptop numbers from a single Postgres container. The point is the
**mechanism and the shape** — flat keyset/cursor vs a per-page latency that climbs
linearly with the offset, and an `EXPLAIN` that shows the deep OFFSET scanning
`offset + page` rows and discarding `offset` of them. Absolute milliseconds drift
run to run with background load; the O(N²)-vs-O(N) contrast does not. Any
non-reproducing shapes are kept under `results/attempts/`.
