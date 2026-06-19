# OpenTelemetry Python SDK instrumentation-overhead harness

A digest-pinned harness that measures, with real captured numbers, the
**app-side** cost of OpenTelemetry tracing on a hot path. This is about the SDK
running inside your process -- span creation, attribute recording, the span
processor, head sampling -- **not** the collector. (For the collector side, see
the sibling [`otel-tail-sampling`](../otel-tail-sampling/) harness.)

Every arm does the *same* fixed unit of CPU work (a sum of 64 float multiplies)
so the only thing that varies is the tracing. These are laptop measurements
demonstrating the mechanism, not capacity-planning numbers; the absolute ns/op
depend on the box and on CPython, but the *shape* (attributes cost extra, Batch
takes export off the hot path, sampling drops cost) is the point.

## Experiments

1. **Span-creation cost vs attribute count.** A tight loop over the fixed work.
   Arms: (a) no tracing, (b) start+end a span with 0 attributes, (c) 5
   attributes, (d) 20 attributes. `TracerProvider` with **AlwaysOn** sampler and
   a `SimpleSpanProcessor` writing to an in-process **no-export** exporter
   (`NoOpSpanExporter`, returns `SUCCESS` instantly) so we isolate SDK cost, not
   network. Reports ns/op, ops/sec, and overhead-vs-baseline per arm.
   -> `results/exp1_span_creation.csv`
2. **`SimpleSpanProcessor` vs `BatchSpanProcessor` request latency (the money
   chart).** Model a request handler: each request does the fixed work + one
   span with 5 attributes, then the span is exported. The backend/collector
   round-trip is modeled by a custom `SpanExporter` that `time.sleep(RTT)` before
   returning `SUCCESS` (`EXPORT_RTT_MS`, default 5). This RTT is **modeled, not a
   live network hop** -- deliberately, so the Simple-vs-Batch contrast is clean
   and reproducible. Drive N requests single-threaded (default 5000), measure
   per-request wall-clock latency, report p50/p90/p99/max and throughput.
   -> `results/exp2_processor_latency.csv` (+ raw sample in
   `results/exp2_latencies_raw.csv`)
3. **Head-sampling ratio vs throughput.** Instrumented hot path with ~10
   attributes and a `BatchSpanProcessor` exporting over **OTLP gRPC to a live,
   digest-pinned collector** on `127.0.0.1:4317`. Vary the head sampler via
   `TraceIdRatioBased` in {1.0, 0.1, 0.01, 0.0}. Unsampled spans are
   non-recording, so `set_attribute` is a no-op and nothing is queued/exported;
   throughput rises as the ratio falls. `spans_exported` is read from the
   collector's `otelcol_receiver_accepted_spans` counter.
   -> `results/exp3_sampling_throughput.csv`

## What it runs

Only Exp3 needs a collector. Image:
`otel/opentelemetry-collector-contrib`, pinned by digest in `docker-compose.yml`,
`benchmark.py`, and `run_metadata.csv`. All ports bind to loopback only: OTLP
gRPC on `127.0.0.1:4317`, internal Prometheus telemetry on `127.0.0.1:8888`. The
collector pipeline is a minimal `otlp` receiver -> `debug` (nop-drain) exporter.
`benchmark.py all` brings the collector up for Exp3 and tears it down
(`docker compose down -v`) when Exp3 ends. Exp1 and Exp2 use no containers at
all.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/otel-sdk-overhead
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# full suite (~30s; brings the collector up/down itself for Exp3)
python benchmark.py all
python benchmark.py summary        # rebuild results/summary.txt from the CSVs

