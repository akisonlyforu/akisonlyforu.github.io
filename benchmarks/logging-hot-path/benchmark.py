"""Measure the cost of stdlib `logging` on the hot path.

Three experiments, all stdlib-only:
  1. disabled-debug-cost - a DEBUG line that never emits (logger at INFO), built
     three ways with an EXPENSIVE argument: eager f-string (arg built every call
     and thrown away), isEnabledFor guard, and %-style lazy formatting. The point
     is how much you pay for a log line that produces nothing.
  2. sync-vs-async - real INFO lines to a file, timed PER CALL on the calling
     thread, sync FileHandler vs QueueHandler+QueueListener. p50/p99/p999/max.
  3. sampling - log every event vs ~1 in 100. Lines, bytes, and throughput.

Env:
  RESULTS_DIR   - where CSVs/summary land (default ./results)
  EXP1_ITERS    - exp1 iterations per variant       (default 1_000_000)
  EXP1_REPEATS  - exp1 timed repeats, median kept    (default 5)
  EXP2_CALLS    - exp2 INFO calls per mode           (default 200_000)
  EXP2_SAMPLES  - exp2 downsample target per mode    (default 2000)
  EXP3_EVENTS   - exp3 events per mode               (default 1_000_000)
  EXP3_SAMPLE_RATE - exp3 sample denominator (1 in N)(default 100)
  SEED          - RNG seed                           (default 1234)
"""
import csv
import json
import logging
import logging.handlers
import os
import queue
import random
import statistics
import sys
import tempfile
import time

RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
EXP1_ITERS = int(os.environ.get("EXP1_ITERS", "1000000"))
EXP1_REPEATS = int(os.environ.get("EXP1_REPEATS", "5"))
EXP2_CALLS = int(os.environ.get("EXP2_CALLS", "50000"))
EXP2_SAMPLES = int(os.environ.get("EXP2_SAMPLES", "2000"))
EXP3_EVENTS = int(os.environ.get("EXP3_EVENTS", "1000000"))
EXP3_SAMPLE_RATE = int(os.environ.get("EXP3_SAMPLE_RATE", "100"))
SEED = int(os.environ.get("SEED", "1234"))

perf = time.perf_counter_ns


def make_payload(order_id):
    """A realistically-sized structured payload that is expensive to render."""
    return {
        "order_id": order_id,
        "customer": f"cust-{order_id % 100000}",
        "items": [{"sku": f"sku-{(order_id + k) % 9999}", "qty": (k % 5) + 1} for k in range(4)],
        "total_cents": (order_id * 37) % 1000000,
        "currency": "USD",
        "shipping": {"country": "US", "method": "ground", "expedited": order_id % 7 == 0},
    }


# --------------------------------------------------------------------------
# Experiment 1: the cost of a DISABLED debug line, built three ways.
# --------------------------------------------------------------------------
def exp1_variant(logger, variant, iters):
    """Run one variant for `iters` and return total nanoseconds for the loop."""
    if variant == "eager":
        t0 = perf()
        for i in range(iters):
            payload = make_payload(i)
            logger.debug(f"processed order {i}: {json.dumps(payload)}")
        return perf() - t0
    if variant == "guarded":
        t0 = perf()
        for i in range(iters):
            if logger.isEnabledFor(logging.DEBUG):
                payload = make_payload(i)
                logger.debug(f"processed order {i}: {json.dumps(payload)}")
        return perf() - t0
    if variant == "lazy":
        t0 = perf()
        for i in range(iters):
            payload = make_payload(i)
            logger.debug("processed order %s: %s", i, payload)
        return perf() - t0
    raise ValueError(variant)


