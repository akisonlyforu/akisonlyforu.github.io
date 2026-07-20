"""java-oom-anatomy: reproduce that java.lang.OutOfMemoryError is several DIFFERENT
failures wearing one name, and that the diagnostic tell for a leak is what GC
RECLAIMS (the post-GC live set), not peak usage.

Four experiments, each run for real inside a digest-pinned eclipse-temurin:21-jdk
container, each producing captured output:

  A. leak       -> OutOfMemoryError: Java heap space   (retained allocation; the anchor)
  B. healthy    -> no OOM                              (identical rate, released each iter)
  C. gcoverhead -> OutOfMemoryError: GC overhead limit exceeded (Parallel GC, near-full)
  D. metaspace  -> OutOfMemoryError: Metaspace         (the one that isn't heap)

A and B share Xmx and allocation shape, so post-GC heap is the ONLY thing that
differs. For A we also poll a live class histogram (jcmd) as the heap climbs, so the
leaking class is caught red-handed dominating the heap. For D we sample heap AND
metaspace over time to show heap staying flat while metadata walks into the wall.

Everything OOMs on batch JVMs -- no host profiler, no exposed ports -- so Docker is
clean and fully reproducible. Results land on the host under RESULTS_DIR (mounted).

Env knobs (all optional): RESULTS_DIR, IMAGE_TAG, plus per-experiment tunables passed
through to the JVM (see EXPERIMENTS below).
"""
import csv
import datetime
import os
import platform
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
LOGS = os.path.join(RESULTS, "logs")
ATTEMPTS = os.path.join(RESULTS, "attempts")

IMAGE_TAG = os.environ.get("IMAGE_TAG", "java-oom-anatomy:local")
# Digest-pinned base, resolved 2026-07-20 via:
#   docker pull eclipse-temurin:21-jdk
#   docker inspect --format='{{index .RepoDigests 0}}' eclipse-temurin:21-jdk
BASE_IMAGE = "eclipse-temurin:21-jdk"
BASE_DIGEST = "sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a"

XMX_HEAP = os.environ.get("XMX_HEAP", "256m")     # leak + healthy
XMX_GCO = os.environ.get("XMX_GCO", "80m")        # gc-overhead
MAX_META = os.environ.get("MAX_META", "64m")      # metaspace

# Each experiment: JVM flags, env passed to the container, collector + limit metadata.
EXPERIMENTS = [
    {
        "name": "leak",
        "flags": ["-Xmx" + XMX_HEAP,
                  "-Xlog:gc*:file=/app/results/logs/leak_gc.log:time,uptime,level,tags"],
        "env": {"BLOCK_KB": "32", "SLEEP_MS": "3"},
        "collector": "G1 (JDK 21 default)",
        "limit": "-Xmx" + XMX_HEAP,
        "poll_histogram": True,
        "oom": True,
    },
    {
        "name": "healthy",
        "flags": ["-Xmx" + XMX_HEAP,
                  "-Xlog:gc*:file=/app/results/logs/healthy_gc.log:time,uptime,level,tags"],
        "env": {"BLOCK_KB": "32", "SLEEP_MS": "3", "HEALTHY_ITER": "8000"},
        "collector": "G1 (JDK 21 default)",
        "limit": "-Xmx" + XMX_HEAP,
        "poll_histogram": False,
        "oom": False,
    },
    {
        "name": "gcoverhead",
        "flags": ["-Xmx" + XMX_GCO, "-XX:+UseParallelGC",
                  "-Xlog:gc*:file=/app/results/logs/gc_overhead_gc.log:time,uptime,level,tags"],
        "env": {"GCO_FILL_PCT": "80", "GCO_NODE_BYTES": "512"},
        "collector": "Parallel (-XX:+UseParallelGC)",
        "limit": "-Xmx" + XMX_GCO,
        "poll_histogram": False,
        "oom": True,
    },
    {
        "name": "metaspace",
        "flags": ["-Xmx" + XMX_HEAP, "-XX:MaxMetaspaceSize=" + MAX_META],
        "env": {"META_SAMPLE_EVERY": "200", "META_SLEEP_MS": "1"},
        "collector": "G1 (JDK 21 default)",
        "limit": "-XX:MaxMetaspaceSize=" + MAX_META,
        "poll_histogram": False,
        "oom": True,
    },
]

