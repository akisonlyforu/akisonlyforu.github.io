# redis replica expiration harness

This is the harness behind [The Expired Keys Your Redis Replica Still Counts](../../collections/_posts/2026-07-18-redis-replica-expiry.md).

It runs a digest-pinned Redis 7.4 primary and a read replica and measures what a
replica does with logically-expired keys, because replicas do not run their own
expiration cycle, they hold a key until the primary propagates a `DEL`.

Three experiments:

- **A. Ghost keyspace** — set 1000 keys with a short TTL, let them expire, then compare the replica's `DBSIZE` against how many of those keys still return a value on `GET`.
- **B. Command behavior** — one expired key, queried on the replica: which commands mask it and which still count it, plus the contrast that a read on the *primary* lazily deletes it while a read on the *replica* does not.
- **C. DBSIZE drift** — with the primary's active-expire cycle on, sample `DBSIZE` on both nodes as a 5000-key batch expires, and report the peak replica-minus-primary gap.

These are laptop measurements demonstrating the mechanism, not production numbers.
`DEBUG SET-ACTIVE-EXPIRE 0` is used in A and B to make the timing deterministic
(so the primary's background sweeper isn't racing the measurement); C runs with it
on, the realistic default.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/redis-replica-expiry
docker compose up -d --wait          # primary on :6391, replica on :6392

python3 -m venv /tmp/rep-venv && source /tmp/rep-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

## The pre-3.2 contrast run

Replica read masking landed in Redis 3.2. To see the older, scarier behavior,
where a `GET` on the replica returns the expired value itself, run the same
script against a pinned Redis 3.0:

```bash
docker compose -f docker-compose.legacy.yml up -d --wait     # 3.0, same ports
RESULTS_DIR="$PWD/results/legacy" python benchmark.py | tee results/legacy/summary.txt
docker compose -f docker-compose.legacy.yml down -v
```

On 3.0 the replica returns `owner` for the expired lock and `EXISTS` says `1`;
on 7.4 both are masked. The `docker-compose.legacy.yml` image is amd64-only and
runs under emulation on arm64 hosts (slow, but fine for this).

## Results

- `summary.txt` — the captured console run used in the post (Redis 7.4.9).
- `legacy/summary.txt` — the same script on Redis 3.0.7 (unmasked reads).
- `command_behavior.csv` — the per-command table from experiment B.
- `dbsize_timeline.csv` — primary vs replica `DBSIZE` samples from experiment C.
- `run_metadata.csv` — Redis version, ports, and the headline numbers.

The mechanism (replicas wait for the primary's `DEL`) is not version-specific.
What changed across versions is how much the replica masks on read: nothing
pre-3.2, and `GET`/`EXISTS`/`TTL`/`SCAN` by 7.4. `DBSIZE` counts the ghosts on
every version.
