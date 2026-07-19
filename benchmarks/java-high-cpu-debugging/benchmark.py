#!/usr/bin/env python3
"""High-CPU-in-Java reproduction lab driver.

Builds the lab jar (mvn -q package), then for each of 6 modes -- 3 classic CPU bugs
each with a "bad" and "fixed" variant -- launches the workload as a real JVM
subprocess on the host (no Docker: async-profiler needs host-level JVMTI/signal
access that doesn't play nicely through Docker Desktop's Linux VM on a Mac, and the
whole point is that the reader runs this natively), waits for JIT warmup, attaches
`asprof` to capture a real CPU flame graph while the workload runs, and lets the
workload finish out its fixed duration.

  regex-bad / regex-fixed          unanchored ".*ERROR.*" backtracking vs contains()
  spin-bad  / spin-fixed           busy-spin queue.poll() vs blocking poll(timeout)
  hibernate-bad / hibernate-fixed  per-call AUTO-flush dirty-check scan vs FlushMode.COMMIT

Each mode's Main.java writes its own CPU samples (mode,epoch_ms,cpu_load once/sec)
and a throughput/latency row directly into RESULTS_DIR -- this script does not
fabricate or recompute any of those numbers, it only launches processes, attaches
the profiler, and summarizes what the Java side already measured.

Env knobs:
  RESULTS_DIR           where CSVs / flame graphs / summary land (default: ./results)
  WORKLOAD_DURATION_SEC total wall-clock length of each mode's workload (default: 35)
  PROFILE_DURATION_SEC  how long asprof samples for, started after warmup (default: 30)
  WARMUP_SEC            JIT/JVM-startup grace period before attaching the profiler (default: 2)
  HIBERNATE_N           managed-entity count for the Hibernate bug (default: 8000)
"""
import csv
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
JAR = os.path.join(HERE, "target", "java-high-cpu-debugging.jar")

WORKLOAD_DURATION_SEC = int(os.environ.get("WORKLOAD_DURATION_SEC", "35"))
PROFILE_DURATION_SEC = int(os.environ.get("PROFILE_DURATION_SEC", "30"))
WARMUP_SEC = float(os.environ.get("WARMUP_SEC", "2"))
HIBERNATE_N = int(os.environ.get("HIBERNATE_N", "8000"))

MODES = [
    "regex-bad", "regex-fixed",
    "spin-bad", "spin-fixed",
    "hibernate-bad", "hibernate-fixed",
]

# per-bug CSV shared by its bad/fixed variants (Main.java appends "mode,epoch_ms,cpu_load")
BUG_CSV = {
    "regex": "regex_cpu.csv",
    "spin": "spin_cpu.csv",
    "hibernate": "hibernate_cpu.csv",
}
MODE_BUG = {
    "regex-bad": "regex", "regex-fixed": "regex",
    "spin-bad": "spin", "spin-fixed": "spin",
    "hibernate-bad": "hibernate", "hibernate-fixed": "hibernate",
}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build():
    print("building jar (mvn -q package)...", flush=True)
    r = sh(["mvn", "-q", "package"], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"mvn package failed:\n{r.stdout}\n{r.stderr}")
    if not os.path.exists(JAR):
        sys.exit(f"expected jar not found at {JAR}")
    print("  ok", flush=True)


def reset_results():
    """Fresh run each time: drop previously accumulated CSVs/flames so results/ only
    ever contains data from the most recent real run (Main.java appends, so stale
    rows from a prior run would otherwise linger)."""
    os.makedirs(RESULTS, exist_ok=True)
    for name in list(BUG_CSV.values()) + ["throughput.csv"]:
        p = os.path.join(RESULTS, name)
        if os.path.exists(p):
            os.remove(p)
    for mode in MODES:
        for pat in (f"flame-{mode}.html", f"stdout_{mode}.txt"):
            p = os.path.join(RESULTS, pat)
            if os.path.exists(p):
                os.remove(p)


