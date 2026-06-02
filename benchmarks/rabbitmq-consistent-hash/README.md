# rabbitmq consistent-hash exchange harness

This harness runs a digest-pinned RabbitMQ 4.0 broker with the
`rabbitmq_consistent_hash_exchange` plugin enabled and measures what the
`x-consistent-hash` exchange actually does: it hashes each message's routing key
onto a ring and routes it to exactly one bound queue, so the same key always lands
on the same queue, and adding a queue reshuffles only a small slice of the keys
instead of almost all of them.

Three experiments, all against the real broker via `pika`:

- **A. Distribution evenness** — 8 equally-weighted queues (binding key `"1"`), publish ~100k messages over ~50k distinct routing keys (`user-<n>`), then read each queue's depth and report the spread against the ideal `total/8`.
- **B. Rebalance cost** — map K=10k distinct keys across 8 queues, add a 9th queue, and count how many keys changed queue. Then, on the *same* keys, compute a naive `hash(key) % 8` vs `hash(key) % 9` in Python and count how many changed. Consistent hashing should remap ~1/9 (~11%); modulo churns almost everything (~87%).
- **C. Affinity** — send several copies of each key on the 8-queue ring and confirm every key coalesces to exactly one queue.

These are laptop measurements demonstrating the mechanism, not a throughput or
capacity benchmark. The queue weight is left at `"1"` (equal weight) as the plugin
intends; the plugin places many ring points per binding, so even at weight 1 the
distribution is close to even and the 8→9 remap lands near the theoretical 1/9.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/rabbitmq-consistent-hash
docker compose up -d --wait          # AMQP on :6672, management on :16672 (loopback)

python3 -m venv /tmp/chx-venv && source /tmp/chx-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

The plugin is enabled by mounting `enabled_plugins`
(`[rabbitmq_management,rabbitmq_consistent_hash_exchange].`) into
`/etc/rabbitmq/`. The broker binds to loopback on non-default host ports (6672 /
16672) to avoid clashing with a local RabbitMQ. The image is multi-arch and runs
natively on arm64.

Env overrides: `RABBITMQ_HOST` (127.0.0.1), `RABBITMQ_PORT` (6672),
`RABBITMQ_MGMT_PORT` (16672), `RESULTS_DIR` (./results).

## Results

- `summary.txt` — the captured console run used in the post (RabbitMQ 4.0.9).
- `distribution.csv` — per-queue message counts, ideal, and deviation from experiment A.
- `rebalance.csv` — consistent-hash vs modulo remap counts and percentages from experiment B.
- `run_metadata.csv` — RabbitMQ version, pinned image digest, ports, and the headline numbers.

The mechanism (a routing key hashes to one queue on a ring; adding a queue only
moves the keys in the new queue's arc) is not version-specific. What you're
measuring here is that the real broker's ring behaves like textbook consistent
hashing: even spread and a ~1/9 remap on scale-out, against a modulo baseline that
remaps nearly everything.
