# Tuning note: why the query has an ORDER BY

The first query shape was a pure aggregate with no sort:

    SELECT COUNT(*), AVG(val) FROM reads_test
    WHERE id BETWEEN %s AND %s AND val >= %s

with SCAN_W = 25000 rows (single-query p50 ~2.85 ms, in the 1-3 ms band).

That shape reproduced the **p99 explosion** cleanly (p99 ~4 ms at the peak vs
~1490 ms at C=512, ~33x), but the **QPS knee was soft** and, more importantly,
the pool did **not** beat direct-512 on throughput:

    Exp A peak      : 909 QPS @ C=8, p99 45.4 ms
    Exp A C=512     : 780 QPS,      p99 1490 ms   (only 1.17x QPS drop from peak)
    Exp C best pool : 749 QPS @ P=8, p99 47.2 ms  (0.96x the QPS of direct-512)

A pure CPU-bound scan+aggregate lets MySQL 8.0 hold throughput near
core-saturation even at 512 connections (it just queues internally), so the only
thing that collapsed was latency, not QPS. The pool's win was entirely in the
tail — it did not "recover throughput," because throughput had barely dropped.

**Fix (what was tuned):** switched to a query that also does a real *filesort*
over the non-indexed `val` column, so each connection allocates a sort buffer and
burns extra CPU:

    SELECT id, val FROM reads_test
    WHERE id BETWEEN %s AND %s AND val >= %s
    ORDER BY val DESC LIMIT 50

with SCAN_W = 20000 and a low `val` threshold so nearly every scanned row feeds
the sort. Single-query p50 stayed ~2.6 ms (still in band). Now the throughput
knee is real: peak 1137 QPS @ C=4 falls to 720 QPS @ C=512 (1.58x drop), and the
pool of 8 backend connections serving 512 clients delivers 957 QPS — **1.33x the
QPS of direct-512 at 41x lower p99** — recovering 84% of peak. That is the
result checked into `../`.

Both shapes are real captured runs; only the sort-heavy one is kept as the
headline because it shows the throughput collapse the pool actually reverses.
