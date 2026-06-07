"""JVM garbage-collector / heap-tuning benchmark.

Runs the SAME Java allocation workload (Bench.java) under different GC configs inside
a digest-pinned eclipse-temurin:21-jdk container, and measures two things per run:

  1. request-latency percentiles (p50/p99/p99.9/max) from the app itself -- GC
     stop-the-world pauses land on top of a request and show up in the tail.
  2. real STW pause times, parsed from the JVM's own -Xlog:gc output on stderr.

Two experiments:

  1. collector_comparison -- same -Xmx, same workload, under ParallelGC / G1GC /
     generational ZGC. Story: throughput collectors (Parallel, and to a lesser
     extent G1) take big STW pauses that wreck the latency tail; ZGC's pauses are
     sub-millisecond, so the tail stays flat.

  2. heap_sizing -- fix the collector (G1), shrink -Xmx until the same workload no
     longer fits comfortably. Story: undersize the heap and GC frequency / % time in
     GC explode while throughput collapses.

Everything is env-configurable so the matrix can be retuned without editing code.
No network, no ports -- the workload is purely in-process CPU + heap.

Env knobs:
  IMAGE         pinned JDK image (default: the digest below)
  RESULTS_DIR   where CSVs / summary land (default: ./results)
  RUNS          repeats per config, median is reported (default: 3)
  OPS           measured requests per run
  WARMUP_OPS    warmup requests (discarded) per run
  LIVE_ENTRIES  cache entries held live (old-gen occupancy)
  PAYLOAD_KB    size of each live cache payload
  GARBAGE_KB    short-lived garbage allocated per request
  CHURN_EVERY   replace a cache entry every N requests (promotion rate)
  SEED          workload RNG seed
"""
import csv
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.environ.get(
    "IMAGE",
    "eclipse-temurin:21-jdk@sha256:"
    "da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a",
)
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
RUNS = int(os.environ.get("RUNS", "3"))

SEED = os.environ.get("SEED", "42")

# Two experiments need two workload scales, because they tell opposite stories:
#
#  - collector comparison wants a LARGE live set so a full/old collection has a lot
#    to compact -> the throughput collectors take clearly visible STW pauses. It runs
#    at a big heap that comfortably holds the live set, so the ONLY difference between
#    configs is the collector.
#
#  - heap sizing wants a SMALL live set and then shrinks the heap toward it, so the
#    small heaps thrash. A 2.5GB live set would simply OOM at 256m; a ~140MB live set
#    fits at 1g and thrashes at 256m.
#
# Within each experiment the workload is identical across every config (same seed,
# same live set, same op count) -- only the JVM flag under test changes.

COL_WORK = {  # experiment 1: collector comparison
    "OPS": os.environ.get("COL_OPS", "2000000"),
    "WARMUP_OPS": os.environ.get("COL_WARMUP_OPS", "300000"),
    "LIVE_ENTRIES": os.environ.get("COL_LIVE_ENTRIES", "10000"),
    "PAYLOAD_KB": os.environ.get("COL_PAYLOAD_KB", "256"),   # 10000 * 256KB = 2.5GB live
    "GARBAGE_KB": os.environ.get("COL_GARBAGE_KB", "16"),
    "CHURN_EVERY": os.environ.get("COL_CHURN_EVERY", "8"),
    "SEED": SEED,
}
COLLECTOR_HEAP = os.environ.get("COLLECTOR_HEAP", "4g")

HEAP_WORK = {  # experiment 2: heap sizing thrash
    "OPS": os.environ.get("HEAP_OPS", "4000000"),
    "WARMUP_OPS": os.environ.get("HEAP_WARMUP_OPS", "500000"),
    "LIVE_ENTRIES": os.environ.get("HEAP_LIVE_ENTRIES", "3000"),
    "PAYLOAD_KB": os.environ.get("HEAP_PAYLOAD_KB", "48"),   # 3000 * 48KB = 141MB live
    "GARBAGE_KB": os.environ.get("HEAP_GARBAGE_KB", "16"),
    "CHURN_EVERY": os.environ.get("HEAP_CHURN_EVERY", "6"),
    "SEED": SEED,
}
HEAP_SIZES = os.environ.get("HEAP_SIZES", "256m,320m,384m,512m,1g").split(",")