def run_mode(mode):
    print(f"\n== {mode} ==", flush=True)
    env = dict(os.environ)
    env["RESULTS_DIR"] = RESULTS
    cmd = [
        "java",
        f"-Dlab.durationSec={WORKLOAD_DURATION_SEC}",
        f"-Dlab.hibernateN={HIBERNATE_N}",
        "-cp", JAR,
        "lab.Main", mode,
    ]
    stdout_path = os.path.join(RESULTS, f"stdout_{mode}.txt")
    flame_path = os.path.join(RESULTS, f"flame-{mode}.html")

    with open(stdout_path, "w") as out:
        proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=out, stderr=subprocess.STDOUT)
        print(f"  pid={proc.pid}  waiting {WARMUP_SEC}s warmup...", flush=True)
        time.sleep(WARMUP_SEC)

        if proc.poll() is not None:
            sys.exit(f"[{mode}] java process exited early (code={proc.returncode}); see {stdout_path}")

        asprof_cmd = ["asprof", "-d", str(PROFILE_DURATION_SEC), "-e", "cpu", "-f", flame_path, str(proc.pid)]
        print(f"  profiling {PROFILE_DURATION_SEC}s: {' '.join(asprof_cmd)}", flush=True)
        r = sh(asprof_cmd)
        if r.returncode != 0:
            print(f"  WARNING: asprof exited {r.returncode}\n{r.stdout}\n{r.stderr}", flush=True)
        else:
            print(f"  asprof: {r.stdout.strip()}", flush=True)

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print(f"  WARNING: {mode} still running after grace period, killing pid={proc.pid}", flush=True)
            proc.kill()
            proc.wait()

    if proc.returncode != 0:
        print(f"  WARNING: {mode} exited with code {proc.returncode}, see {stdout_path}", flush=True)

    verify_flame(mode, flame_path)
    return flame_path


def verify_flame(mode, path):
    """Sanity-check the flame graph actually contains captured sample data before
    moving on -- fail loudly rather than silently shipping an empty profile."""
    if not os.path.exists(path):
        sys.exit(f"[{mode}] flame graph missing: {path}")
    size = os.path.getsize(path)
    with open(path, "r", errors="replace") as f:
        content = f.read()
    has_cpool = "const cpool = [" in content
    m = re.search(r"\nn\(3,(\d+)\)", content)
    total_samples = int(m.group(1)) if m else None
    if size < 5000 or not has_cpool or not total_samples:
        sys.exit(f"[{mode}] flame graph looks empty (size={size}, cpool={has_cpool}, "
                  f"total_width={total_samples}): {path}")
    print(f"  flame graph ok: {size} bytes, root sample-width={total_samples}", flush=True)


def read_throughput():
    path = os.path.join(RESULTS, "throughput.csv")
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for row in csv.DictReader(f):
            rows[row["mode"]] = row
    return rows


def read_cpu_stats():
    stats = {}
    for bug, fname in BUG_CSV.items():
        path = os.path.join(RESULTS, fname)
        if not os.path.exists(path):
            continue
        per_mode = {}
        with open(path) as f:
            for row in csv.DictReader(f):
                per_mode.setdefault(row["mode"], []).append(float(row["cpu_load"]))
        for mode, vals in per_mode.items():
            # first sample is taken ~immediately after JVM start, before the OS has
            # enough elapsed time to compute a rate -- it's reliably 0.0 and not
            # representative, so drop it from the average (still shown in the raw CSV)
            usable = vals[1:] if len(vals) > 1 else vals
            stats[mode] = {
                "avg_cpu_pct": round(100.0 * statistics.mean(usable), 2) if usable else 0.0,
                "max_cpu_pct": round(100.0 * max(usable), 2) if usable else 0.0,
                "samples": len(vals),
            }
    return stats


def java_version():
    r = sh(["java", "-version"])
    return (r.stderr or r.stdout).strip().replace("\n", " | ")


def asprof_version():
    r = sh(["asprof", "--version"])
    return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "unknown"


def maven_version():
    r = sh(["mvn", "-version"])
    return (r.stdout or r.stderr).strip().splitlines()[0]


def write_run_metadata():
    meta = [
        ("timestamp_utc", datetime.now(timezone.utc).isoformat()),
        ("java_version", java_version()),
        ("async_profiler_version", asprof_version()),
        ("maven_version", maven_version()),
        ("host_os", platform.system()),
        ("host_os_release", platform.release()),
        ("host_arch", platform.machine()),
        ("workload_duration_sec", WORKLOAD_DURATION_SEC),
        ("profile_duration_sec", PROFILE_DURATION_SEC),
        ("warmup_sec", WARMUP_SEC),
        ("hibernate_n", HIBERNATE_N),
        ("modes", ";".join(MODES)),
    ]
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerows(meta)


