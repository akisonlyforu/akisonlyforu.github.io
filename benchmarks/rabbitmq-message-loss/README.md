# rabbitmq message-loss harness (mirrored vs quorum)

A "does this still happen in 2026" re-test of the classic RabbitMQ failure:
**mirrored (HA) queues could lose messages that were already confirmed to the
publisher**, when an unsynchronised mirror got promoted on failover. RabbitMQ 4.x
removed classic mirrored queues; **quorum queues** (Raft) are the replacement. This
harness measures, against real brokers, that the loss reproduces on the old model
and that quorum queues close it — plus the honest catch: quorum queues trade
availability for that safety.

Three experiments, three real 3-node clusters:

- **A. The problem (RabbitMQ 3.13, classic mirrored queue).** Publish M messages
  with publisher confirms, arrange for the mirrors to be *unsynchronised*, then
  crash the master. The default `ha-promote-on-failure=always` promotes an
  unsynchronised mirror that does not hold the confirmed messages. **Metric:
  confirmed vs recovered → confirmed-messages-lost.**
- **B. The fix (RabbitMQ 4.0, quorum queue).** Same shape: publish M with confirms,
  crash the queue *leader*, let Raft elect a new leader, drain. A quorum-queue
  confirm means a majority already has the message, so losing one node of three
  loses nothing. **Metric: confirmed vs recovered → expect 0 lost.**
- **C. The catch (RabbitMQ 4.0, quorum queue availability).** Measure
  publisher-confirm success as nodes are removed: 3 up, 2 up (still a majority),
  1 up (majority lost). **Metric: confirm success rate per surviving-node count.**
  Quorum queues choose consistency over availability, so at 1/3 confirms block.

**Laptop numbers, not a capacity benchmark.** Everything runs on a single machine
with three broker containers on one Docker bridge. M is 5,000 messages published
one-at-a-time in confirm mode. The point is the *behaviour* (does a confirmed
message survive a node loss), not throughput, latency, or scale.

## The exact failover sequence (this is what makes the loss reproduce)

The classic bug is real but not *random*: a confirmed message is only lost if, at
the instant the master dies, a mirror that is **behind** (unsynchronised) gets
promoted. In a healthy cluster new messages replicate to the mirrors as they are
published, so you have to reproduce the "mirror is behind" state deliberately.
Two real-world ways that happens: a big queue whose sync never finishes, or an
operator who set `ha-sync-mode=manual` to dodge the sync stall. We use the second.

Experiment A does exactly this (see `benchmark.py`, `experiment_a`):

1. 3-node cluster (`rmq0`/`rmq1`/`rmq2`), formed via `classic_config` peer discovery
   + a shared `RABBITMQ_ERLANG_COOKIE`. Verified with `rabbitmqctl cluster_status`.
2. Policy `ha-all` on `^ha\.`:
   `ha-mode=all`, **`ha-sync-mode=manual`**, `ha-promote-on-failure=always`,
   **`ha-promote-on-shutdown=always`**.
3. Declare a durable classic queue `ha.q` on `rmq0` → master lives on `rabbit@rmq0`.
4. `docker kill rmq1 rmq2` — take both mirrors **down**.
5. Publish 5,000 persistent messages to `rmq0` with publisher confirms. The master
   confirms all 5,000; there are no live mirrors to replicate to.
6. `docker start rmq1 rmq2` — the mirrors rejoin, but under **manual sync** they come
   back **unsynchronised**: they hold none of the 5,000 messages and will not catch
   up on their own. Verified via
   `rabbitmqctl list_queues name messages slave_pids synchronised_slave_pids` →
   `synchronised_slave_pids` is **empty** (`[]`).
7. `docker kill rmq0` — crash the master. `ha-promote-on-failure=always` promotes an
   **unsynchronised** mirror.
8. Reconnect to a survivor, drain `ha.q`, count. **Recovered = 0. Lost = 5,000.**

Two config knobs matter and are set on purpose:

- **`ha-sync-mode=manual`** is what keeps the rejoined mirrors behind. With the
  default `automatic` you race a live sync; manual makes the loss deterministic and
  models the real operational cases above.
- **`ha-promote-on-shutdown=always`** matters only because `docker kill` is a hard
  crash (SIGKILL), which is the *failure* path (`ha-promote-on-failure`, default
  `always`). We also set `promote-on-shutdown=always` so the result is identical if
  you swap the `docker kill` for a graceful `docker stop`. Leave the shutdown knob at
  its default (`when-synced`) and a *graceful* master shutdown would instead refuse
  to promote the stale mirror and go **unavailable** — no loss, but downtime. That is
  the broker protecting you; the loss story is specifically the crash path.