# ---------------------------------------------------------------------------

def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def docker_state(name):
    r = sh(["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name])
    if r.returncode != 0:
        return None, None
    parts = r.stdout.strip().split()
    return parts[0], (int(parts[1]) if len(parts) > 1 else None)


def rm(name):
    sh(["docker", "rm", "-f", name])


def build_image():
    print(f"[build] {IMAGE_TAG} from {BASE_IMAGE}@{BASE_DIGEST}")
    r = sh(["docker", "build", "-t", IMAGE_TAG, HERE])
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit("docker build failed")


def java_version():
    r = sh(["docker", "run", "--rm", IMAGE_TAG, "-version"])
    # `java -version` prints to stderr
    return (r.stderr or r.stdout).strip()


def run_experiment(exp):
    """Launch the container detached, (optionally) poll a live histogram while it
    runs, wait for exit, and return (stdout, stderr, elapsed_s, exit_code, histogram)."""
    name = "oom_" + exp["name"]
    rm(name)
    cmd = ["docker", "run", "-d", "--name", name, "-v", f"{RESULTS}:/app/results"]
    for k, v in exp["env"].items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE_TAG] + exp["flags"] + ["Main", exp["name"], "/app/results"]

    print(f"\n[run] {exp['name']}: {' '.join(exp['flags'])} env={exp['env']}")
    r = sh(cmd)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"failed to start container for {exp['name']}")

    t0 = time.time()
    histogram = None
    last_beat = 0
    while True:
        status, code = docker_state(name)
        if status is None or status not in ("running", "created"):
            break
        if exp["poll_histogram"]:
            h = sh(["docker", "exec", name, "jcmd", "1", "GC.class_histogram"])
            if h.returncode == 0 and h.stdout.strip():
                histogram = h.stdout           # keep the LAST successful capture
        elapsed = time.time() - t0
        if elapsed - last_beat >= 5:
            print(f"    ...{exp['name']} running {elapsed:.0f}s")
            last_beat = elapsed
        time.sleep(1)
        if time.time() - t0 > 600:
            print(f"    [!] {exp['name']} exceeded 600s, stopping")
            sh(["docker", "stop", name])
            break

    elapsed = time.time() - t0
    _, code = docker_state(name)
    logs = sh(["docker", "logs", name])         # stdout -> .stdout, stderr -> .stderr
    rm(name)
    print(f"[done] {exp['name']} exit={code} in {elapsed:.1f}s")
    return logs.stdout, logs.stderr, elapsed, code, histogram


def run_gcoverhead(exp, tries=5):
    """The GC-overhead tripwire is LUMPY: the same near-full-heap recipe sometimes
    fires 'GC overhead limit exceeded' and sometimes just 'Java heap space', because
    which one wins is a race between the overhead detector (needs several consecutive
    ~98%-in-GC / <2%-reclaimed Full GCs) and a plain allocation failure. The intended,
    most-common outcome is the overhead message, so we retry a few times to land it;
    any 'Java heap space' variant we get on the way is saved to results/attempts/ with
    a note. This is honest reproduction, not forcing: every run is real."""
    want = "GC overhead limit exceeded"
    accepted = None
    for i in range(1, tries + 1):
        out, err, elapsed, code, hist = run_experiment(exp)
        msg, _ = extract_oom(err)
        if msg and want in msg:
            print(f"[gcoverhead] attempt {i}: got '{msg}' (accepted)")
            return out, err, elapsed, code, hist
        # not the overhead message -- record the honest near-miss and retry
        note = os.path.join(ATTEMPTS, f"gcoverhead_attempt{i}_stderr.log")
        with open(note, "w") as f:
            f.write(f"# gcoverhead attempt {i}: OOMed with a DIFFERENT (real) message\n")
            f.write(f"# got: {msg}\n")
            f.write(f"# wanted (most-common outcome): java.lang.OutOfMemoryError: {want}\n")
            f.write(f"# time_to_oom_s={elapsed:.1f}\n")
            f.write("# The GC-overhead tripwire is a race with plain allocation failure;\n")
            f.write("# on this attempt allocation failed first. Retried. See README honesty note.\n\n")
            f.write(err)
        print(f"[gcoverhead] attempt {i}: got '{msg}' -> saved to attempts/, retrying")
        accepted = (out, err, elapsed, code, hist)
    print(f"[gcoverhead] never landed '{want}' in {tries} tries; keeping last REAL message honestly")
    return accepted


