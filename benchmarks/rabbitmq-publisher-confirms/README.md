# rabbitmq publisher-confirm in-flight window harness

This harness measures how a publisher's **confirm window** — the maximum number of
unconfirmed ("in-flight") messages it will let pile up before it blocks waiting for
the broker to ack them — affects sustained publish throughput.

It runs a digest-pinned RabbitMQ 4.0 broker and drives it with the official
[RabbitMQ PerfTest](https://github.com/rabbitmq/rabbitmq-perf-test) load generator
(also digest-pinned), run as a container on the same docker network so it reaches
the broker by service name. All traffic goes to one **quorum queue** with
**persistent** messages, which is the realistic durable setup where confirms
actually cost something (every confirm waits on a Raft-replicated disk write).

Two experiments:

- **A. In-flight sweet-spot sweep** — fixed producers/consumers, sweep the confirm
  window `-c` across `1, 2, 4, 8, 16, 32, 64, 128, 256, 1000` and record the average
  send and receive rate for each. This traces the throughput-vs-window curve and
  finds where it peaks.
- **B. Confirms on vs off** — same setup, compare fire-and-forget publishing (no
  confirms at all) against a small confirm window (`-c 2`), to see which sustains
  higher publish throughput and what it does to the consumer backlog.

Each data point is run 3 times against a **fresh queue** (the previous run's queue
is deleted first, so a growing on-disk backlog doesn't bias later runs). We report
the median with the min/max spread, because at laptop scale these numbers are noisy.

These are laptop measurements demonstrating the mechanism, **not** production
capacity numbers. A single-node broker on a developer machine flow-controls and
saturates at completely different points than a real multi-node cluster, and the
absolute msg/s here are meaningless as a capacity figure — only the *shape* of the
curve and the *relative* comparisons are the point.

## A note on PerfTest `-r 0`

Older PerfTest docs (and the common shorthand) treat `-r 0` as "unlimited rate."
On the pinned PerfTest **2.25.0** image that is **wrong**: `-r 0` means a literal
rate of *zero* — the producers publish nothing and every rate reads `0 msg/s`.
Unlimited rate is achieved by **omitting `-r` entirely**, which is what this harness
does. If you copy the flags into your own script, don't add `-r 0`.

## Run it

Docker with Compose v2, plus Python 3.9+ (standard library only — nothing to
`pip install`).

```bash
cd benchmarks/rabbitmq-publisher-confirms
docker compose up -d --wait          # broker: AMQP :6672, management :16672 (loopback)

python3 benchmark.py | tee results/summary.txt

docker compose down -v               # tear down; -v drops the broker's volume
```

The load generator is pulled and run by `benchmark.py` itself (as a throwaway
`--rm` container on the `rmq-confirms-net` network), so there's nothing else to
start. A full run is ~10-12 minutes (36 PerfTest runs of 15s each plus settle).

Tunables via environment variables (defaults in parentheses): `PRODUCERS` (30),
`CONSUMERS` (4), `MSG_SIZE` (1000 bytes), `DURATION` (15s), `REPEATS` (3),
`RESULTS_DIR` (`./results`).

## Results

- `summary.txt` — the captured console run (the sweep table + the on/off comparison).
- `inflight_sweep.csv` — one row per confirm window: `confirm_window, producers,
  consumers, send_rate_msgs_s, recv_rate_msgs_s` (median of 3 runs). Experiment A.
- `inflight_sweep_raw.csv` — every individual repeat behind the medians, for
  inspecting the run-to-run variance.
- `confirms_onoff.csv` — `mode, send_rate_msgs_s, recv_rate_msgs_s` for
  fire-and-forget vs `-c 2`. Experiment B.
- `run_metadata.csv` — RabbitMQ version, both image digests, the `-x/-y/-s/-z`
  parameters, queue type, and where the sweep peaked.

The mechanism (a bounded confirm window trades per-message latency for backpressure,
and an unbounded one lets a publisher outrun the broker into a backlog) is not
version-specific. The exact window where throughput peaks, and whether fire-and-forget
"wins," depends entirely on the broker, the queue type, and the hardware — see
`summary.txt` for what this machine actually did.
