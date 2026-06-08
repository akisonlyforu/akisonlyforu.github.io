#!/usr/bin/env python3
"""Java G1GC: on-heap vs off-heap long-lived population benchmark orchestrator.

Builds a digest-pinned eclipse-temurin:21-jdk image that compiles GcBench.java,
then runs the same fixed workload twice under identical resource limits:

  onheap  - the long-lived population lives in a HashMap<Long, byte[]> (~64% of heap),
            which pins the G1 old generation above IHOP and forces repeated,
            low-yield concurrent-mark cycles.
  offheap - the same logical data lives in one ByteBuffer.allocateDirect() slab.
            The heap old generation stays nearly empty, so concurrent marking
            collapses -- at the cost of a per-lookup byte-copy/decode.

For each mode it parses the unified GC log (gc-<mode>.log) and writes:
  gc_onheap.csv / gc_offheap.csv    - concurrent-mark cycles, concurrent time,
                                      per-type pause counts+time, total GC time
  latency_onheap.csv / latency_offheap.csv   - written by GcBench (lookup percentiles)
  throughput.csv                    - per-mode wall clock, ops/sec, lookup p50/p99
  summary.txt                       - human-readable comparison
  run_metadata.csv                  - JDK version, image digest, params, seed

Everything is env-configurable; nothing here fabricates numbers -- every field is
either measured by GcBench or parsed from the JVM's own GC log.
"""

from __future__ import annotations

import csv
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", ROOT / "results"))

IMAGE = os.environ.get("IMAGE", "java-gc-offheap:latest")
# Digest-pinned base recorded for provenance (kept in sync with the Dockerfile FROM).
BASE_IMAGE_DIGEST = os.environ.get(
    "BASE_IMAGE_DIGEST",
    "eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a",
)

MEM = os.environ.get("BENCH_MEM", "3g")
CPUS = os.environ.get("BENCH_CPUS", "4")
HEAP = os.environ.get("BENCH_HEAP", "1500m")

# IHOP is pinned (adaptive IHOP disabled) so the on-heap live set (~50% of heap)
# deterministically and repeatedly crosses the concurrent-mark trigger. This keeps
# the reproduction stable and lets N (and therefore the off-heap direct buffer) stay
# modest, which matters on a memory-contended laptop. Both modes get the SAME flags;
# the off-heap old gen stays near-empty so it never crosses the threshold.
IHOP_PERCENT = os.environ.get("BENCH_IHOP", "40")
EXTRA_JVM_FLAGS = [
    "-XX:-G1UseAdaptiveIHOP",
    f"-XX:InitiatingHeapOccupancyPercent={IHOP_PERCENT}",
]

# Workload parameters (identical for both modes; passed to the JVM as env vars).
BENCH_ENV = {
    "BENCH_N": os.environ.get("BENCH_N", "3500000"),
    "BENCH_PAYLOAD": os.environ.get("BENCH_PAYLOAD", "192"),
    "BENCH_ITERS": os.environ.get("BENCH_ITERS", "40000000"),
    "BENCH_GARBAGE": os.environ.get("BENCH_GARBAGE", "2048"),
    "BENCH_SEED": os.environ.get("BENCH_SEED", "-7046029254386353131"),  # 0x9E3779B97F4A7C15
    "BENCH_SAMPLES": os.environ.get("BENCH_SAMPLES", "2000000"),
}

MODES = ("onheap", "offheap")

DUR_AT_END = re.compile(r"(\d+\.\d+)ms\s*$")
CYCLE_END = re.compile(r"Concurrent Mark Cycle\s+(\d+\.\d+)ms\s*$")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def build_image() -> None:
    run(["docker", "build", "-t", IMAGE, str(ROOT)])


def jdk_version() -> str:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "java", IMAGE, "-version"],
        capture_output=True, text=True, check=True,
    )
    # java -version prints to stderr
    return (out.stderr or out.stdout).strip().splitlines()[0] if (out.stderr or out.stdout) else "unknown"