# a Pause line looks like:
#   [1.234s][info][gc] GC(3) Pause Young (Normal) (G1 Evacuation Pause) 210M->48M(512M) 12.345ms
# ZGC generational STW pauses look like:
#   [1.2s][info][gc] GC(4) Major Collection ... / Pause Mark Start 0.033ms
PAUSE_RE = re.compile(r"\bPause\b.*?([\d.]+)ms\s*$", re.M)
METRICS_RE = re.compile(r"^METRICS\s+(.*)$", re.M)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def compile_bench():
    """Compile Bench.java inside the pinned image; class stays in a named volume-less
    tmpfs per run, so we just compile fresh each run (javac is ~1s). Here we only
    verify it compiles cleanly once, up front."""
    r = sh([
        "docker", "run", "--rm", "-v", f"{HERE}:/src:ro",
        IMAGE, "bash", "-c", "javac -d /tmp/out /src/Bench.java && echo OK",
    ])
    if "OK" not in r.stdout:
        sys.exit(f"compile failed:\n{r.stdout}\n{r.stderr}")


def run_once(label, heap, gc_flags, work):
    """Run the workload once. Returns dict of parsed metrics + GC pause stats."""
    jvm = [
        f"-Xms{heap}", f"-Xmx{heap}",
        "-XX:+AlwaysPreTouch",          # commit heap up front so growth isn't measured as GC
        # gc  -> G1/Parallel top-level "Pause Young/Full/..." STW lines
        # gc+phases -> ZGC's sub-millisecond "Pause Mark Start/End/Relocate" STW lines
        # (ZGC does not emit any Pause line at the plain gc tag). METRICS -> stdout.
        "-Xlog:gc,gc+phases=info:stderr",
    ] + gc_flags
    inner = "javac -d /tmp/out /src/Bench.java && java " + " ".join(jvm) + " -cp /tmp/out Bench"
    cmd = ["docker", "run", "--rm", "-v", f"{HERE}:/src:ro"]
    for k, v in work.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "bash", "-c", inner]

    r = sh(cmd)
    m = METRICS_RE.search(r.stdout)
    if not m:
        # one retry: transient docker/host hiccups shouldn't abort a 10-minute matrix
        r = sh(cmd)
        m = METRICS_RE.search(r.stdout)
    if not m:
        sys.exit(f"[{label}] no METRICS line.\nstdout:\n{r.stdout}\nstderr tail:\n{r.stderr[-2000:]}")
    metrics = {}
    for tok in m.group(1).split():
        k, v = tok.split("=")
        metrics[k] = float(v)

    pauses = [float(x) for x in PAUSE_RE.findall(r.stderr)]
    metrics["_pauses"] = pauses
    return metrics


def summarize_run(metrics):
    """Collapse GC pause list into count / total / max / p99 (ms)."""
    p = sorted(metrics["_pauses"])
    n = len(p)
    total = sum(p)
    mx = p[-1] if p else 0.0
    p99 = p[int(0.99 * (n - 1))] if n else 0.0
    return {
        "gc_pause_count": n,
        "gc_pause_total_ms": round(total, 2),
        "gc_pause_max_ms": round(mx, 3),
        "gc_pause_p99_ms": round(p99, 3),
        "throughput_ops_s": round(metrics["throughput_ops_s"]),
        "wall_s": round(metrics["wall_s"], 3),
        "req_p50_us": round(metrics["p50_us"], 1),
        "req_p99_us": round(metrics["p99_us"], 1),
        "req_p999_us": round(metrics["p999_us"], 1),
        "req_max_us": round(metrics["max_us"], 1),
        "pct_time_in_gc": round(100.0 * total / (metrics["wall_s"] * 1000.0), 2),
    }


def median_of(rows, keys):
    """Median across RUNS for each metric. Reported run count is len(rows)."""
    out = {}
    for k in keys:
        out[k] = round(statistics.median(r[k] for r in rows), 3)
    return out