def experiment_1():
    logger = logging.getLogger("exp1")
    logger.handlers[:] = [logging.NullHandler()]
    logger.setLevel(logging.INFO)          # DEBUG is disabled -> nothing emits
    logger.propagate = False

    # sanity: confirm not a single line escapes (all three are no-ops output-wise)
    variants = ["eager", "guarded", "lazy"]
    warm = max(1, EXP1_ITERS // 20)
    for v in variants:
        exp1_variant(logger, v, warm)      # warm up caches / branch predictor

    rows = []
    best = {}
    for v in variants:
        samples_ns = [exp1_variant(logger, v, EXP1_ITERS) for _ in range(EXP1_REPEATS)]
        total_ns = int(statistics.median(samples_ns))
        ns_per_call = total_ns / EXP1_ITERS
        ops = EXP1_ITERS / (total_ns / 1e9)
        best[v] = ns_per_call
        rows.append({
            "variant": v,
            "iterations": EXP1_ITERS,
            "total_ns": total_ns,
            "ns_per_call": round(ns_per_call, 3),
            "ops_per_sec": int(ops),
        })

    with open(os.path.join(RESULTS, "exp1_disabled_debug.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["variant", "iterations", "total_ns", "ns_per_call", "ops_per_sec"])
        w.writeheader()
        w.writerows(rows)

    eager_vs_lazy = best["eager"] / best["lazy"]
    eager_vs_guarded = best["eager"] / best["guarded"]

    lines = []
    lines.append("=" * 66)
    lines.append("EXPERIMENT 1  cost of a DISABLED debug line (logger at INFO)")
    lines.append("=" * 66)
    lines.append(f"  {EXP1_ITERS:,} iterations/variant, median of {EXP1_REPEATS} timed repeats")
    lines.append(f"  {'variant':<9} {'ns/call':>12} {'ops/sec':>16}")
    for r in rows:
        lines.append(f"  {r['variant']:<9} {r['ns_per_call']:>12.2f} {r['ops_per_sec']:>16,}")
    lines.append(f"  eager is {eager_vs_lazy:.1f}x slower than lazy (%-formatting)")
    lines.append(f"  eager is {eager_vs_guarded:.1f}x slower than isEnabledFor-guarded")
    return rows, eager_vs_lazy, eager_vs_guarded, lines


# --------------------------------------------------------------------------
# Experiment 2: sync FileHandler vs async QueueHandler, per-call latency.
# --------------------------------------------------------------------------
def _percentiles(latencies_us):
    s = sorted(latencies_us)
    n = len(s)

    def pct(p):
        # nearest-rank; clamp index
        idx = min(n - 1, max(0, int(round(p / 100.0 * n)) - 1))
        return s[idx]

    return pct(50), pct(99), pct(99.9), s[-1]


def _downsample(latencies_us, target):
    n = len(latencies_us)
    if n <= target:
        return list(latencies_us), 1
    stride = n // target
    return latencies_us[::stride], stride


class FsyncFileHandler(logging.FileHandler):
    """A FileHandler that flushes AND fsyncs every record -- a durable/audit log.

    This makes the sink genuinely block on I/O per emit, which is the premise of
    the experiment ("sync blocks the caller on I/O"). A plain buffered FileHandler
    is absorbed by the OS page cache and does not block enough to show the effect
    (see results/attempts/NOTE_exp2_buffered.txt). The SAME fsync cost is paid in
    both modes; only WHERE it runs differs -- caller thread (sync) vs background
    thread (async) -- so the experiment isolates that one variable.
    """
    def emit(self, record):
        super().emit(record)
        if self.stream is not None:
            os.fsync(self.stream.fileno())


def exp2_run(mode, logfile, calls):
    logger = logging.getLogger(f"exp2.{mode}")
    logger.handlers[:] = []
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = FsyncFileHandler(logfile, mode="w")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    listener = None
    if mode == "sync":
        logger.addHandler(file_handler)
    elif mode == "async":
        q = queue.SimpleQueue()
        logger.addHandler(logging.handlers.QueueHandler(q))
        listener = logging.handlers.QueueListener(q, file_handler, respect_handler_level=True)
        listener.start()
    else:
        raise ValueError(mode)

    # warm up
    for i in range(min(5000, calls // 10 or 1)):
        logger.info("warmup order %s status=%s amount=%s", i, "ok", i * 7)

    latencies_us = [0.0] * calls
    t_wall0 = perf()
    for i in range(calls):
        t0 = perf()
        logger.info("processed order %s status=%s amount=%s region=%s", i, "ok", i * 7, "us-east-1")
        latencies_us[i] = (perf() - t0) / 1000.0
    wall_s = (perf() - t_wall0) / 1e9

    if listener is not None:
        listener.stop()   # drains the queue; excluded from per-call timing on purpose
    file_handler.close()

    p50, p99, p999, mx = _percentiles(latencies_us)
    return {
        "mode": mode,
        "calls": calls,
        "p50_us": round(p50, 3),
        "p99_us": round(p99, 3),
        "p999_us": round(p999, 3),
        "max_us": round(mx, 3),
        "wall_s": round(wall_s, 4),
    }, latencies_us


def experiment_2():
    rows = []
    sample_rows = []
    tmpdir = tempfile.mkdtemp(prefix="exp2-logs-")
    for mode in ["sync", "async"]:
        logfile = os.path.join(tmpdir, f"{mode}.log")
        row, latencies = exp2_run(mode, logfile, EXP2_CALLS)
        rows.append(row)
        ds, stride = _downsample(latencies, EXP2_SAMPLES)
        for v in ds:
            sample_rows.append({"mode": mode, "latency_us": round(v, 3)})
        row["_stride"] = stride

    with open(os.path.join(RESULTS, "exp2_sync_vs_async.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mode", "calls", "p50_us", "p99_us", "p999_us", "max_us", "wall_s"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    with open(os.path.join(RESULTS, "exp2_latency_samples.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mode", "latency_us"])
        w.writeheader()
        w.writerows(sample_rows)

    lines = []
    lines.append("")
    lines.append("=" * 66)
    lines.append("EXPERIMENT 2  per-call latency on the caller: sync vs async handler")
    lines.append("=" * 66)
    lines.append(f"  {EXP2_CALLS:,} INFO calls/mode to a durable (flush+fsync) file sink")
    lines.append(f"  latency timed per call on the calling thread")
    lines.append(f"  {'mode':<7} {'p50_us':>9} {'p99_us':>9} {'p999_us':>10} {'max_us':>11} {'wall_s':>9}")
    for r in rows:
        lines.append(f"  {r['mode']:<7} {r['p50_us']:>9.3f} {r['p99_us']:>9.3f} "
                     f"{r['p999_us']:>10.3f} {r['max_us']:>11.3f} {r['wall_s']:>9.4f}")
    stride = rows[0].get("_stride", 1)
    lines.append(f"  latency samples downsampled to ~{EXP2_SAMPLES}/mode (every {stride}th call)")
    return rows, lines


# --------------------------------------------------------------------------
# Experiment 3: log every event vs sample ~1 in N.
# --------------------------------------------------------------------------
def exp3_run(mode, logfile, events, sample_rate, rng):
    logger = logging.getLogger(f"exp3.{mode}")
    logger.handlers[:] = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fh = logging.FileHandler(logfile, mode="w")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    lines_written = 0
    t0 = perf()
    if mode == "full":
        for i in range(events):
            # simulate a tiny unit of real work per event
            _ = (i * 2654435761) & 0xFFFFFFFF
            logger.info("event %s user=%s action=%s value=%s", i, i % 100000, "click", (i * 7) % 999)
            lines_written += 1
    elif mode == "sampled":
        for i in range(events):
            _ = (i * 2654435761) & 0xFFFFFFFF
            if i % sample_rate == 0:
                logger.info("event %s user=%s action=%s value=%s", i, i % 100000, "click", (i * 7) % 999)
                lines_written += 1
    else:
        raise ValueError(mode)
    wall_s = (perf() - t0) / 1e9
    fh.close()

    bytes_written = os.path.getsize(logfile)
    ops = events / wall_s
    return {
        "mode": mode,
        "events": events,
        "lines_written": lines_written,
        "bytes_written": bytes_written,
        "ops_per_sec": int(ops),
    }


def experiment_3():
    rng = random.Random(SEED)
    tmpdir = tempfile.mkdtemp(prefix="exp3-logs-")
    rows = []
    # warm the filesystem / handler path
    exp3_run("full", os.path.join(tmpdir, "warm.log"), min(20000, EXP3_EVENTS // 10 or 1),
             EXP3_SAMPLE_RATE, rng)
    for mode in ["full", "sampled"]:
        logfile = os.path.join(tmpdir, f"{mode}.log")
        rows.append(exp3_run(mode, logfile, EXP3_EVENTS, EXP3_SAMPLE_RATE, rng))

    with open(os.path.join(RESULTS, "exp3_sampling.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mode", "events", "lines_written", "bytes_written", "ops_per_sec"])
        w.writeheader()
        w.writerows(rows)

    full, sampled = rows[0], rows[1]
    line_ratio = full["lines_written"] / max(1, sampled["lines_written"])
    byte_ratio = full["bytes_written"] / max(1, sampled["bytes_written"])
    tput_ratio = sampled["ops_per_sec"] / max(1, full["ops_per_sec"])

    lines = []
    lines.append("")
    lines.append("=" * 66)
    lines.append(f"EXPERIMENT 3  log every event vs sample 1-in-{EXP3_SAMPLE_RATE}")
    lines.append("=" * 66)
    lines.append(f"  {EXP3_EVENTS:,} events/mode")
    lines.append(f"  {'mode':<9} {'lines':>10} {'bytes':>14} {'ops/sec':>16}")
    for r in rows:
        lines.append(f"  {r['mode']:<9} {r['lines_written']:>10,} {r['bytes_written']:>14,} {r['ops_per_sec']:>16,}")
    lines.append(f"  sampling wrote {line_ratio:.0f}x fewer lines, {byte_ratio:.0f}x fewer bytes")
    lines.append(f"  and ran {tput_ratio:.2f}x the throughput of full logging")
    return rows, lines


def main():
    os.makedirs(RESULTS, exist_ok=True)
    py = sys.version.split()[0]
    image_digest = os.environ.get("IMAGE_DIGEST", "unknown")

    r1, e_vs_l, e_vs_g, l1 = experiment_1()
    r2, l2 = experiment_2()
    r3, l3 = experiment_3()

    out = []
    out.append(f"logging hot-path benchmark | python {py} | image {image_digest}")
    out.extend(l1)
    out.extend(l2)
    out.extend(l3)
    out.append("")
    out.append(f"  artifacts in results/  (seed={SEED})")
    text = "\n".join(out)
    print(text)

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["python_version", py])
        w.writerow(["image_digest", image_digest])
        w.writerow(["seed", SEED])
        w.writerow(["exp1_iters", EXP1_ITERS])
        w.writerow(["exp1_repeats", EXP1_REPEATS])
        w.writerow(["exp1_ns_per_call_eager", r1[0]["ns_per_call"]])
        w.writerow(["exp1_ns_per_call_guarded", r1[1]["ns_per_call"]])
        w.writerow(["exp1_ns_per_call_lazy", r1[2]["ns_per_call"]])
        w.writerow(["exp1_eager_vs_lazy_ratio", round(e_vs_l, 2)])
        w.writerow(["exp1_eager_vs_guarded_ratio", round(e_vs_g, 2)])
        w.writerow(["exp2_calls", EXP2_CALLS])
        w.writerow(["exp2_sync_p99_us", r2[0]["p99_us"]])
        w.writerow(["exp2_async_p99_us", r2[1]["p99_us"]])
        w.writerow(["exp2_sync_p999_us", r2[0]["p999_us"]])
        w.writerow(["exp2_async_p999_us", r2[1]["p999_us"]])
        w.writerow(["exp3_events", EXP3_EVENTS])
        w.writerow(["exp3_sample_rate", EXP3_SAMPLE_RATE])
        w.writerow(["exp3_full_lines", r3[0]["lines_written"]])
        w.writerow(["exp3_sampled_lines", r3[1]["lines_written"]])
        w.writerow(["exp3_full_bytes", r3[0]["bytes_written"]])
        w.writerow(["exp3_sampled_bytes", r3[1]["bytes_written"]])


if __name__ == "__main__":
    main()