def run_mode(mode: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log_name = f"gc-{mode}.log"
    out_path = RESULTS_DIR / f"stdout_{mode}.txt"

    # Clear stale logs (incl. any previously rotated .0/.1 segments) so parsing is clean.
    for old in RESULTS_DIR.glob(f"{log_name}*"):
        old.unlink()

    cmd = [
        "docker", "run", "--rm",
        f"--memory={MEM}", f"--cpus={CPUS}",
        "-v", f"{RESULTS_DIR}:/results",
    ]
    for k, v in BENCH_ENV.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        IMAGE,
        f"-Xmx{HEAP}", f"-Xms{HEAP}", "-XX:+UseG1GC",
        *EXTRA_JVM_FLAGS,
        # filecount=0 disables log rotation so the whole run lands in one file.
        f"-Xlog:gc*:file=/results/{log_name}:time,level,tags:filecount=0",
        "GcBench", mode, "/results",
    ]
    print(f"\n=== running mode={mode} ===")
    proc = run(cmd, capture_output=True, text=True)
    out_path.write_text(proc.stdout + proc.stderr)
    # echo the key lines
    for line in (proc.stdout + proc.stderr).splitlines():
        if "[GcBench]" in line:
            print("   ", line)


def parse_gc_log(mode: str) -> dict:
    # Read the active log plus any rotated .N segments (belt-and-suspenders; rotation
    # is disabled via filecount=0, but this stays correct if it is ever re-enabled).
    base = RESULTS_DIR / f"gc-{mode}.log"
    segments = [base] + sorted(RESULTS_DIR.glob(f"gc-{mode}.log.*"))
    text = []
    for seg in segments:
        if seg.exists():
            text.extend(seg.read_text().splitlines())

    concurrent_cycles = 0
    concurrent_ms = 0.0

    pause_counts = {
        "young_normal": 0,
        "young_concurrent_start": 0,
        "young_prepare_mixed": 0,
        "young_mixed": 0,
        "remark": 0,
        "cleanup": 0,
        "full": 0,
        "other": 0,
    }
    pause_ms = {k: 0.0 for k in pause_counts}
    total_pause_ms = 0.0

    for line in text:
        if "] GC(" not in line:
            continue

        cyc = CYCLE_END.search(line)
        if cyc and "Pause" not in line:
            concurrent_cycles += 1
            concurrent_ms += float(cyc.group(1))
            continue

        if "Pause" not in line:
            continue
        m = DUR_AT_END.search(line)
        if not m:
            continue  # a gc,start line without a duration
        dur = float(m.group(1))
        total_pause_ms += dur

        if "Concurrent Start" in line:
            key = "young_concurrent_start"
        elif "Prepare Mixed" in line:
            key = "young_prepare_mixed"
        elif "Pause Young (Mixed)" in line:
            key = "young_mixed"
        elif "Pause Young (Normal)" in line:
            key = "young_normal"
        elif "Pause Remark" in line:
            key = "remark"
        elif "Pause Cleanup" in line:
            key = "cleanup"
        elif "Pause Full" in line:
            key = "full"
        else:
            key = "other"
        pause_counts[key] += 1
        pause_ms[key] += dur

    total_gc_ms = total_pause_ms + concurrent_ms

    return {
        "mode": mode,
        "concurrent_mark_cycles": concurrent_cycles,
        "concurrent_mark_ms": round(concurrent_ms, 3),
        "total_pause_ms": round(total_pause_ms, 3),
        "total_gc_ms": round(total_gc_ms, 3),
        "pause_counts": pause_counts,
        "pause_ms": {k: round(v, 3) for k, v in pause_ms.items()},
    }


