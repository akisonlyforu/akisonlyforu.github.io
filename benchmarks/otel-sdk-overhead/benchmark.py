#!/usr/bin/env python3
"""
OpenTelemetry Python SDK instrumentation-overhead harness.

Measures the APP-SIDE cost of tracing on a hot path -- not the collector. Three
questions, real captured numbers:

  Exp1  How many ns does creating one span cost, and how does that scale with
        the number of attributes you attach? (0 / 5 / 20 attrs vs no tracing.)
        Isolated from the network with an in-process no-export processor.

  Exp2  Does the span processor choice show up in per-REQUEST latency? Model a
        request handler that does fixed work + one span, then export it across a
        modeled 5ms backend round-trip. SimpleSpanProcessor exports inline (the
        RTT lands on every request); BatchSpanProcessor exports off-thread (the
        RTT leaves the hot path). p50/p90/p99/max per arm. THE money chart.

  Exp3  Head-sampling ratio vs hot-path throughput. Instrumented path with ~10
        attributes and a BatchSpanProcessor to a LIVE collector over OTLP gRPC.
        Vary TraceIdRatioBased in {1.0, 0.1, 0.01, 0.0}. Unsampled spans are
        non-recording -- no attribute recording, no export -- so throughput
        rises as the ratio falls.

Usage:
    python benchmark.py all            # run exp1-3, write results/*.csv + summary
    python benchmark.py exp1|exp2|exp3
    python benchmark.py summary        # rebuild results/summary.txt from CSVs

Env overrides:
    RESULTS_DIR        default ./results
    OTLP_ENDPOINT      default 127.0.0.1:4317   (Exp3)
    METRICS_URL        default http://127.0.0.1:8888/metrics  (Exp3)
    EXP1_ITERS         default 300000            span-creation iterations/arm
    EXP2_REQUESTS      default 5000              modeled requests/arm
    EXPORT_RTT_MS      default 5                 modeled backend round-trip (Exp2)
    EXP3_ITERS         default 300000            hot-path iterations/ratio (Exp3)

Laptop measurements demonstrating the mechanism, not capacity planning.
"""

import os
import sys
import csv
import math
import time
import subprocess
import urllib.request

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# Digest-pinned contrib image (same one the tail-sampling harness uses).
IMAGE = ("otel/opentelemetry-collector-contrib@sha256:"
         "125bdbeb7590cc1952c5b3430ecf14063568980c2c93d5b38676cc0446ed8108")
HERE = os.path.dirname(os.path.abspath(__file__))

OTLP_ENDPOINT = os.environ.get("OTLP_ENDPOINT", "127.0.0.1:4317")
METRICS_URL = os.environ.get("METRICS_URL", "http://127.0.0.1:8888/metrics")
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))

EXP1_ITERS = int(os.environ.get("EXP1_ITERS", "300000"))
EXP2_REQUESTS = int(os.environ.get("EXP2_REQUESTS", "5000"))
EXPORT_RTT_MS = float(os.environ.get("EXPORT_RTT_MS", "5"))
EXP3_ITERS = int(os.environ.get("EXP3_ITERS", "300000"))

SERVICE = "sdk-overhead-bench"


# ---------------------------------------------------------------------------
# fixed unit of CPU work -- identical across every arm so tracing overhead is
# the ONLY thing that differs. A small float reduction over a fixed array.
# ---------------------------------------------------------------------------
_WORK = [float(i) for i in range(64)]


def do_work():
    s = 0.0
    for x in _WORK:
        s += x * 1.000001
    return s


# ---------------------------------------------------------------------------
# exporters used to isolate cost
# ---------------------------------------------------------------------------
class NoOpSpanExporter(SpanExporter):
    """Drops spans instantly. Exercises the full SDK span lifecycle + processor
    dispatch with ZERO network/serialization cost, so Exp1 measures SDK cost."""

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class SlowSpanExporter(SpanExporter):
    """Models a backend/collector round-trip with a fixed sleep before returning
    SUCCESS. Makes the Simple-vs-Batch contrast in Exp2 clean and reproducible.
    This RTT is MODELED, not a live network hop."""

    def __init__(self, rtt_s):
        self.rtt_s = rtt_s

    def export(self, spans):
        time.sleep(self.rtt_s)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