def bench(label, heap, gc_flags, work):
    print(f"  {label:14} heap={heap} runs={RUNS} ...", flush=True)
    runs = []
    for i in range(RUNS):
        s = summarize_run(run_once(label, heap, gc_flags, work))
        runs.append(s)
        print(f"      run{i+1}: max_pause={s['gc_pause_max_ms']}ms "
              f"req_p99.9={s['req_p999_us']}us thr={s['throughput_ops_s']} "
              f"gc%={s['pct_time_in_gc']}", flush=True)
    keys = list(runs[0].keys())
    med = median_of(runs, keys)
    med["label"] = label
    med["heap"] = heap
    return med


# ---------------------------------------------------------------- experiments

COLLECTORS = [
    ("ParallelGC", ["-XX:+UseParallelGC"]),
    ("G1GC",       ["-XX:+UseG1GC"]),
    ("ZGC-gen",    ["-XX:+UseZGC", "-XX:+ZGenerational"]),
]


def experiment_collectors():
    print("\n== experiment 1: collector comparison (heap fixed at %s, live=%s) =="
          % (COLLECTOR_HEAP, COL_WORK["LIVE_ENTRIES"]))
    rows = [bench(name, COLLECTOR_HEAP, flags, COL_WORK) for name, flags in COLLECTORS]
    cols = ["label", "heap", "gc_pause_count", "gc_pause_total_ms", "gc_pause_max_ms",
            "gc_pause_p99_ms", "req_p50_us", "req_p99_us", "req_p999_us", "req_max_us",
            "throughput_ops_s", "pct_time_in_gc", "wall_s"]
    write_csv("collector_comparison.csv", rows, cols)
    return rows


def experiment_heap_sizing():
    print("\n== experiment 2: heap sizing thrash (collector fixed at G1GC, live=%s) =="
          % HEAP_WORK["LIVE_ENTRIES"])
    rows = [bench(f"G1-{h}", h, ["-XX:+UseG1GC"], HEAP_WORK) for h in HEAP_SIZES]
    cols = ["label", "heap", "gc_pause_count", "gc_pause_total_ms", "gc_pause_max_ms",
            "req_p99_us", "req_p999_us", "throughput_ops_s", "pct_time_in_gc", "wall_s"]
    write_csv("heap_sizing.csv", rows, cols)
    return rows


def write_csv(name, rows, cols):
    path = os.path.join(RESULTS, name)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"    wrote {path}")


def java_version():
    r = sh(["docker", "run", "--rm", IMAGE, "java", "-version"])
    return (r.stderr or r.stdout).strip().replace("\n", " | ")


def docker_arch():
    r = sh(["docker", "inspect", "--format", "{{.Architecture}}", IMAGE])
    return r.stdout.strip()


def main():
    os.makedirs(RESULTS, exist_ok=True)
    print(f"image: {IMAGE}")
    compile_bench()

    col = experiment_collectors()
    heap = experiment_heap_sizing()

    # metadata (no wall-clock date, per repo convention)
    ver = java_version()
    arch = docker_arch()
    digest = IMAGE.split("@", 1)[1] if "@" in IMAGE else "unpinned"
    def live_mb(w):
        return round((int(w["LIVE_ENTRIES"]) * int(w["PAYLOAD_KB"])) / 1024.0, 1)
    meta = [
        ("java_version", ver),
        ("image", IMAGE),
        ("image_digest", digest),
        ("image_arch", arch),
        ("runs_per_config", RUNS),
        ("seed", SEED),
        # experiment 1: collector comparison
        ("col_collector_heap", COLLECTOR_HEAP),
        ("col_ops", COL_WORK["OPS"]),
        ("col_warmup_ops", COL_WORK["WARMUP_OPS"]),
        ("col_live_entries", COL_WORK["LIVE_ENTRIES"]),
        ("col_payload_kb", COL_WORK["PAYLOAD_KB"]),
        ("col_garbage_kb", COL_WORK["GARBAGE_KB"]),
        ("col_churn_every", COL_WORK["CHURN_EVERY"]),
        ("col_live_mb", live_mb(COL_WORK)),
        # experiment 2: heap sizing
        ("heap_sizes", ";".join(HEAP_SIZES)),
        ("heap_ops", HEAP_WORK["OPS"]),
        ("heap_warmup_ops", HEAP_WORK["WARMUP_OPS"]),
        ("heap_live_entries", HEAP_WORK["LIVE_ENTRIES"]),
        ("heap_payload_kb", HEAP_WORK["PAYLOAD_KB"]),
        ("heap_garbage_kb", HEAP_WORK["GARBAGE_KB"]),
        ("heap_churn_every", HEAP_WORK["CHURN_EVERY"]),
        ("heap_live_mb", live_mb(HEAP_WORK)),
    ]
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerows(meta)

    write_summary(col, heap, ver, arch, digest)
    print(f"\ndone. artifacts in {RESULTS}")