def write_gc_csv(gc: dict) -> None:
    path = RESULTS_DIR / f"gc_{gc['mode']}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "count", "total_ms"])
        w.writerow(["concurrent_mark_cycle", gc["concurrent_mark_cycles"], gc["concurrent_mark_ms"]])
        for k in ("young_normal", "young_concurrent_start", "young_prepare_mixed",
                  "young_mixed", "remark", "cleanup", "full", "other"):
            w.writerow([f"pause_{k}", gc["pause_counts"][k], gc["pause_ms"][k]])
        w.writerow(["total_pause", sum(gc["pause_counts"].values()), gc["total_pause_ms"]])
        w.writerow(["total_gc", "", gc["total_gc_ms"]])
    print("wrote", path)


def read_perf(mode: str) -> dict:
    path = RESULTS_DIR / f"perf_{mode}.csv"
    with path.open() as f:
        return next(csv.DictReader(f))


def read_latency(mode: str) -> dict:
    path = RESULTS_DIR / f"latency_{mode}.csv"
    with path.open() as f:
        return next(csv.DictReader(f))


def write_throughput_csv(perf: dict, lat: dict) -> None:
    path = RESULTS_DIR / "throughput.csv"
    fields = [
        "mode", "iterations", "wall_seconds", "throughput_ops_sec",
        "lookup_p50_ns", "lookup_p90_ns", "lookup_p99_ns", "lookup_p999_ns",
        "n_entries", "payload_bytes",
    ]
    exists = path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({
            "mode": perf["mode"],
            "iterations": perf["iterations"],
            "wall_seconds": perf["wall_seconds"],
            "throughput_ops_sec": perf["throughput_ops_sec"],
            "lookup_p50_ns": lat["p50_ns"],
            "lookup_p90_ns": lat["p90_ns"],
            "lookup_p99_ns": lat["p99_ns"],
            "lookup_p999_ns": lat["p999_ns"],
            "n_entries": perf["n_entries"],
            "payload_bytes": perf["payload_bytes"],
        })
    print("wrote", path)


def write_summary(results: dict) -> None:
    lines = []
    lines.append("Java G1GC: on-heap vs off-heap long-lived population")
    lines.append("=" * 56)
    lines.append("")
    lines.append(f"heap=-Xmx{HEAP} -Xms{HEAP}  G1GC {' '.join(EXTRA_JVM_FLAGS)}  mem={MEM} cpus={CPUS}")
    lines.append(f"N={BENCH_ENV['BENCH_N']} entries  payload={BENCH_ENV['BENCH_PAYLOAD']}B  "
                 f"iters={BENCH_ENV['BENCH_ITERS']}  garbage={BENCH_ENV['BENCH_GARBAGE']}B/iter")
    lines.append("")
    header = f"{'metric':<32}{'onheap':>16}{'offheap':>16}"
    lines.append(header)
    lines.append("-" * len(header))

    def row(label, on, off):
        lines.append(f"{label:<32}{str(on):>16}{str(off):>16}")

    on = results["onheap"]
    off = results["offheap"]
    row("concurrent-mark cycles", on["gc"]["concurrent_mark_cycles"], off["gc"]["concurrent_mark_cycles"])
    row("concurrent-mark time (ms)", on["gc"]["concurrent_mark_ms"], off["gc"]["concurrent_mark_ms"])
    row("total STW pause time (ms)", on["gc"]["total_pause_ms"], off["gc"]["total_pause_ms"])
    row("total GC time (ms)", on["gc"]["total_gc_ms"], off["gc"]["total_gc_ms"])
    row("young pauses (count)", on["gc"]["pause_counts"]["young_normal"], off["gc"]["pause_counts"]["young_normal"])
    row("mixed pauses (count)", on["gc"]["pause_counts"]["young_mixed"], off["gc"]["pause_counts"]["young_mixed"])
    row("full GCs (count)", on["gc"]["pause_counts"]["full"], off["gc"]["pause_counts"]["full"])
    row("wall clock (s)", on["perf"]["wall_seconds"], off["perf"]["wall_seconds"])
    row("throughput (ops/sec)", f'{float(on["perf"]["throughput_ops_sec"]):.0f}',
        f'{float(off["perf"]["throughput_ops_sec"]):.0f}')
    row("lookup p50 (ns)", on["lat"]["p50_ns"], off["lat"]["p50_ns"])
    row("lookup p90 (ns)", on["lat"]["p90_ns"], off["lat"]["p90_ns"])
    row("lookup p99 (ns)", on["lat"]["p99_ns"], off["lat"]["p99_ns"])
    row("lookup p999 (ns)", on["lat"]["p999_ns"], off["lat"]["p999_ns"])

    # deltas
    lines.append("")
    conc_on = on["gc"]["concurrent_mark_ms"]
    conc_off = off["gc"]["concurrent_mark_ms"]
    if conc_on > 0:
        lines.append(f"concurrent-mark time reduction off-heap: "
                     f"{100.0 * (conc_on - conc_off) / conc_on:.1f}%")
    gc_on = on["gc"]["total_gc_ms"]
    gc_off = off["gc"]["total_gc_ms"]
    if gc_on > 0:
        lines.append(f"total GC time reduction off-heap:       "
                     f"{100.0 * (gc_on - gc_off) / gc_on:.1f}%")
    tp_on = float(on["perf"]["throughput_ops_sec"])
    tp_off = float(off["perf"]["throughput_ops_sec"])
    if tp_on > 0:
        lines.append(f"throughput change off-heap:             "
                     f"{100.0 * (tp_off - tp_on) / tp_on:+.1f}%")
    p50_on = int(on["lat"]["p50_ns"])
    p50_off = int(off["lat"]["p50_ns"])
    if p50_on > 0:
        lines.append(f"lookup p50 change off-heap:             "
                     f"{100.0 * (p50_off - p50_on) / p50_on:+.1f}%")

    text = "\n".join(lines) + "\n"
    (RESULTS_DIR / "summary.txt").write_text(text)
    print("\n" + text)