Experiment B is the same skeleton on a quorum queue: publish 5,000 with confirms,
`docker kill` the current leader (found via the management API `leader` field), wait
for a new leader, drain. Experiment C declares a quorum queue and measures confirmed
publishes at 3, then 2, then 1 surviving nodes (`docker kill` one node between
stages), each publish guarded by a wall-clock timeout so the no-quorum case reports
a clean 0 instead of hanging.

Failures are driven with `docker kill` / `docker start` (a crash, not a partition);
`cluster_partition_handling=ignore` and `net_ticktime=10` (see `rabbitmq.conf`) keep
a surviving minority node up and make failure detection quick.

## Run it

Docker with Compose v2, plus Python 3.9+. The two clusters reuse the same container
names and host ports, so run them **one at a time**.

```bash
cd benchmarks/rabbitmq-message-loss
python3 -m venv /tmp/rmqloss-venv && source /tmp/rmqloss-venv/bin/activate
pip install -r requirements.txt

# Experiment A — RabbitMQ 3.13, classic mirrored queue
docker compose -f docker-compose.mirrored.yml up -d --wait
python benchmark.py mirrored
docker compose -f docker-compose.mirrored.yml down -v

# Experiments B + C — RabbitMQ 4.0, quorum queue
docker compose -f docker-compose.quorum.yml up -d --wait
python benchmark.py quorum
docker compose -f docker-compose.quorum.yml down -v
```

Both clusters bind to loopback on non-default host ports so they never clash with a
local broker: AMQP `5772`/`5773`/`5774`, management `15772`/`15773`/`15774` (one pair
per node). The images are digest-pinned multi-arch manifests and run natively on
arm64 / Apple Silicon.

Env overrides: `RABBITMQ_HOST` (127.0.0.1), `RMQ0_PORT`/`RMQ1_PORT`/`RMQ2_PORT`,
`RMQ0_MGMT`/`RMQ1_MGMT`/`RMQ2_MGMT`, `MESSAGES` (5000), `AVAIL_ATTEMPTS` (50),
`AVAIL_TIMEOUT` (6s), `RESULTS_DIR` (./results).

## Results

Measured on Apple Silicon (arm64), native containers.

| model                     | RabbitMQ | confirmed | recovered | lost | lost % |
|---------------------------|----------|-----------|-----------|------|--------|
| mirrored (classic HA)     | 3.13.7   | 5000      | 0         | 5000 | 100.0  |
| quorum (Raft)             | 4.0.9    | 5000      | 5000      | 0    | 0.0    |

Quorum-queue availability vs surviving nodes (Experiment C):

| nodes up | majority? | confirms attempted | succeeded | success % |
|----------|-----------|--------------------|-----------|-----------|
| 3        | yes       | 50                 | 50        | 100.0     |
| 2        | yes       | 50                 | 50        | 100.0     |
| 1        | no        | 50                 | 0         | 0.0       |

Artifacts in `results/`:

- `loss.csv` — one row per model: confirmed / recovered / lost / lost_pct. The
  headline contrast.
- `availability.csv` — quorum confirm success at 3 / 2 / 1 surviving nodes.
- `loss_timeline_mirrored.csv`, `loss_timeline_quorum.csv` — queue depth at each
  failover event (declared → publish_done → … → drained), for a timeline chart.
  Mirrored ends at 0 (loss); quorum holds 5000 the whole way through.
- `run_metadata.csv` — both RabbitMQ versions, both pinned image digests, params,
  and headline numbers in one row.
- `summary.txt` — the captured console for all three experiments.
- `attempts/` — non-reproducing runs would live here. It is empty: everything
  reproduced cleanly and repeatably (see `attempts/NOTES.txt`).

Image digests (multi-arch, arm64-native):

- `rabbitmq:3.13-management@sha256:e582c0bc7766f3342496d8485efb5a1df782b5ce3886ad017e2eaae442311f69`
- `rabbitmq:4.0-management@sha256:ad4268113c27d02f08ac1151f9651d6e475c955f81c3a5ad522b79955ce11cf3`

## What reproduced cleanly vs what needed care

The contrast is stark and repeatable: the 3.13 mirrored queue lost **100% of
confirmed messages** on both runs, the 4.0 quorum queue lost **nothing** on both
runs, and the availability cliff at 1/3 nodes is a clean 0. The only thing that took
deliberate setup (not luck) was forcing the mirrors into the unsynchronised state so
the loss is deterministic instead of a race — documented in full above. Nothing was
lumpy; nothing landed in `attempts/`.