# ---------------------------------------------------------------------------
# attribute sets
# ---------------------------------------------------------------------------
def attrs(n):
    """Return a dict of n plausible span attributes (mixed str/int/bool)."""
    base = {
        "http.method": "GET",
        "http.route": "/api/v1/thing",
        "http.status_code": 200,
        "http.scheme": "https",
        "net.host.name": "svc.internal",
        "net.peer.port": 8443,
        "user.tier": "premium",
        "cache.hit": True,
        "db.system": "postgresql",
        "db.rows_affected": 3,
        "request.id": "a1b2c3d4e5f6",
        "tenant.id": "tenant-42",
        "region": "us-east-1",
        "az": "us-east-1b",
        "feature.flag.newpath": False,
        "retry.count": 0,
        "queue.depth": 7,
        "span.kind.hint": "server",
        "app.version": "2.14.0",
        "trace.sampled.hint": True,
    }
    items = list(base.items())[:n]
    return dict(items)


# ---------------------------------------------------------------------------
# percentiles (no numpy dependency)
# ---------------------------------------------------------------------------
def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


# ---------------------------------------------------------------------------
# csv writer (mirrors the tail-sampling harness convention)
# ---------------------------------------------------------------------------
def write_csv(name, header, rows, subdir=""):
    d = os.path.join(RESULTS_DIR, subdir) if subdir else RESULTS_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    print(f"  -> wrote {path}")
    return path