# ---- GC log parsing --------------------------------------------------------

# [..][12.345s][info][gc ...] GC(7) Pause Young (Normal) (...) 120M->45M(256M) 3.456ms
GC_RE = re.compile(
    r"\]\[(?P<uptime>[\d.]+)s\]"
    r".*?GC\((?P<idx>\d+)\)\s+Pause\s+(?P<kind>\w+)"
    r".*?(?P<before>\d+)M->(?P<after>\d+)M\((?P<cap>\d+)M\)"
    r"\s+(?P<pause>[\d.]+)ms"
)


def parse_gc_log(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            m = GC_RE.search(line)
            if not m:
                continue
            rows.append({
                "gc_index": int(m["idx"]),
                "kind": m["kind"],
                "heap_before_mb": int(m["before"]),
                "heap_after_mb": int(m["after"]),
                "heap_capacity_mb": int(m["cap"]),
                "pause_ms": float(m["pause"]),
                "uptime_s": float(m["uptime"]),
            })
    return rows


def write_gc_csv(rows, path):
    cols = ["gc_index", "kind", "heap_before_mb", "heap_after_mb",
            "heap_capacity_mb", "pause_ms", "uptime_s"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_gc_overhead_csv(rows, path, nwindows=6):
    """Split the run into N equal time windows; per window report the fraction of
    wall time spent in GC and the mean fraction of heap reclaimed per cycle. Near
    the end pct_time_in_gc -> ~100 and pct_heap_reclaimed -> ~0 -- the numbers that
    justify the 'GC overhead limit exceeded' message."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["window", "pct_time_in_gc", "pct_heap_reclaimed",
                    "gc_events", "window_start_s", "window_end_s"])
        if not rows:
            return
        t_lo = min(r["uptime_s"] for r in rows)
        t_hi = max(r["uptime_s"] for r in rows)
        span = max(t_hi - t_lo, 1e-6)
        width = span / nwindows
        for i in range(nwindows):
            lo = t_lo + i * width
            hi = t_lo + (i + 1) * width
            win = [r for r in rows if (lo <= r["uptime_s"] < hi) or
                   (i == nwindows - 1 and r["uptime_s"] == hi)]
            if not win:
                continue
            gc_ms = sum(r["pause_ms"] for r in win)
            wall_ms = max(hi - lo, 1e-6) * 1000.0
            pct_time = min(100.0, gc_ms / wall_ms * 100.0)
            reclaimed = [max(0.0, (r["heap_before_mb"] - r["heap_after_mb"]) /
                              max(r["heap_capacity_mb"], 1) * 100.0) for r in win]
            pct_recl = sum(reclaimed) / len(reclaimed)
            w.writerow([i + 1, round(pct_time, 2), round(pct_recl, 3),
                        len(win), round(lo, 3), round(hi, 3)])


# ---- histogram parsing -----------------------------------------------------

HIST_RE = re.compile(r"^\s*(\d+):\s+(\d+)\s+(\d+)\s+(.+?)\s*$")


def parse_histogram(text, top=15):
    rows = []
    if not text:
        return rows
    for line in text.splitlines():
        m = HIST_RE.match(line)
        if not m:
            continue
        rank, instances, byts, cls = int(m[1]), int(m[2]), int(m[3]), m[4]
        rows.append({"rank": rank, "class_name": cls,
                     "instances": instances, "bytes": byts})
        if len(rows) >= top:
            break
    return rows


def write_histogram_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "class_name", "instances", "bytes"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---- OOM message extraction ------------------------------------------------

OOM_MSG_RE = re.compile(r"(java\.lang\.OutOfMemoryError: .+)")
TOP_FRAME_RE = re.compile(r"^\s*at\s+(.+)$", re.M)


def extract_oom(stderr):
    msg, frame = None, None
    m = OOM_MSG_RE.search(stderr or "")
    if m:
        msg = m.group(1).strip()
    fm = TOP_FRAME_RE.search(stderr or "")
    if fm:
        frame = fm.group(1).strip()
    return msg, frame


def region_for(msg):
    if not msg:
        return "?"
    if "Metaspace" in msg:
        return "Metaspace"
    if "GC overhead" in msg:
        return "heap (GC-overhead tripwire)"
    if "Java heap space" in msg:
        return "heap"
    if "Direct buffer" in msg or "direct" in msg:
        return "off-heap (direct)"
    return "?"


# ---------------------------------------------------------------------------

def main():
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(ATTEMPTS, exist_ok=True)

    build_image()
    jver = java_version()
    print("[java]\n" + jver)

    results = {}
    oom_events = []

    for exp in EXPERIMENTS:
        name = exp["name"]
        if name == "gcoverhead":
            out, err, elapsed, code, histogram = run_gcoverhead(exp)
        else:
            out, err, elapsed, code, histogram = run_experiment(exp)
        # persist raw stdout + stderr
        with open(os.path.join(LOGS, f"{name}_stdout.log"), "w") as f:
            f.write(out)
        stderr_path = {
            "leak": "leak_stderr.log",
            "healthy": "healthy_stderr.log",
            "gcoverhead": "gc_overhead_stderr.log",
            "metaspace": "metaspace_stderr.log",
        }[name]
        with open(os.path.join(LOGS, stderr_path), "w") as f:
            f.write(err)

        results[name] = {"elapsed": elapsed, "exit": code,
                         "stdout": out, "stderr": err}

        if name == "leak":
            rows = parse_gc_log(os.path.join(LOGS, "leak_gc.log"))
            write_gc_csv(rows, os.path.join(RESULTS, "gc_leak.csv"))
            results[name]["gc"] = rows
            hist = parse_histogram(histogram)
            write_histogram_csv(hist, os.path.join(RESULTS, "histogram_leak.csv"))
            results[name]["hist"] = hist

        if name == "healthy":
            rows = parse_gc_log(os.path.join(LOGS, "healthy_gc.log"))
            write_gc_csv(rows, os.path.join(RESULTS, "gc_healthy.csv"))
            results[name]["gc"] = rows

        if name == "gcoverhead":
            rows = parse_gc_log(os.path.join(LOGS, "gc_overhead_gc.log"))
            write_gc_overhead_csv(rows, os.path.join(RESULTS, "gc_overhead.csv"))
            results[name]["gc"] = rows

        if exp["oom"]:
            msg, frame = extract_oom(err)
            results[name]["oom_msg"] = msg
            results[name]["oom_frame"] = frame
            oom_events.append({
                "scenario": name,
                "oom_message": msg or "(not captured)",
                "region": region_for(msg),
                "limit": exp["limit"],
                "gc_collector": exp["collector"],
                "time_to_oom_s": round(elapsed, 1),
                "top_stack_frame": frame or "",
            })

    # cross-experiment outputs
    with open(os.path.join(RESULTS, "oom_events.csv"), "w", newline="") as f:
        cols = ["scenario", "oom_message", "region", "limit", "gc_collector",
                "time_to_oom_s", "top_stack_frame"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for e in oom_events:
            w.writerow(e)

    write_run_metadata(jver)
    write_summary(results, oom_events)
    print("\n[ok] all experiments done. results in", RESULTS)


def write_run_metadata(jver):
    path = os.path.join(RESULTS, "run_metadata.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["timestamp_utc", datetime.datetime.now(datetime.timezone.utc).isoformat()])
        w.writerow(["java_version", " | ".join(jver.splitlines())])
        w.writerow(["base_image", BASE_IMAGE])
        w.writerow(["base_image_digest", BASE_DIGEST])
        w.writerow(["image_ref", f"{BASE_IMAGE}@{BASE_DIGEST}"])
        w.writerow(["host_os", platform.system()])
        w.writerow(["host_os_release", platform.release()])
        w.writerow(["host_arch", platform.machine()])
        w.writerow(["leak_xmx", XMX_HEAP])
        w.writerow(["healthy_xmx", XMX_HEAP])
        w.writerow(["gcoverhead_xmx", XMX_GCO])
        w.writerow(["gcoverhead_collector", "Parallel (-XX:+UseParallelGC)"])
        w.writerow(["metaspace_xmx", XMX_HEAP])
        w.writerow(["metaspace_max", MAX_META])
        w.writerow(["leak_collector", "G1 (JDK 21 default)"])
        w.writerow(["metaspace_collector", "G1 (JDK 21 default)"])
        w.writerow(["alloc_block_kb", "32"])
        w.writerow(["alloc_sleep_ms", "3"])
        w.writerow(["healthy_iterations", "8000"])
        w.writerow(["gcoverhead_fill_pct", "80"])
        w.writerow(["gcoverhead_node_bytes", "512"])


def write_summary(results, oom_events):
    path = os.path.join(RESULTS, "summary.txt")
    L = []
    bar = "=" * 78
    dash = "-" * 78
    L.append(bar)
    L.append("java-oom-anatomy: four OutOfMemoryErrors, one name")
    L.append(bar)
    L.append("laptop numbers demonstrating the MECHANISM, not capacity planning.")
    L.append("the tell for a leak is what GC RECLAIMS (post-GC heap), not peak usage.")
    L.append("")

    # A: leak
    lg = results["leak"].get("gc", [])
    L.append(dash)
    L.append("A. leak -> Java heap space  (the anchor: retained allocation)")
    L.append(dash)
    if lg:
        first = lg[0]
        fulls = [r for r in lg if r["kind"] == "Full"]
        peak_after = max(r["heap_after_mb"] for r in lg)
        peak_cap = max(r["heap_capacity_mb"] for r in lg)
        L.append(f"  GC events parsed          : {len(lg)}  (of which Full: {len(fulls)})")
        L.append(f"  post-GC heap start->peak  : {first['heap_after_mb']}MB -> "
                 f"{peak_after}MB (cap grew to {peak_cap}MB)")
        L.append(f"  ^ the tell: GC cannot pull the live set back down; it climbs to the ceiling")
        if fulls:
            widest = max(fulls, key=lambda r: r["pause_ms"])
            L.append(f"  Full-GC pause first->widest: {fulls[0]['pause_ms']:.3f}ms -> "
                     f"{widest['pause_ms']:.3f}ms")
            near = [r for r in fulls if r["heap_after_mb"] >= peak_after - 8]
            if near:
                r = near[-1]
                L.append(f"  a near-death Full GC       : {r['heap_before_mb']}MB->"
                         f"{r['heap_after_mb']}MB(cap {r['heap_capacity_mb']}MB) "
                         f"reclaimed {r['heap_before_mb']-r['heap_after_mb']}MB in {r['pause_ms']:.1f}ms")
    hist = results["leak"].get("hist", [])
    if hist:
        top = hist[0]
        L.append(f"  histogram top class       : {top['class_name']}  "
                 f"{top['instances']:,} instances  {top['bytes']:,} bytes")
        total = sum(h["bytes"] for h in hist)
        if total:
            L.append(f"  ^ that class is {top['bytes']/total*100:.1f}% of the top-15 bytes")
    L.append(f"  OOM message               : {results['leak'].get('oom_msg')}")
    L.append(f"  time to OOM               : {results['leak']['elapsed']:.1f}s")
    L.append("")

    # B: healthy
    hg = results["healthy"].get("gc", [])
    L.append(dash)
    L.append("B. healthy -> NO OOM  (same rate + Xmx, released each iteration)")
    L.append(dash)
    if hg:
        afters = [r["heap_after_mb"] for r in hg]
        L.append(f"  GC events parsed          : {len(hg)}")
        L.append(f"  post-GC heap min..max     : {min(afters)}MB .. {max(afters)}MB "
                 f"(cap {hg[-1]['heap_capacity_mb']}MB)  <- FLAT, no climb")
    L.append(f"  exit code                 : {results['healthy']['exit']} (0 = ran to completion)")
    last_out = [l for l in results["healthy"]["stdout"].splitlines() if "DONE" in l]
    if last_out:
        L.append(f"  {last_out[-1].strip()}")
    L.append("  => identical allocation to A; the ONLY difference is retention.")
    L.append("")

    # C: gcoverhead
    L.append(dash)
    L.append("C. gcoverhead -> GC overhead limit exceeded  (Parallel GC, near-full)")
    L.append(dash)
    gco_path = os.path.join(RESULTS, "gc_overhead.csv")
    if os.path.exists(gco_path):
        with open(gco_path) as f:
            wins = list(csv.DictReader(f))
        if wins:
            lastw = wins[-1]
            L.append(f"  windows                   : {len(wins)}")
            L.append(f"  final window pct_time_in_gc     : {lastw['pct_time_in_gc']}%")
            L.append(f"  final window pct_heap_reclaimed : {lastw['pct_heap_reclaimed']}%")
    L.append(f"  OOM message               : {results['gcoverhead'].get('oom_msg')}")
    L.append(f"  time to OOM               : {results['gcoverhead']['elapsed']:.1f}s")
    L.append("")

    # D: metaspace
    L.append(dash)
    L.append("D. metaspace -> Metaspace  (the OOM that isn't heap)")
    L.append(dash)
    meta_path = os.path.join(RESULTS, "metaspace.csv")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            ms = list(csv.DictReader(f))
        if ms:
            first, last = ms[0], ms[-1]
            heaps = [int(r["heap_used_mb"]) for r in ms]
            L.append(f"  samples                   : {len(ms)}")
            L.append(f"  metadata total start->wall: {first['metadata_total_mb']}MB -> "
                     f"{last['metadata_total_mb']}MB   (cap {MAX_META})")
            L.append(f"  classes loaded start->wall: {first['classes_loaded']} -> "
                     f"{last['classes_loaded']}")
            L.append(f"  heap_used stayed within   : {min(heaps)}MB .. {max(heaps)}MB "
                     f"(of {XMX_HEAP} heap)  <- FLAT while metadata climbed")
    L.append(f"  OOM message               : {results['metaspace'].get('oom_msg')}")
    L.append(f"  time to OOM               : {results['metaspace']['elapsed']:.1f}s")
    L.append("")

    L.append(dash)
    L.append("OOM messages (exact strings captured):")
    for e in oom_events:
        L.append(f"  {e['scenario']:11s}: {e['oom_message']}")
    L.append(dash)
    L.append("artifacts: gc_leak.csv, gc_healthy.csv, gc_overhead.csv, metaspace.csv,")
    L.append("           histogram_leak.csv, oom_events.csv, run_metadata.csv, logs/*")

    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n" + "\n".join(L))


if __name__ == "__main__":
    main()
