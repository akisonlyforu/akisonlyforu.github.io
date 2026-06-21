# mysql connection-pool vs connection-per-client harness

This harness runs a single digest-pinned MySQL 8.0 instance and measures what
happens to read throughput and tail latency as you scale the number of
*concurrent connections* two different ways: give every client its own
dedicated connection, versus multiplex thousands of clients over a small,
bounded **connection pool**.

The mechanism: a read query that makes the server do real per-row CPU work (a
bounded PK-range scan plus a filesort over a **non-indexed** column) saturates
the box's cores at a low concurrency — roughly the core count. Past that point
every extra concurrent connection is pure overhead: context switching, internal
lock/mutex contention, per-connection sort buffers competing for cache and
memory bandwidth. Throughput *declines* and tail latency *explodes*. Scaling
reads is therefore not "let every client open its own connection." A stateless
pool of a handful of backend connections, with thousands of client callers
borrowing and returning them, beats connection-per-client on both throughput
and tail latency.

Three experiments, all read-only, all against the **same** single instance:

- **A. the curve (connection-per-client)** — for offered concurrency `C` in
  `[1,2,4,8,16,32,64,128,256,512]`, spawn `C` worker threads, each owning its
  own dedicated MySQL connection, all hammering the read query for an 8s
  measured window (after a 2s warmup). Record achieved QPS, p50, p99, and the
  server's `Threads_connected` / `Threads_running` sampled mid-run. The curve
  rises, peaks near the core count, then declines while p99 climbs.
- **B. the collapse** — the `C=512` row of A, called out as the pathological
  point (no separate run).
- **C. the pool fix** — keep the same 512 offered client threads, but route
  every query through a bounded pool of `P` persistent connections for
  `P in [8,16,32,64]`. The pool is a `queue.Queue` of `P` connections used as a
  semaphore: each client borrows a connection, runs the query, returns it. The
  client-observed latency includes the wait to borrow. A pool near the core
  count beats direct-512 on both QPS and p99.

The read query is:

```sql
SELECT id, val FROM reads_test
WHERE id BETWEEN %s AND %s AND val >= %s
ORDER BY val DESC LIMIT 50
```

`reads_test` has 200,000 rows: indexed PK `id INT`, a **non-indexed** numeric
column `val` (seeded deterministically as `val = id*2654435761 % 100000`), and a
`payload VARCHAR(255)`. The `id BETWEEN` bounds the scan to a moving 20,000-row
window (so a single query is ~2.6 ms — thousands of QPS, and the knee is
visible), the low `val` threshold makes nearly every scanned row feed the
`ORDER BY val` filesort (real CPU + a sort buffer per connection, `val` is not
indexed), and the moving window means no two calls are identical, so nothing
collapses to a cached constant. The buffer pool is warmed before measuring.

The scan width and query shape were tuned once: a pure `COUNT(*),AVG(val)`
aggregate reproduced the p99 explosion but not a real throughput knee (MySQL
held throughput near saturation and the pool didn't beat direct-512 on QPS).
Adding the `ORDER BY val` filesort made the throughput collapse real. That is
documented with numbers in [`results/attempts/aggregate_no_sort_note.md`](results/attempts/aggregate_no_sort_note.md).

These are laptop measurements demonstrating the mechanism, not capacity
planning. The absolute QPS depends entirely on the query cost, the row count,
and the four cores this run was pinned to; what transfers is the *shape* — a
throughput peak near the core count, a tail that explodes with connection count,
and a small pool that reverses both.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/mysql-connection-pool

python3 -m venv /tmp/mcp-venv && source /tmp/mcp-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
```

`benchmark.py` brings the container up fresh (digest-pinned, pinned to 4 CPUs),
seeds the table, warms the buffer pool, runs all three experiments, writes the
CSVs, and tears the container down on exit. If you interrupt it, clean up with
`docker compose down -v --remove-orphans`.

Tunables via env: `SCAN_W` (rows scanned per query, default 20000), `WINDOW_S`
(8), `WARMUP_S` (2), `CONC_LEVELS`, `POOL_SIZES`, `CLIENT_THREADS` (512).

## Results (MySQL 8.0.46, aarch64, pinned to 4 CPUs, 200k rows, ~2.6 ms/query)

**Experiment A — connection-per-client curve:**

| C (connections) | QPS | p50 ms | p99 ms | Threads_connected |
|---|---|---|---|---|
| 1 | 340 | 2.86 | 5.20 | 2 |
| 2 | 720 | 2.65 | 4.90 | 3 |
| **4** | **1137** | **3.23** | **8.13** | 5 |
| 8 | 1087 | 5.12 | 40.65 | 9 |
| 16 | 985 | 13.08 | 58.31 | 17 |
| 32 | 926 | 28.29 | 93.24 | 33 |
| 64 | 875 | 74.29 | 184.07 | 65 |
| 128 | 842 | 147.16 | 349.61 | 130 |
| 256 | 796 | 294.70 | 772.11 | 258 |
| **512** | **720** | **600.32** | **1714.63** | 514 |

Throughput peaks at **1137 QPS @ C=4** (the box has 4 cores) with a **8.1 ms**
p99. By **C=512** it has fallen to **720 QPS** — 1.58x less throughput — while
p99 has climbed to **1715 ms**, roughly **211x** the peak's tail. The peak is at
the core count; everything past it is contention.

**Experiment C — 512 client threads over a bounded pool:**

| Pool size | Client threads | QPS | p50 ms | p99 ms |
|---|---|---|---|---|
| **8** | 512 | **957** | 5.57 | **42.06** |
| 16 | 512 | 802 | 14.24 | 64.31 |
| 32 | 512 | 744 | 34.11 | 103.44 |
| 64 | 512 | 712 | 90.62 | 217.66 |

The same 512 callers, funnelled through **8 backend connections**, do **957 QPS
at a 42 ms p99** — **1.33x the throughput of direct-512 at ~41x lower p99**, and
84% of the C=4 peak. Note the pool can be oversized too: `P=64` (712 QPS, 218 ms
p99) is essentially the `C=64` row of the direct curve, because 64 concurrent
backend connections is 64 concurrent backend connections however they got there.
The win comes from keeping the backend connection count small, near the core
count — not from how many clients are offered.

### Files

- `summary.txt` — the captured console run used above.
- `expA_curve.csv` — `concurrency,qps,p50_ms,p99_ms,threads_connected` for Exp A.
- `expC_pool.csv` — `pool_size,client_threads,qps,p50_ms,p99_ms` for Exp C.
- `run_metadata.csv` — MySQL version, image digest, arch, core count, table
  rows, query shape, calibration latency, window/warmup, levels, and headline
  numbers.
- `attempts/aggregate_no_sort_note.md` — the aggregate-only query variant that
  showed the p99 explosion but not a throughput knee, and what was tuned.

The mechanism (cores saturate near the core count; extra connections add
contention, not throughput; a small pool reverses it) is not host-specific. What
a single-laptop run understates is the absolute scale — on a bigger box the peak
moves right and up, but the knee and the tail explosion are the same shape.