# ---------------------------------------------------------------------------
# docker + metrics helpers (Exp3 only)
# ---------------------------------------------------------------------------
def sh(args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def compose(*args, check=True):
    return sh(["docker", "compose", "-f", os.path.join(HERE, "docker-compose.yml"),
               *args], check=check)


def scrape_raw():
    with urllib.request.urlopen(METRICS_URL, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def metric_sum(text, name):
    total = 0.0
    found = False
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        rest = line[len(name):]
        if rest and rest[0] not in (" ", "{"):
            continue
        try:
            total += float(line.rsplit(" ", 1)[1])
            found = True
        except (ValueError, IndexError):
            pass
    return total if found else 0.0


def m(name):
    try:
        return metric_sum(scrape_raw(), name)
    except Exception:
        return 0.0


RECV_SPANS = "otelcol_receiver_accepted_spans"


def collector_up():
    compose("up", "-d", "--wait")
    for _ in range(60):
        try:
            scrape_raw()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("collector telemetry endpoint did not become ready")


def collector_down():
    compose("down", "-v", check=False)


# ---------------------------------------------------------------------------
# Experiment 1: span-creation cost vs attribute count
# ---------------------------------------------------------------------------
def exp1():
    print("\n=== Exp1: span-creation cost vs attribute count ===")
    iters = EXP1_ITERS
    prov = TracerProvider(
        sampler=ALWAYS_ON,
        resource=Resource.create({"service.name": SERVICE}))
    prov.add_span_processor(SimpleSpanProcessor(NoOpSpanExporter()))
    tracer = prov.get_tracer("bench")

    def run_baseline():
        for _ in range(iters):
            do_work()

    def run_span(n):
        a = attrs(n)
        for _ in range(iters):
            do_work()
            span = tracer.start_span("handle")
            for k, v in a.items():
                span.set_attribute(k, v)
            span.end()

    arms = [
        ("baseline_no_tracing", lambda: run_baseline()),
        ("span_0_attrs", lambda: run_span(0)),
        ("span_5_attrs", lambda: run_span(5)),
        ("span_20_attrs", lambda: run_span(20)),
    ]

    # warm up the interpreter / JIT-free CPython caches on the fixed work
    for _ in range(20000):
        do_work()

    measured = {}
    for name, fn in arms:
        t0 = time.perf_counter_ns()
        fn()
        dt = time.perf_counter_ns() - t0
        ns_per_op = dt / iters
        ops_per_sec = 1e9 / ns_per_op
        measured[name] = ns_per_op
        print(f"[exp1] {name:22s} {ns_per_op:9.1f} ns/op  {ops_per_sec:12,.0f} ops/s")

    base = measured["baseline_no_tracing"]
    rows = []
    for name, _ in arms:
        ns_per_op = measured[name]
        overhead = round(ns_per_op - base, 1)
        rows.append([name, iters, round(ns_per_op, 1),
                     round(1e9 / ns_per_op, 0), overhead])
    prov.shutdown()
    write_csv("exp1_span_creation.csv",
              ["arm", "iterations", "ns_per_op", "ops_per_sec",
               "overhead_ns_vs_baseline"], rows)
    return rows


# ---------------------------------------------------------------------------
# Experiment 2: SimpleSpanProcessor vs BatchSpanProcessor request latency
# ---------------------------------------------------------------------------
def _run_requests(processor_kind, n, rtt_s):
    """Drive n single-threaded 'requests'; each does fixed work + one 5-attr
    span that then gets exported across the modeled RTT. Return per-request
    latencies in ms."""
    prov = TracerProvider(
        sampler=ALWAYS_ON,
        resource=Resource.create({"service.name": SERVICE}))
    exporter = SlowSpanExporter(rtt_s)
    if processor_kind == "simple":
        prov.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        prov.add_span_processor(BatchSpanProcessor(exporter))  # default settings
    tracer = prov.get_tracer("bench")
    a = attrs(5)

    latencies = []
    wall0 = time.perf_counter()
    for _ in range(n):
        t0 = time.perf_counter_ns()
        do_work()
        span = tracer.start_span("request")
        for k, v in a.items():
            span.set_attribute(k, v)
        span.end()                          # simple: blocks on RTT; batch: enqueue
        latencies.append((time.perf_counter_ns() - t0) / 1e6)  # ms
    wall = time.perf_counter() - wall0
    prov.shutdown()                          # drain batch queue (not timed)
    return latencies, wall


def exp2():
    print("\n=== Exp2: Simple vs Batch span processor -- per-request latency ===")
    n = EXP2_REQUESTS
    rtt_s = EXPORT_RTT_MS / 1000.0
    rows = []
    raw = []
    for kind in ("simple", "batch"):
        lat, wall = _run_requests(kind, n, rtt_s)
        s = sorted(lat)
        p50, p90, p99 = pct(s, 0.50), pct(s, 0.90), pct(s, 0.99)
        mx = s[-1]
        rps = n / wall
        arm = "SimpleSpanProcessor" if kind == "simple" else "BatchSpanProcessor"
        print(f"[exp2] {arm:20s} p50={p50:7.3f} p90={p90:7.3f} "
              f"p99={p99:7.3f} max={mx:8.3f} ms  {rps:10,.0f} rps")
        rows.append([arm, n, EXPORT_RTT_MS, round(p50, 3), round(p90, 3),
                     round(p99, 3), round(mx, 3), round(rps, 1)])
        for i, v in enumerate(lat):
            raw.append([arm, i, round(v, 4)])
    write_csv("exp2_processor_latency.csv",
              ["arm", "requests", "rtt_ms", "p50_ms", "p90_ms", "p99_ms",
               "max_ms", "throughput_rps"], rows)
    write_csv("exp2_latencies_raw.csv",
              ["arm", "request_idx", "latency_ms"], raw)
    return rows


# ---------------------------------------------------------------------------
# Experiment 3: head-sampling ratio vs throughput (live collector, OTLP gRPC)
# ---------------------------------------------------------------------------
def exp3():
    print("\n=== Exp3: head-sampling ratio vs hot-path throughput ===")
    iters = EXP3_ITERS
    ratios = [1.0, 0.1, 0.01, 0.0]
    a = attrs(10)
    rows = []
    collector_up()
    try:
        for ratio in ratios:
            recv_before = m(RECV_SPANS)
            prov = TracerProvider(
                sampler=TraceIdRatioBased(ratio),
                resource=Resource.create({"service.name": SERVICE}))
            exporter = OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)
            prov.add_span_processor(BatchSpanProcessor(
                exporter, max_queue_size=8192, max_export_batch_size=512,
                schedule_delay_millis=200))
            tracer = prov.get_tracer("bench")

            t0 = time.perf_counter()
            for _ in range(iters):
                do_work()
                span = tracer.start_span("request")   # non-recording if unsampled
                for k, v in a.items():
                    span.set_attribute(k, v)          # no-op on non-recording span
                span.end()
            dur = time.perf_counter() - t0
            prov.shutdown()                            # drain queue (not timed)
            time.sleep(2)                              # let collector count settle
            recv_after = m(RECV_SPANS)
            exported = int(round(recv_after - recv_before))
            ops = iters / dur
            print(f"[exp3] ratio={ratio:<5} {ops:12,.0f} ops/s  "
                  f"dur={dur:6.2f}s  spans_exported~{exported}")
            rows.append([ratio, iters, round(dur, 3), round(ops, 0), exported])
    finally:
        collector_down()
    write_csv("exp3_sampling_throughput.csv",
              ["sample_ratio", "iterations", "duration_s", "ops_per_sec",
               "spans_exported"], rows)
    return rows


# ---------------------------------------------------------------------------
# metadata + summary
# ---------------------------------------------------------------------------
def host_info():
    plat = sh(["uname", "-mrs"], check=False).stdout.strip()
    try:
        import multiprocessing
        cpus = multiprocessing.cpu_count()
    except Exception:
        cpus = "?"
    return plat, cpus


def sdk_version():
    try:
        from importlib.metadata import version
        return version("opentelemetry-sdk")
    except Exception:
        return "?"


def write_metadata():
    plat, cpus = host_info()
    rows = [
        ["python_version", sys.version.split()[0]],
        ["opentelemetry_sdk_version", sdk_version()],
        ["collector_image_digest", IMAGE],
        ["otlp_endpoint", OTLP_ENDPOINT],
        ["metrics_url", METRICS_URL],
        ["host_platform", plat],
        ["host_cpus", cpus],
        ["exp1_iterations", EXP1_ITERS],
        ["exp2_requests", EXP2_REQUESTS],
        ["export_rtt_ms", EXPORT_RTT_MS],
        ["exp3_iterations", EXP3_ITERS],
        ["fixed_work_unit", "sum of 64 float multiplies"],
        ["timestamp_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())],
    ]
    write_csv("run_metadata.csv", ["key", "value"], rows)


def _read_csv(name, subdir=""):
    d = os.path.join(RESULTS_DIR, subdir) if subdir else RESULTS_DIR
    path = os.path.join(d, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return list(csv.reader(f))


def write_summary():
    lines = []
    lines.append("OTel Python SDK instrumentation-overhead harness -- key numbers")
    lines.append("=" * 62)
    meta = _read_csv("run_metadata.csv")
    if meta:
        d = {r[0]: r[1] for r in meta[1:]}
        lines.append(f"python:    {d.get('python_version','?')}")
        lines.append(f"otel-sdk:  {d.get('opentelemetry_sdk_version','?')}")
        lines.append(f"collector: {d.get('collector_image_digest','?')}")
        lines.append(f"host:      {d.get('host_platform','?')}  cpus={d.get('host_cpus','?')}")
        lines.append("")

    e1 = _read_csv("exp1_span_creation.csv")
    if e1:
        lines.append("Exp1 -- span-creation cost vs attribute count "
                     "(in-process no-export processor, AlwaysOn):")
        lines.append("  arm                     ns/op        ops/s   overhead_ns_vs_baseline")
        for r in e1[1:]:
            lines.append("  %-22s  %8s  %11s  %s" % (r[0], r[2], r[3], r[4]))
        lines.append("  => a span is cheap vs the network but not free; each attribute")
        lines.append("     adds a roughly fixed increment on top of span start/end.")
        lines.append("")

    e2 = _read_csv("exp2_processor_latency.csv")
    if e2:
        rtt = e2[1][2] if len(e2) > 1 else "?"
        lines.append(f"Exp2 -- Simple vs Batch processor, per-request latency "
                     f"(modeled RTT={rtt}ms):")
        lines.append("  arm                  p50_ms   p90_ms   p99_ms    max_ms       rps")
        for r in e2[1:]:
            lines.append("  %-19s  %7s  %7s  %7s  %8s  %8s" %
                         (r[0], r[3], r[4], r[5], r[6], r[7]))
        lines.append("  => Simple pays the RTT on EVERY request (p99 ~ RTT + work);")
        lines.append("     Batch moves export off the hot path (p99 ~ work only).")
        lines.append("")

    e3 = _read_csv("exp3_sampling_throughput.csv")
    if e3:
        lines.append("Exp3 -- head-sampling ratio vs hot-path throughput "
                     "(BatchSpanProcessor -> live collector, OTLP gRPC):")
        lines.append("  ratio  iterations   duration_s      ops/s   spans_exported")
        for r in e3[1:]:
            lines.append("  %-5s  %10s  %10s  %11s  %s" %
                         (r[0], r[1], r[2], r[3], r[4]))
        lines.append("  => unsampled spans are non-recording (no attribute recording,")
        lines.append("     no export), so throughput rises as the sample ratio falls.")
        lines.append("")

    lines.append("Laptop measurements demonstrating the mechanism, not capacity planning.")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "summary.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n  -> wrote {path}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "summary":
        write_summary()
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    if mode in ("all", "exp1"):
        exp1()
    if mode in ("all", "exp2"):
        exp2()
    if mode in ("all", "exp3"):
        exp3()
    write_metadata()
    if mode == "all":
        write_summary()
    print("\nDONE.")


if __name__ == "__main__":
    main()