def write_metadata(jdk: str) -> None:
    path = RESULTS_DIR / "run_metadata.csv"
    docker_ver = subprocess.run(["docker", "--version"], capture_output=True, text=True).stdout.strip()
    rows = [
        ("run_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("platform", platform.platform()),
        ("python", platform.python_version()),
        ("docker", docker_ver),
        ("image", IMAGE),
        ("base_image_digest", BASE_IMAGE_DIGEST),
        ("jdk_version", jdk),
        ("gc", "G1GC"),
        ("jvm_extra_flags", " ".join(EXTRA_JVM_FLAGS)),
        ("heap", f"-Xmx{HEAP} -Xms{HEAP}"),
        ("container_memory", MEM),
        ("container_cpus", CPUS),
        ("n_entries", BENCH_ENV["BENCH_N"]),
        ("payload_bytes", BENCH_ENV["BENCH_PAYLOAD"]),
        ("workload_iterations", BENCH_ENV["BENCH_ITERS"]),
        ("garbage_bytes_per_iter", BENCH_ENV["BENCH_GARBAGE"]),
        ("seed", BENCH_ENV["BENCH_SEED"]),
        ("latency_samples", BENCH_ENV["BENCH_SAMPLES"]),
    ]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerows(rows)
    print("wrote", path)


def main() -> int:
    modes = sys.argv[1:] or list(MODES)
    for m in modes:
        if m not in MODES:
            print(f"unknown mode {m!r}; valid: {MODES}", file=sys.stderr)
            return 2

    build_image()
    jdk = jdk_version()
    print("JDK:", jdk)

    # fresh throughput.csv
    tp = RESULTS_DIR / "throughput.csv"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if tp.exists():
        tp.unlink()

    results = {}
    for m in modes:
        run_mode(m)
        gc = parse_gc_log(m)
        write_gc_csv(gc)
        perf = read_perf(m)
        lat = read_latency(m)
        write_throughput_csv(perf, lat)
        results[m] = {"gc": gc, "perf": perf, "lat": lat}

    if set(results) == set(MODES):
        write_summary(results)
    write_metadata(jdk)
    print("\nDone. Results in", RESULTS_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
