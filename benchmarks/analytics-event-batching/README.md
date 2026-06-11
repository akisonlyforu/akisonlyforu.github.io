# analytics event batching + compression harness

The batching + compression layer is what lets a product-analytics pipeline move
billions of events a day cheaply. A single event barely compresses. But every
event shares the same JSON schema, so once you glue many of them into one batch,
the redundancy *across* events is what the compressor eats, and bytes-per-event
collapses. This harness measures that, plus the codec trade-off and the
at-least-once duplicate tax.

Pure Python, no Docker, no server engine (like `../latency-numbers/`, this is a
CPU/data experiment). Deterministic under a fixed seed.

Four experiments:

- **A. Per-event amortization** — for batch sizes 1..1000, zstd-compress each
  newline-joined batch (level 3) and report bytes-per-event after compression.
  A single event stays fat; a big batch amortizes the shared schema away.
- **B. Compression ratio vs batch size** — same sizes, the headline ratio curve
  (`raw_total / compressed_total`) with stdev over many batches per size.
- **C. Codec + level shootout at batch 500** — none / gzip-6 / zstd-3 / zstd-9 /
  zstd-19: ratio, compress ms/batch, decompress ms/batch, and input MB/s.
  Shows the diminishing returns of cranking the level.
- **D. At-least-once duplicates & dedup** — each send has probability `p` of an
  ambiguous ack (server committed, the 200 got lost) that triggers a client
  retry and a duplicate at the consumer. Measures the duplicate rate for
  `p in [0.005, 0.01, 0.02, 0.05]` and confirms dedup by `event_id` recovers
  exactly N.

The synthetic events resemble real product analytics: `event` (one of 12 names),
`user_id`, `session_id`, an `event_id` for dedup, a monotonic `ts` derived from
the index (never the wall clock), and a small `props` dict (page, referrer,
device, country, ab_variant, duration_ms, position). Mean raw event size is
~293 bytes as compact JSON.

**These are laptop numbers demonstrating the mechanism, not pipeline capacity.**
The absolute MB/s and ms/batch depend entirely on this machine; treat the
*shapes* (bytes/event falling, ratio flattening, level 19 falling off a timing
cliff) as the result, not the specific milliseconds.

## Run it

```bash
pip install -r requirements.txt
python benchmark.py
```

Runs in <60s (~10s here). Env knobs: `RESULTS_DIR` (output dir), `SEED`
(default 42), `N_EVENTS` (default 40000).

## Results (seed=42, N_EVENTS=40000, zstd 0.23.0, Python 3.9.6, macOS arm64)

Mean raw event size: **292.7 bytes** compact JSON.

**A/B — batching amortizes the schema (zstd level 3):**

| batch | comp bytes/event | ratio |
|------:|-----------------:|------:|
| 1     | 227.2 B          | 1.29x |
| 10    | 93.2 B           | 3.15x |
| 100   | 63.6 B           | 4.62x |
| 1000  | 62.2 B           | 4.72x |

A single event compresses ~1.3x; batches of 1000 hit ~4.7x, and bytes/event
falls from 227 B to ~62 B. The curve flattens by ~batch 100 because each event
still carries high-entropy identifiers (`event_id`, `user_id`, `session_id`)
that don't compress — that entropy floor is why the real ratio plateaus at ~4.7x
here rather than 10x. The stdev is tiny (±0.01–0.05x), so the curve is stable.

**C — codec shootout at batch 500:**

| codec   | ratio | compress ms | decompress ms | MB/s   |
|---------|------:|------------:|--------------:|-------:|
| none    | 1.00x | 0.000       | 0.000         | —      |
| gzip-6  | 4.71x | 1.371       | 0.176         | 107    |
| zstd-3  | 4.43x | 0.289       | 0.072         | 509    |
| zstd-9  | 5.05x | 1.203       | 0.064         | 122    |
| zstd-19 | 5.81x | 31.051      | 0.059         | 4.7    |

zstd-3 gets ~95% of gzip's ratio at ~5x the compress throughput. zstd-19 buys
~1.3x more ratio for ~100x the compress time — a bad trade for a hot ingest path.

**D — at-least-once duplicate tax, and dedup recovers N exactly:**

| retry p | delivered | duplicates | duplicate rate | unique after dedup |
|--------:|----------:|-----------:|---------------:|-------------------:|
| 0.005   | 40185     | 185        | 0.46%          | 40000              |
| 0.01    | 40423     | 423        | 1.05%          | 40000              |
| 0.02    | 40813     | 813        | 1.99%          | 40000              |
| 0.05    | 42143     | 2143       | 5.09%          | 40000              |

The duplicate rate tracks `p`, and deduping by `event_id` returns exactly 40000
every time — the point of carrying an idempotency key on each event.

Artifacts: per-experiment CSVs, `summary.txt`, and `run_metadata.csv` in
`results/`.
