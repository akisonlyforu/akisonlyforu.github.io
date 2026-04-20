# OpenTelemetry Collector tail-sampling memory harness

A digest-pinned harness that measures, with real captured numbers, how the
OTel Collector `tail_sampling` processor uses memory.

The processor buffers whole traces in a ring buffer (`num_traces`) and holds
each trace until its per-trace `decision_wait` timer fires, then applies the
sampling policies. Two consequences fall out of that design:

- **High `decision_wait` x high rate = large RAM.** More in-flight traces, each
  10s of KB, all resident at once. In a memory-capped container that is an
  OOMKill.
- **When the `num_traces` ring buffer overflows, the OLDEST traces are evicted
  before their decision fires** - which can be exactly the error traces you
  stood up tail sampling to keep.

An important nuance this harness surfaced empirically on collector **v0.156.0**:
a trace entry is *not* released the instant its decision fires. It stays in the
ring buffer (holding its span bytes) until `num_traces` wraps around and evicts
it. So `num_traces` is the real memory knob, and the classic
`num_traces = rate x decision_wait x 2` rule is really a *ring-buffer sizing*
rule: size the buffer to the formula and its memory - and therefore the
collector's RAM - grows linearly with `decision_wait`.

These are laptop measurements demonstrating the mechanism, not capacity
planning numbers. Per-trace size here is a single padded span (~2.5 KB), far
leaner than a real multi-span trace, so absolute MB are small; the *shape*
(linear growth, oldest-first eviction, near-total storage reduction) is the
point.

## What it runs

Image: `otel/opentelemetry-collector-contrib` (contrib distro - it has the
`tailsamplingprocessor`), pinned by digest in `docker-compose.yml`,
`benchmark.py`, and `run_metadata.csv`. All ports bind to loopback only:
OTLP gRPC receiver on `127.0.0.1:4317`, internal Prometheus telemetry on
`127.0.0.1:8888`.

`benchmark.py` generates OTLP spans (configurable rate, error fraction, slow
fraction), scrapes `127.0.0.1:8888/metrics`, and samples
`docker stats` memory for the collector container. The tail-sampling metric
names differ by collector version; the ones used here were discovered live with
`curl -s localhost:8888/metrics | grep tail_sampling` on v0.156.0:

- `otelcol_processor_tail_sampling_sampling_traces_on_memory` - in-memory trace count
- `otelcol_processor_tail_sampling_sampling_trace_dropped_too_early` - evicted-before-decision
- `otelcol_processor_tail_sampling_global_count_traces_sampled{sampled="true"}` - traces kept
- `otelcol_receiver_accepted_spans`, `otelcol_exporter_sent_spans`

## Experiments

1. **Memory scales with `decision_wait`.** Fixed rate (1000/s), ring buffer
   sized to the formula `num_traces = rate x wait x 2`, cheap `debug` sink.
   Vary `decision_wait` in {5, 15, 30, 60}s. Capture peak collector RSS and peak
   in-memory trace count; compare measured vs predicted `num_traces`.
   -> `results/exp1_decision_wait_memory.csv`
2. **`num_traces` cap drops the OLDEST (= error traces).** Inject a batch of
   ERROR traces first, then flood NORMAL traces within one `decision_wait`
   window so the ring overflows before any decision fires. Small ring vs
   adequate ring. Measure error traces kept vs sent and the
   `trace_dropped_too_early` counter.
   -> `results/exp2_num_traces_eviction.csv`
3. **Cost model: storage savings.** Realistic mix (~1% error, ~1% slow),
   generous ring + `decision_wait`, policy keeps ERROR or latency>500ms. Measure
   spans received vs exported.
   -> `results/exp3_storage_savings.csv`
4. **OOMKill attempt.** Tight container `mem_limit`, high `decision_wait` + high
   rate + large ring so retained span bytes blow the cap. Capture
   `docker inspect .State.OOMKilled` and restart count. Reproduces -> CSV in
   `results/`; lumpy -> `results/attempts/`.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/otel-tail-sampling
