# mysql semi-sync failover harness

This is the harness behind [The Commits That Didn't Survive the Failover](../../collections/_posts/2026-07-18-the-commits-that-didnt-survive-the-failover.md).

This harness runs a digest-pinned MySQL 8.0 primary and a GTID replica and
measures what a failover actually loses under **asynchronous** vs
**semi-synchronous** replication.

The mechanism: with plain async replication the primary commits and ACKs the
client without waiting for the replica, so a primary crash can lose transactions
the client was already told had succeeded. Semi-synchronous replication
(`AFTER_SYNC`) makes each commit block until a replica acknowledges it has
*received* (relay-logged) the transaction, before the client sees success. That
protects the transmit even when the replica hasn't *applied* the change yet.

Four experiments, all on the same schema (`bench.t`, an auto-increment table):

- **A. async failover loss** — plain async, then `STOP REPLICA IO_THREAD` on the replica to induce a deterministic receipt gap (this models a replica whose receipt has fallen behind, disclosed honestly). Burst N acked inserts into the primary, `docker kill` the primary, promote the replica, count survivors.
- **B. semi-sync failover** — semi-sync ON, then `STOP REPLICA SQL_THREAD` only, so the replica still acks *receipt* into its relay log but hasn't *applied* yet. Burst N acked inserts (each commit blocks on the replica's receipt ack), `docker kill` the primary, `START REPLICA SQL_THREAD` to drain the relay log, count survivors.
- **C. latency cost** — per-commit wall-clock latency for M single-row inserts, once async and once semi-sync, both with the replica applying normally. Reports p50/p95/p99/max.
- **D. semi-sync timeout fallback** (kept under `results/attempts/`) — semi-sync on, `STOP REPLICA IO_THREAD` so nothing can ack; time how long a commit stalls before the primary falls back to async (`rpl_semi_sync_source_timeout`), and confirm `Rpl_semi_sync_source_status` flips OFF.

Each experiment recreates the cluster fresh (`docker compose down -v` + `up`).
The harness drives Docker via subprocess so it can hard-kill the primary
mid-run. Containers are torn down on exit.

These are laptop measurements demonstrating the mechanism, not production
numbers. In particular, primary and replica run on the same host, so the
semi-sync ack round-trip in experiment C is sub-millisecond — the latency cost
is in the noise here and is what widens on a real network.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/mysql-semisync-failover
docker compose up -d --wait          # primary on :3307, replica on :3308 (loopback only)

python3 -m venv /tmp/mysql-venv && source /tmp/mysql-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
# benchmark.py recreates the cluster fresh per experiment and tears everything
# down on exit; the manual `up` above is just a connectivity smoke test.
```

The harness manages its own cluster lifecycle. If you interrupt it, clean up
with `docker compose down -v --remove-orphans`.

## Results

- `summary.txt` — the captured console run used in the post (MySQL 8.0.46).
- `failover_loss.csv` — acked / present-on-replica / lost for experiments A and B.
- `latency_percentiles.csv` — p50/p95/p99/max per mode from experiment C.
- `latency_async.csv`, `latency_semisync.csv` — raw per-commit latencies (ms) so charts can be recomputed.
- `run_metadata.csv` — MySQL version, image digest, params, and headline numbers.
- `attempts/experiment_d_timeout.txt` — the semi-sync timeout stall (kept under `attempts/` because a fixed ~10s wall is a coarse, timing-sensitive result).

The mechanism (async ACKs before the replica has the data; semi-sync `AFTER_SYNC`
waits for receipt) is not host-specific. What a single-laptop run understates is
the *latency* cost of semi-sync, since there is no network distance between the
nodes.