BUG_LABEL = {
    "regex": "Bug 1: unanchored regex (.*ERROR.* backtracking)",
    "spin": "Bug 2: busy-spin queue.poll()",
    "hibernate": "Bug 3: Hibernate AUTO-flush dirty-check scan",
}
BUG_ORDER = ["regex", "spin", "hibernate"]
THROUGHPUT_LABEL = {
    "regex": "lines/sec",
    "spin": "polls/sec",
    "hibernate": "checks/sec",
}
TAKEAWAY = {
    "regex": "unanchored .* forces the backtracking engine to retry every offset on\n"
             "  every non-matching line -- anchoring/contains() turns an O(n^2)-ish scan into O(n).",
    "spin": "a non-blocking poll() in a hot loop burns a full core per idle consumer thread;\n"
            "  poll(timeout) parks the thread and lets the OS wake it only when there's work.",
    "hibernate": "with FlushMode.AUTO, every single query first dirty-checks the WHOLE\n"
                 "  persistence context (O(N) per call) before running a 1-row lookup;\n"
                 "  FlushMode.COMMIT skips that scan and the query goes back to being O(1).",
}


def write_summary(cpu_stats, throughput):
    lines = []
    W = 78
    lines.append("=" * W)
    lines.append("Java high-CPU debugging lab: 3 bugs, bad vs fixed")
    lines.append("=" * W)
    lines.append(f"workload duration: {WORKLOAD_DURATION_SEC}s   profile window: {PROFILE_DURATION_SEC}s   "
                 f"hibernate N: {HIBERNATE_N}")
    lines.append("laptop numbers demonstrating the mechanism, not capacity planning.")
    lines.append("")

    def fmt_ops(row):
        if not row:
            return "n/a"
        try:
            return f"{float(row['ops_per_sec']):,.1f}"
        except (KeyError, ValueError):
            return "n/a"

    for bug in BUG_ORDER:
        bad, fixed = f"{bug}-bad", f"{bug}-fixed"
        lines.append("-" * W)
        lines.append(BUG_LABEL[bug])
        lines.append("-" * W)
        cb, cf = cpu_stats.get(bad, {}), cpu_stats.get(fixed, {})
        tb, tf = throughput.get(bad, {}), throughput.get(fixed, {})
        lines.append(f"  {'variant':10} {'avg_cpu%':>10} {'max_cpu%':>10} {'throughput (' + THROUGHPUT_LABEL[bug] + ')':>26}")
        lines.append(f"  {'bad':10} {cb.get('avg_cpu_pct', 'n/a'):>10} {cb.get('max_cpu_pct', 'n/a'):>10} {fmt_ops(tb):>26}")
        lines.append(f"  {'fixed':10} {cf.get('avg_cpu_pct', 'n/a'):>10} {cf.get('max_cpu_pct', 'n/a'):>10} {fmt_ops(tf):>26}")
        if bug == "spin":
            # for the spin bug, a higher poll rate is NOT "better" -- fixed intentionally
            # polls far less often (it blocks). The story here is CPU burned, not throughput.
            try:
                cpu_ratio = float(cb["avg_cpu_pct"]) / max(float(cf["avg_cpu_pct"]), 0.01)
                lines.append(f"  bad burns {cpu_ratio:,.0f}x the CPU of fixed for the same (near-empty) queue")
            except (KeyError, ZeroDivisionError, TypeError):
                pass
        elif tb and tf:
            try:
                ratio = float(tf["ops_per_sec"]) / max(float(tb["ops_per_sec"]), 1e-9)
                lines.append(f"  fixed does {ratio:,.1f}x the throughput of bad")
            except (ValueError, ZeroDivisionError):
                pass
        lines.append(f"  takeaway: {TAKEAWAY[bug]}")
        lines.append("")

    lines.append("-" * W)
    lines.append("artifacts: flame-<mode>.html (6), <bug>_cpu.csv (3), throughput.csv, run_metadata.csv")
    txt = "\n".join(lines) + "\n"
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(txt)
    print("\n" + txt)


def main():
    build()
    reset_results()

    print(f"results dir: {RESULTS}")
    print(f"workload_duration={WORKLOAD_DURATION_SEC}s profile_duration={PROFILE_DURATION_SEC}s "
          f"warmup={WARMUP_SEC}s hibernate_n={HIBERNATE_N}")

    for mode in MODES:
        run_mode(mode)

    write_run_metadata()
    cpu_stats = read_cpu_stats()
    throughput = read_throughput()
    write_summary(cpu_stats, throughput)
    print(f"\ndone. artifacts in {RESULTS}")


if __name__ == "__main__":
    main()