python3 -m venv /tmp/otel-venv && source /tmp/otel-venv/bin/activate
pip install -r requirements.txt

# full suite (manages its own collector containers per experiment, ~10 min)
python benchmark.py all
python benchmark.py summary        # rebuild results/summary.txt from the CSVs

# or a single experiment
python benchmark.py exp1           # exp1 | exp2 | exp3 | exp4
```

Standalone generator against a manually-started collector:

```bash
docker compose up -d --wait
RATE=1500 ERROR_RATIO=0.05 SLOW_RATIO=0.02 DURATION_S=20 python benchmark.py gen
docker compose down -v
```

The experiment orchestrator (`benchmark.py all`) does **not** use the compose
file - it starts each collector with `docker run` so it can vary
`decision_wait` / `num_traces` / `mem_limit` per experiment. It tears each one
down when the experiment ends.

## Results (this run, collector v0.156.0, arm64 laptop, ~2.5 KB/trace)

**Exp1 - RAM grows with `decision_wait`** (rate 1000/s, ring = rate x wait x 2):

| decision_wait | num_traces (predicted) | peak in-memory traces | peak RSS |
|---:|---:|---:|---:|
| 5s  | 10000  | 10000 | 133.5 MB |
| 15s | 30000  | 30000 | 220.0 MB |
| 30s | 60000  | 30000 | 320.9 MB |
| 60s | 120000 | 85000 | 576.0 MB |

Peak RSS climbs monotonically with `decision_wait` (133 -> 576 MB). At the two
largest configs the generator could not sustain 1000/s once the collector was
under memory pressure (gRPC backpressure), so the measured in-memory count falls
short of the formula cap - the ring never fully fills. The mechanism (bigger
wait -> more resident traces -> more RAM) is unambiguous; the absolute curve is
slightly sub-linear because of that backpressure, which is itself an honest
artifact of a single-box test.

**Exp2 - undersized ring evicts your error traces:**

| config | num_traces | error_sent | error_kept | error_lost | dropped_too_early |
|---|---:|---:|---:|---:|---:|
| small   | 2000   | 500 | 0   | **100%** | 6500 |
| adequate | 200000 | 500 | 500 | 0%       | 0    |

Inject 500 errors first, flood 8000 normals within one 30s window: the small
ring evicts all 500 errors before their decision fires (100% loss); the adequate
ring keeps every one.

**Exp3 - storage savings:** 30000 spans received, 600 exported, **98.0% reduction**
(policy: keep ERROR or latency>500ms, mix 1% error + 1% slow).

**Exp4 - OOMKill:** `mem_limit=400MB`, `decision_wait=90s`, `rate=3000`,
`num_traces=400000`, ~6 KB/trace. RSS climbed to ~357MB then the container was
killed and restarted (`RestartCount=1`) at t~=67s. The post-restart
`.State.OOMKilled` flag resets to false, so `RestartCount` under the cap is the
durable evidence of the kill.

## Files

- `summary.txt` - human-readable headline numbers.
- `exp1_decision_wait_memory.csv` - decision_wait vs peak RSS / in-memory count.
- `exp2_num_traces_eviction.csv` - error traces kept vs lost, small vs adequate ring.
- `exp3_storage_savings.csv` - spans received vs exported, reduction %.
- `exp4_oomkill.csv` (or `attempts/`) - OOMKill outcome.
- `run_metadata.csv` - collector version, image digest, params, host info.

The mechanism (ring-buffered traces, decision on a timer, oldest-first
eviction) is not version-specific. What can differ across collector versions is
the exact metric names and whether a decided trace's memory is released eagerly
or only on ring eviction - which is why the metric names are discovered live
and why Exp1 sizes the ring to the formula rather than assuming eager release.