# or a single experiment
python benchmark.py exp1           # exp1 | exp2 | exp3
```

Env knobs: `RESULTS_DIR`, `EXP1_ITERS` (300000), `EXP2_REQUESTS` (5000),
`EXPORT_RTT_MS` (5), `EXP3_ITERS` (300000), `OTLP_ENDPOINT`, `METRICS_URL`.

## Results (this run, otel-sdk 1.29.0, Python 3.9.6, arm64 laptop, 10 cpus)

**Exp1 - span creation is cheap vs the network but not free; attributes add up**
(300k iterations/arm, AlwaysOn, in-process no-export processor):

| arm | ns/op | ops/sec | overhead vs baseline |
|---|---:|---:|---:|
| no tracing | 1211 | 825,466 | 0 |
| span, 0 attrs | 9632 | 103,824 | +8420 ns |
| span, 5 attrs | 13651 | 73,257 | +12439 ns |
| span, 20 attrs | 24885 | 40,185 | +23674 ns |

Starting and ending an empty span costs ~8.4 µs here; each attribute adds a
roughly fixed increment (~800 ns/attr on top of the empty span). Absolute
numbers are CPython-on-a-laptop, but the trend -- span start/end is a fixed base
cost and attributes are linear on top -- is clean and monotonic.

**Exp2 - Simple vs Batch, per-request latency (modeled RTT = 5 ms):**

| arm | p50 | p90 | p99 | max | throughput |
|---|---:|---:|---:|---:|---:|
| SimpleSpanProcessor | 6.37 ms | 6.50 ms | 6.61 ms | 10.08 ms | 160 rps |
| BatchSpanProcessor | 0.016 ms | 0.029 ms | 0.046 ms | 4.10 ms | 49,476 rps |

This is the headline. `SimpleSpanProcessor` exports **inline on `span.end()`**,
so the 5 ms round-trip lands on every single request -- p99 ≈ RTT + work, and
single-threaded throughput collapses to ~160 rps. `BatchSpanProcessor` enqueues
and exports on a background thread, so the RTT leaves the hot path entirely: p99
= 0.046 ms, ~49k rps. Two orders of magnitude, from one processor choice.
(Simple's p50 of 6.4 ms sits a touch above the 5 ms RTT because `time.sleep`
granularity on this host rounds up; the point -- RTT-per-request -- is
unaffected.)

**Exp3 - head-sampling ratio vs hot-path throughput** (BatchSpanProcessor ->
live collector over OTLP gRPC, 300k iterations/ratio):

| sample ratio | ops/sec | duration | spans_exported |
|---:|---:|---:|---:|
| 1.0 | 31,932 | 9.40 s | 138,766 |
| 0.1 | 122,831 | 2.44 s | 29,748 |
| 0.01 | 222,024 | 1.35 s | 3,022 |
| 0.0 | 234,866 | 1.28 s | 0 |

Throughput rises ~7x from full sampling to none. When a trace loses the head
sampling coin flip, the SDK hands back a **non-recording** span: `set_attribute`
is a no-op and nothing is serialized or queued. `spans_exported` tracks the
ratio (≈30k at 0.1, ≈3k at 0.01, 0 at 0.0). At ratio 1.0 the exported count
(138,766) is *less* than 300k because the `BatchSpanProcessor` queue overflows
under this synthetic burst and drops the excess (the SDK logs "Queue is full,
likely spans will be dropped") -- an honest artifact of driving 300k spans as
fast as possible single-threaded; it does not affect the throughput measurement,
which is wall-clock over the fixed iteration count.

## Honesty note

All three experiments reproduced cleanly and monotonically on this run, so
`results/attempts/` is empty. If a future run comes out lumpy (e.g. Batch p99
not flat, or a non-monotonic sampling curve), the harness convention is to keep
that output under `results/attempts/` and say so. The one modeled quantity is
Exp2's export RTT (a `time.sleep`, documented above); Exp1 and Exp3 use the real
SDK code paths end to end, and Exp3 exports to a real running collector.

## Files

- `summary.txt` - human-readable headline numbers.
- `exp1_span_creation.csv` - arm, iterations, ns/op, ops/sec, overhead vs baseline.
- `exp2_processor_latency.csv` - arm, requests, rtt_ms, p50/p90/p99/max, throughput.
- `exp2_latencies_raw.csv` - per-request latency sample (both arms).
- `exp3_sampling_throughput.csv` - sample_ratio, iterations, duration, ops/sec, spans_exported.
- `run_metadata.csv` - python + otel-sdk versions, collector image digest, host info, all params.
