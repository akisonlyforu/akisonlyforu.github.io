# rabbitmq priority / head-of-line-blocking harness

This harness runs a digest-pinned RabbitMQ 4.0 broker and measures a non-obvious
failure mode: a notification service pushes everything through **one queue** -
bulk marketing blasts and critical OTP/transactional messages. A big campaign is
enqueued, then an OTP arrives while the queue is draining. How long until a single
consumer actually delivers that OTP?

The load is identical across all three runs: a backlog of `BACKLOG` bulk messages
is fully enqueued, a single consumer with a fixed per-message service time starts
draining it, and once it has drained `WARMUP` messages the `OTP_COUNT` OTPs are
injected (spaced `OTP_GAP_MS` apart, because real OTPs from different users arrive
spread over time, not in one microsecond burst). We embed a publish timestamp in
every message and the consumer records end-to-end latency (`recv_time -
publish_time`) for the OTP-tagged ones. Only the priority setting and the prefetch
change between runs.

Three experiments, all against the real broker via `pika`:

- **A. Single FIFO queue (the problem)** — plain durable queue, no priority. The
  OTP is appended to the tail behind the entire backlog, so its latency is
  `~ backlog / drain_rate` — multiple seconds.
- **B. Priority queue + HIGH prefetch (the fix that doesn't work)** — queue
  declared with `x-max-priority=10`, bulk published at priority 0, OTP at priority
  9, but `basic_qos(prefetch_count=5000)`. The surprise: OTP latency is *still*
  seconds. RabbitMQ priority only reorders messages still in the **READY** state;
  with a large prefetch the consumer has already buffered thousands of bulk
  messages locally as unacked/in-flight, and that client-side buffer is plain
  FIFO. A newly-arrived high-priority OTP is delivered into the **tail** of that
  local buffer, so it waits `~ prefetch / drain_rate`.
- **C. Priority queue + prefetch=1 (the real fix)** — same priority queue,
  `basic_qos(prefetch_count=1)`. The broker re-evaluates priority before every
  single delivery, so the OTP jumps the entire bulk backlog and lands in single
  milliseconds.

The dramatic contrast the whole thing hangs on is the OTP p99: **seconds (A) →
still seconds (B) → milliseconds (C)**, with only the prefetch/priority config
changing. Adding priority (A→B) barely helps; dropping the prefetch (B→C) is what
actually fixes it.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/rabbitmq-priority-hol
docker compose up -d --wait          # AMQP on :6672, management on :16672 (loopback)

python3 -m venv /tmp/prio-hol-venv && source /tmp/prio-hol-venv/bin/activate
pip install -r requirements.txt

python benchmark.py                  # writes results/ and prints the summary
docker compose down -v
```

Priority queues are **core** RabbitMQ (no plugin), so unlike the consistent-hash
harness there is no `enabled_plugins` file. The broker binds to loopback on
non-default host ports (6672 / 16672) to avoid clashing with a local RabbitMQ. The
image is multi-arch and runs natively on arm64.

Env overrides: `AMQP_HOST` (127.0.0.1), `AMQP_PORT` (6672), `MGMT_PORT` (16672),
`RESULTS_DIR` (./results).

## Results

Captured on RabbitMQ 4.0.9. OTP end-to-end latency (delivery to the consumer):

| exp | priority        | prefetch | p50       | p99       | max       | bulk drain |
|-----|-----------------|----------|-----------|-----------|-----------|------------|
| A   | no (FIFO)       | 5000     | 14857.7ms | 15282.7ms | 15290.7ms | 1272.9/s   |
| B   | yes (max-prio 10) | 5000   | 3740.2ms  | 3813.2ms  | 3815.2ms  | 1309.1/s   |
| C   | yes (max-prio 10) | 1      | 2.1ms     | 3.6ms     | 4.1ms     | 823.0/s    |

- A → B: adding priority alone drops OTP p99 from ~15.3s to ~3.8s (it maps to the
  local prefetch buffer, `~5000 / 1300 ≈ 3.8s`, exactly as predicted) — better,
  but still catastrophic for an OTP.
- B → C: dropping prefetch to 1 drops OTP p99 from ~3.8s to ~3.6ms, a ~1000×
  improvement. The cost is lower bulk throughput (823/s vs ~1300/s) because
  prefetch=1 pays a broker round-trip per message.

Artifacts:

- `summary.txt` — the captured console run used in the post (RabbitMQ 4.0.9).
- `otp_latencies_A.csv`, `_B.csv`, `_C.csv` — per-OTP end-to-end latency (ms).
- `run_metadata.csv` — RabbitMQ version, pinned image digest, ports, and every
  parameter (backlog, otp count, service time, warmup, otp gap, prefetch per
  experiment, `x-max-priority`, bulk/otp priorities) plus the headline p50/p99/max
  and drain rate per experiment.
- `attempts/` — non-headline runs kept for honesty. `01-burst-injection/` is the
  same experiment with the 200 OTPs published in one tight loop instead of spaced;
  there C's p99 rose to ~273ms because at prefetch=1 the OTPs serialize behind
  *each other*, which is real but conflates OTP-vs-OTP contention with the
  OTP-vs-backlog effect the post is about. Spacing the injection isolates the
  phenomenon and is more representative of independent OTP arrivals.

These are **laptop numbers, not a capacity statement.** The per-message service
time is a `time.sleep` (the only artificial knob, identical across all three
runs), so the absolute drain rate is a fixed ~1300 msg/s ceiling, not a measure of
RabbitMQ throughput. What's being measured is the *shape*: how OTP latency responds
to priority and prefetch. The mechanism — priority reorders only READY messages,
and a large prefetch parks a FIFO buffer of low-priority work in front of the
consumer — is not version-specific.