def write_summary(col, heap, ver, arch, digest):
    lines = []
    W = 62
    lines.append("=" * W)
    lines.append("JVM GC / heap-tuning benchmark")
    lines.append("=" * W)
    lines.append(f"image  : {IMAGE}")
    lines.append(f"java   : {ver}")
    lines.append(f"arch   : {arch}   runs/config: {RUNS} (median reported)")
    lines.append("")
    lines.append("-" * W)
    lines.append(f"EXPERIMENT 1  collector comparison (heap fixed at {COLLECTOR_HEAP})")
    lines.append(f"  workload: ops={COL_WORK['OPS']} live_entries={COL_WORK['LIVE_ENTRIES']} "
                 f"payload={COL_WORK['PAYLOAD_KB']}KB churn_every={COL_WORK['CHURN_EVERY']} "
                 f"(~{round(int(COL_WORK['LIVE_ENTRIES'])*int(COL_WORK['PAYLOAD_KB'])/1024.0)}MB live)")
    lines.append("-" * W)
    lines.append(f"  {'collector':10} {'max_pause':>10} {'gc_p99':>8} {'tot_pause':>10} "
                 f"{'gc_n':>6} {'req_p99.9':>10} {'req_max':>10} {'thr_ops/s':>11}")
    for r in col:
        lines.append(f"  {r['label']:10} {r['gc_pause_max_ms']:>9}ms {r['gc_pause_p99_ms']:>7}ms "
                     f"{r['gc_pause_total_ms']:>9}ms {r['gc_pause_count']:>6} "
                     f"{r['req_p999_us']:>8}us {r['req_max_us']:>8}us {r['throughput_ops_s']:>11}")
    lines.append("")
    lines.append("-" * W)
    lines.append("EXPERIMENT 2  heap sizing thrash (collector fixed at G1GC)")
    lines.append(f"  workload: ops={HEAP_WORK['OPS']} live_entries={HEAP_WORK['LIVE_ENTRIES']} "
                 f"payload={HEAP_WORK['PAYLOAD_KB']}KB churn_every={HEAP_WORK['CHURN_EVERY']} "
                 f"(~{round(int(HEAP_WORK['LIVE_ENTRIES'])*int(HEAP_WORK['PAYLOAD_KB'])/1024.0)}MB live)")
    lines.append("-" * W)
    lines.append(f"  {'heap':8} {'%time_gc':>9} {'gc_n':>7} {'tot_pause':>10} "
                 f"{'req_p99.9':>10} {'thr_ops/s':>11}")
    for r in heap:
        lines.append(f"  {r['heap']:8} {r['pct_time_in_gc']:>8}% {r['gc_pause_count']:>7} "
                     f"{r['gc_pause_total_ms']:>9}ms {r['req_p999_us']:>8}us "
                     f"{r['throughput_ops_s']:>11}")
    lines.append("")
    lines.append("laptop numbers demonstrating the mechanism, not capacity planning.")
    lines.append(f"artifacts in results/  |  digest {digest}")
    txt = "\n".join(lines) + "\n"
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(txt)
    print("\n" + txt)


if __name__ == "__main__":
    main()
