#!/usr/bin/env python3
"""Java memory leak: per-request thread pool (leaky) vs shared singleton (fixed).

Builds a digest-pinned eclipse-temurin:21-jdk image that compiles LeakBench.java,
then runs the same request loop twice under identical JVM flags and Docker limits:

  leaky - each request does `new JobExecutor()` and never calls shutdown(). The
          executor's prestarted core threads are alive, so they are GC roots and
          retain the whole object graph (thread -> Worker -> pool -> threadFactory
          -> JobExecutor -> buffer). Heap-after-GC climbs monotonically until the
          JVM dies with OutOfMemoryError: Java heap space.
  fixed - ONE JobExecutor built at startup and reused for every request. One
          instance, one pool, constant thread count, flat heap, runs indefinitely.

LeakBench writes the per-interval CSV itself (leaky.csv / fixed.csv). This script
runs the containers, keeps the raw GC logs, and derives summary.txt and
run_metadata.csv from what was actually measured. Nothing here fabricates numbers.
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

IMAGE = os.environ.get("IMAGE", "java-memory-leak:latest")
# Digest-pinned base recorded for provenance (kept in sync with the Dockerfile FROM).
BASE_IMAGE_DIGEST = os.environ.get(
    "BASE_IMAGE_DIGEST",
    "eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a",
)

MEM = os.environ.get("BENCH_MEM", "1g")
CPUS = os.environ.get("BENCH_CPUS", "2")
HEAP = os.environ.get("BENCH_HEAP", "192m")
# Small thread stacks keep native memory modest so the JAVA HEAP is unambiguously
# the binding constraint (the leak we want to show), not native thread memory.
XSS = os.environ.get("BENCH_XSS", "256k")
GC_NAME = os.environ.get("BENCH_GC", "G1GC")

# Workload parameters (identical for both modes; passed to the JVM as env vars).
BUFFER_KB = os.environ.get("BUFFER_KB", "512")
POOL_CORE = os.environ.get("POOL_CORE", "2")
REPORT_EVERY = os.environ.get("REPORT_EVERY", "5")
REQ_SLEEP_MS = os.environ.get("REQ_SLEEP_MS", "300")
THREAD_PREFIX = os.environ.get("THREAD_PREFIX", "jobexec-pool-")
# leaky runs until OOM; this ceiling only stops a mis-tuned run from looping forever.
LEAKY_MAX_REQUESTS = os.environ.get("LEAKY_MAX_REQUESTS", "100000")
# fixed runs at least this many, and at least FIXED_MULTIPLE x the leaky death count.
FIXED_MIN_REQUESTS = int(os.environ.get("FIXED_MIN_REQUESTS", "600"))
FIXED_MULTIPLE = int(os.environ.get("FIXED_MULTIPLE", "3"))

MODES = ("leaky", "fixed")

DIED_RE = re.compile(
    r"\[leak\] DIED .*?requests=(\d+) heap_after_gc_mb=([\d.]+) live_threads=(\d+) "
    r"instances=(\d+) gc_count=(\d+) elapsed_s=([\d.]+) cause=(.*)$"
)
SURVIVED_RE = re.compile(
    r"\[leak\] SURVIVED .*?requests=(\d+) heap_after_gc_mb=([\d.]+) live_threads=(\d+) "
    r"instances=(\d+) gc_count=(\d+) elapsed_s=([\d.]+)"
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, **kw)


def build_image() -> None:
    run(["docker", "build", "-t", IMAGE, str(ROOT)], check=True)


def jdk_version() -> str:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "java", IMAGE, "-version"],
        capture_output=True, text=True, check=True,
    )
    text = out.stderr or out.stdout
    return text.strip().splitlines()[0] if text else "unknown"


def run_mode(mode: str, max_requests: str) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log_name = f"gc_{mode}.log"
    for old in RESULTS_DIR.glob(f"{log_name}*"):
        old.unlink()

    cmd = [
        "docker", "run", "--rm",
        f"--memory={MEM}", f"--cpus={CPUS}",
        "-v", f"{RESULTS_DIR}:/results",
        "-e", f"BUFFER_KB={BUFFER_KB}",
        "-e", f"POOL_CORE={POOL_CORE}",
        "-e", f"REPORT_EVERY={REPORT_EVERY}",
        "-e", f"REQ_SLEEP_MS={REQ_SLEEP_MS}",
        "-e", f"MAX_REQUESTS={max_requests}",
        "-e", f"THREAD_PREFIX={THREAD_PREFIX}",
        IMAGE,
        f"-Xmx{HEAP}", f"-Xms{HEAP}", f"-Xss{XSS}", f"-XX:+Use{GC_NAME}",
        # filecount=0 disables rotation so the whole run lands in one file.
        f"-Xlog:gc:file=/results/{log_name}:time,level,tags:filecount=0",
        "LeakBench", mode, "/results",
    ]
    print(f"\n=== running mode={mode} (max_requests={max_requests}) ===")
    proc = run(cmd, capture_output=True, text=True)
    combined = proc.stdout + proc.stderr
    (RESULTS_DIR / f"stdout_{mode}.txt").write_text(combined)

    info = {"mode": mode, "exit_code": proc.returncode, "outcome": "unknown"}
    for line in combined.splitlines():
        m = DIED_RE.search(line)
        if m:
            info.update(
                outcome="died", requests=int(m.group(1)), heap_mb=float(m.group(2)),
                threads=int(m.group(3)), instances=int(m.group(4)),
                gc_count=int(m.group(5)), elapsed_s=float(m.group(6)),
                cause=m.group(7).strip(),
            )
        m = SURVIVED_RE.search(line)
        if m:
            info.update(
                outcome="survived", requests=int(m.group(1)), heap_mb=float(m.group(2)),
                threads=int(m.group(3)), instances=int(m.group(4)),
                gc_count=int(m.group(5)), elapsed_s=float(m.group(6)), cause="",
            )

    # If the JVM died so hard it could not print the marker, fall back to the CSV,
    # which is flushed after every row.
    if info["outcome"] == "unknown":
        rows = read_csv(mode)
        if rows:
            last = rows[-1]
            info.update(
                outcome="died" if "OutOfMemoryError" in combined else "ended",
                requests=int(last["requests_served"]),
                heap_mb=float(last["heap_used_after_gc_mb"]),
                threads=int(last["live_thread_count"]),
                instances=int(last["jobexecutor_instances"]),
                gc_count=int(last["gc_count"]),
                elapsed_s=float(last["elapsed_s"]),
                cause="OutOfMemoryError (marker line not printed; recovered from CSV)",
            )

    for line in combined.splitlines():
        if "DIED" in line or "SURVIVED" in line or "OutOfMemoryError" in line:
            print("   ", line)
    return info


def read_csv(mode: str) -> list[dict]:
    path = RESULTS_DIR / f"{mode}.csv"
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def write_summary(results: dict) -> None:
    leaky = results.get("leaky", {})
    fixed = results.get("fixed", {})
    lrows = read_csv("leaky")
    frows = read_csv("fixed")

    lines = []
    lines.append("Java memory leak: per-request thread pool vs shared singleton")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"heap=-Xmx{HEAP} -Xms{HEAP} -Xss{XSS}  {GC_NAME}  mem={MEM} cpus={CPUS}")
    lines.append(f"buffer={BUFFER_KB} KB/JobExecutor  poolCore={POOL_CORE} threads "
                 f"(prestarted, allowCoreThreadTimeOut=false)")
    lines.append(f"request pacing={REQ_SLEEP_MS} ms/request  report every {REPORT_EVERY} requests")
    lines.append("heap_used_after_gc_mb = MemoryMXBean heap used, sampled immediately")
    lines.append("                        after a System.gc()-triggered full GC")
    lines.append("")

    if leaky:
        lines.append("LEAKY")
        lines.append("-" * 62)
        lines.append(f"  outcome                : {leaky.get('outcome')}  ({leaky.get('cause','')})")
        lines.append(f"  died after requests    : {leaky.get('requests')}")
        lines.append(f"  heap after GC at death : {leaky.get('heap_mb')} MB  (of {HEAP} max)")
        lines.append(f"  live pool threads      : {leaky.get('threads')}")
        lines.append(f"  JobExecutor instances  : {leaky.get('instances')}")
        lines.append(f"  time to death          : {leaky.get('elapsed_s')} s")
        if lrows:
            first, last = lrows[0], lrows[-1]
            lines.append(f"  heap climb             : {first['heap_used_after_gc_mb']} MB "
                         f"@ {first['requests_served']} req  ->  "
                         f"{last['heap_used_after_gc_mb']} MB @ {last['requests_served']} req")
            lines.append(f"  csv rows               : {len(lrows)}")
        lines.append("")

    if fixed:
        lines.append("FIXED")
        lines.append("-" * 62)
        lines.append(f"  outcome                : {fixed.get('outcome')} (never died)")
        lines.append(f"  requests served        : {fixed.get('requests')}")
        lines.append(f"  heap after GC at end   : {fixed.get('heap_mb')} MB")
        lines.append(f"  live pool threads      : {fixed.get('threads')}")
        lines.append(f"  JobExecutor instances  : {fixed.get('instances')}")
        lines.append(f"  run time               : {fixed.get('elapsed_s')} s")
        if frows:
            heaps = [float(r["heap_used_after_gc_mb"]) for r in frows]
            threads = {int(r["live_thread_count"]) for r in frows}
            insts = {int(r["jobexecutor_instances"]) for r in frows}
            lines.append(f"  heap min/max/mean      : {min(heaps):.2f} / {max(heaps):.2f} / "
                         f"{sum(heaps)/len(heaps):.2f} MB  (flat)")
            lines.append(f"  distinct thread counts : {sorted(threads)}")
            lines.append(f"  distinct instance cnts : {sorted(insts)}")
            lines.append(f"  csv rows               : {len(frows)}")
        lines.append("")

    if leaky and fixed and leaky.get("requests"):
        ratio = fixed.get("requests", 0) / leaky["requests"]
        lines.append(f"FIXED served {ratio:.1f}x the requests that killed LEAKY, and was "
                     f"still alive and flat when the run was stopped.")

    text = "\n".join(lines) + "\n"
    (RESULTS_DIR / "summary.txt").write_text(text)
    print("\n" + text)


def write_metadata(jdk: str, results: dict) -> None:
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
        ("gc", GC_NAME),
        ("heap", f"-Xmx{HEAP} -Xms{HEAP}"),
        ("thread_stack", f"-Xss{XSS}"),
        ("container_memory", MEM),
        ("container_cpus", CPUS),
        ("buffer_kb_per_jobexecutor", BUFFER_KB),
        ("pool_core_threads", POOL_CORE),
        ("pool_prestarted", "true"),
        ("allow_core_thread_timeout", "false"),
        ("thread_name_prefix", THREAD_PREFIX),
        ("request_pacing_ms", REQ_SLEEP_MS),
        ("report_every_requests", REPORT_EVERY),
        ("heap_metric", "MemoryMXBean heap used, sampled right after System.gc() full GC"),
        ("leaky_death_requests", results.get("leaky", {}).get("requests", "")),
        ("leaky_death_cause", results.get("leaky", {}).get("cause", "")),
        ("fixed_requests_served", results.get("fixed", {}).get("requests", "")),
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

    results = {}
    if "leaky" in modes:
        results["leaky"] = run_mode("leaky", LEAKY_MAX_REQUESTS)
    if "fixed" in modes:
        death = results.get("leaky", {}).get("requests")
        target = max(FIXED_MIN_REQUESTS, FIXED_MULTIPLE * death) if death else FIXED_MIN_REQUESTS
        results["fixed"] = run_mode("fixed", str(target))

    write_summary(results)
    write_metadata(jdk, results)
    print("\nDone. Results in", RESULTS_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
