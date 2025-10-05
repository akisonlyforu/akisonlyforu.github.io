"""Measure what a PgBouncer connection pooler buys you in front of Postgres.

Postgres forks a full backend process (and does auth) for every new connection,
and it has a hard `max_connections` ceiling. A pooler in transaction mode keeps a
small warm set of server backends and multiplexes many short-lived client
connections over them. This harness runs the SAME workload twice, once straight
at Postgres and once through PgBouncer, and captures the difference.

Three experiments, each run WITHOUT (direct to Postgres) and WITH (via PgBouncer):
  A. Short-lived connection churn - open/query/close, fixed units, measure
     latency + throughput. The pooler should win by skipping the per-request
     backend fork + auth.
  B. Connection exhaustion  - a burst well above max_connections. Direct, a chunk
     fails with "too many clients"; through the pooler, clients queue and 0 fail.
  C. Backend count + memory  - hold N clients doing brief work, sample how many
     real Postgres backends exist and their total RSS. Direct ~= N; through the
     pooler it stays near default_pool_size.

Everything is env-configurable. Ports default to the compose loopback mappings.

Env:
  PG_HOST (127.0.0.1)
  DIRECT_PORT (55432)  - Postgres, the WITHOUT target
  POOLER_PORT (56432)  - PgBouncer, the WITH target
  PG_DB / PG_USER / PG_PASSWORD (bench / bench / bench)
  UNITS (5000)         - experiment A workload units
  A_CONCURRENCY (20)   - experiment A worker threads
  B_CONCURRENCY (100)  - experiment B burst size (well above max_connections)
  C_HOLD (20)          - experiment C concurrent held clients (<= max_connections)
  PG_CONTAINER (pgbouncer_bench_postgres) - for the `ps` RSS sample
  RESULTS_DIR (./results)
"""
import csv
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2

HOST = os.environ.get("PG_HOST", "127.0.0.1")
DIRECT_PORT = int(os.environ.get("DIRECT_PORT", "55432"))
POOLER_PORT = int(os.environ.get("POOLER_PORT", "56432"))
DB = os.environ.get("PG_DB", "bench")
USER = os.environ.get("PG_USER", "bench")
PASSWORD = os.environ.get("PG_PASSWORD", "bench")
UNITS = int(os.environ.get("UNITS", "5000"))
# Kept safely under max_connections: experiment A measures per-connection latency,
# not exhaustion (that is experiment B's job), so the direct case must not just error.
A_CONCURRENCY = int(os.environ.get("A_CONCURRENCY", "15"))
B_CONCURRENCY = int(os.environ.get("B_CONCURRENCY", "100"))
C_HOLD = int(os.environ.get("C_HOLD", "20"))
PG_CONTAINER = os.environ.get("PG_CONTAINER", "pgbouncer_bench_postgres")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

# The two targets every experiment is run against.
CASES = [("without", DIRECT_PORT), ("with", POOLER_PORT)]

# One tiny query per unit of work. No server-side prepared statements, no
# session state - correct for transaction pooling.
QUERY = "SELECT note FROM bench_seed WHERE id = 1"


def connect(port, timeout=10):
    return psycopg2.connect(host=HOST, port=port, dbname=DB, user=USER,
                            password=PASSWORD, connect_timeout=timeout)


def percentile(sorted_vals, q):
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(q / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


# --------------------------------------------------------------------------- A
def unit_once(port):
    """One workload unit: open a NEW connection, run one tiny query, close."""
    t0 = time.perf_counter()
    conn = connect(port)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(QUERY)
            cur.fetchone()
    finally:
        conn.close()
    return (time.perf_counter() - t0) * 1000.0  # ms


def experiment_a(port):
    latencies = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=A_CONCURRENCY) as pool:
        futures = [pool.submit(unit_once, port) for _ in range(UNITS)]
        for f in as_completed(futures):
            latencies.append(f.result())
    wall = time.perf_counter() - t0
    latencies.sort()
    return {
        "units": UNITS,
        "wall_s": wall,
        "throughput": UNITS / wall,
        "mean_ms": sum(latencies) / len(latencies),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "latencies": latencies,
    }


# --------------------------------------------------------------------------- B
def burst_worker(port, barrier):
    """Line every worker up on a barrier so the connects hit simultaneously,
    then do a brief unit of work that keeps the connection busy long enough to
    overlap with its peers."""
    try:
        barrier.wait(timeout=30)
    except threading.BrokenBarrierError:
        pass
    try:
        conn = connect(port, timeout=15)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_sleep(0.05)")
                cur.fetchone()
        finally:
            conn.close()
        return (True, "")
    except Exception as e:  # noqa: BLE001 - we want the verbatim message
        return (False, str(e).strip())


def experiment_b(port):
    barrier = threading.Barrier(B_CONCURRENCY)
    outcomes = []
    with ThreadPoolExecutor(max_workers=B_CONCURRENCY) as pool:
        futures = [pool.submit(burst_worker, port, barrier) for _ in range(B_CONCURRENCY)]
        for f in as_completed(futures):
            outcomes.append(f.result())
    ok = sum(1 for s, _ in outcomes if s)
    fail = len(outcomes) - ok
    errors = [e for s, e in outcomes if not s]
    return {
        "burst": B_CONCURRENCY,
        "success": ok,
        "fail": fail,
        "error_rate_pct": 100.0 * fail / len(outcomes),
        "sample_error": errors[0] if errors else "",
        "outcomes": outcomes,
    }


# --------------------------------------------------------------------------- C
def sample_backends(admin):
    """Client backends on Postgres right now, excluding our own admin session."""
    with admin.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()"
        )
        return cur.fetchone()[0]


def sample_rss_kb():
    """Sum RSS (KB) of Postgres backend processes inside the container via ps."""
    cmd = [
        "docker", "exec", PG_CONTAINER, "sh", "-c",
        "ps -o rss,args | grep '[p]ostgres:' | awk '{s+=$1} END {print s+0}'",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return int(out.stdout.strip() or "0")
    except Exception:  # noqa: BLE001
        return -1


def hold_worker(port, start_evt, stop_evt, connected):
    """Hold a client connection open and run a brief active query, overlapping
    with peers, so we can sample real backend usage while they run. Resilient:
    a failed connect is recorded, not raised, so one exhausted slot doesn't crash
    the run."""
    try:
        conn = connect(port, timeout=20)
    except Exception:  # noqa: BLE001
        return
    connected.append(1)
    try:
        conn.autocommit = True
        start_evt.wait()
        with conn.cursor() as cur:
            # A short sleep so all held clients are trying to be active at once.
            # Direct: one real backend each. Pooled: capped at default_pool_size,
            # the rest queue inside PgBouncer.
            cur.execute("SELECT pg_sleep(2.0)")
            cur.fetchone()
        stop_evt.wait(timeout=30)
    finally:
        conn.close()


def experiment_c(port):
    # The admin/sampling connection ALWAYS goes direct to Postgres: pg_stat_activity
    # lives on Postgres, and we don't want it eating a pool slot in the WITH case.
    admin = connect(DIRECT_PORT)
    admin.autocommit = True
    start_evt = threading.Event()
    stop_evt = threading.Event()
    connected = []
    threads = [threading.Thread(target=hold_worker,
                                args=(port, start_evt, stop_evt, connected))
               for _ in range(C_HOLD)]
    for t in threads:
        t.start()
    time.sleep(0.5)          # let all clients connect
    start_evt.set()          # fire the active window
    time.sleep(0.7)          # let backends ramp up mid-sleep
    backends = sample_backends(admin)
    rss = sample_rss_kb()
    stop_evt.set()
    for t in threads:
        t.join()
    admin.close()
    return {"held_clients": len(connected), "backends": backends, "rss_kb": rss}


def wait_drain(threshold=0, timeout=15):
    """Block until Postgres client backends (excluding our own probe) fall to
    `threshold`, so residual connections from a prior experiment don't pollute
    the next measurement on this low max_connections box."""
    end = time.time() + timeout
    admin = connect(DIRECT_PORT)
    admin.autocommit = True
    try:
        while time.time() < end:
            if sample_backends(admin) <= threshold:
                return
            time.sleep(0.2)
    finally:
        admin.close()


# ------------------------------------------------------------------------- run
def main():
    os.makedirs(RESULTS, exist_ok=True)

    # Warm up + grab versions and knobs from a live direct connection.
    admin = connect(DIRECT_PORT)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SHOW server_version")
        pg_version = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_conn = cur.fetchone()[0]
    admin.close()

    pgb_version = pgbouncer_version()

    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    # Run each case fully before the next: all WITHOUT experiments happen before
    # PgBouncer ever touches Postgres, so the direct-phase measurements can't be
    # polluted by the pooler's warm server pool. Drain to a clean slate between
    # experiments so residual backends don't spill into the next one.
    a, b, c = {}, {}, {}
    for name, port in CASES:
        wait_drain()
        a[name] = experiment_a(port)
        wait_drain()
        b[name] = experiment_b(port)
        wait_drain()
        c[name] = experiment_c(port)

    # ---- A
    out("=" * 62)
    out("EXPERIMENT A  short-lived connection latency & throughput")
    out("=" * 62)
    out(f"  {UNITS} units (connect -> 1 query -> close), {A_CONCURRENCY} workers")
    out(f"  {'':10} {'thru/s':>10} {'mean':>9} {'p50':>8} {'p95':>8} {'p99':>8}")
    for name, _ in CASES:
        r = a[name]
        out(f"  {name:10} {r['throughput']:>10.0f} "
            f"{r['mean_ms']:>8.2f}m {r['p50_ms']:>7.2f}m "
            f"{r['p95_ms']:>7.2f}m {r['p99_ms']:>7.2f}m")
    spd = a["with"]["throughput"] / a["without"]["throughput"]
    out(f"  => pooler throughput is {spd:.1f}x direct, "
        f"p99 {a['without']['p99_ms']:.2f}ms -> {a['with']['p99_ms']:.2f}ms")

    with open(os.path.join(RESULTS, "exp_a_latencies.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "unit_index", "latency_ms"])
        for name, _ in CASES:
            for i, v in enumerate(a[name]["latencies"]):
                w.writerow([name, i, f"{v:.4f}"])

    # ---- B
    out("")
    out("=" * 62)
    out("EXPERIMENT B  connection exhaustion under a burst")
    out("=" * 62)
    out(f"  {B_CONCURRENCY} simultaneous clients vs max_connections={max_conn}")
    for name, _ in CASES:
        r = b[name]
        out(f"  {name:10} success={r['success']:>4}  fail={r['fail']:>4}  "
            f"error_rate={r['error_rate_pct']:>5.1f}%")
        if r["sample_error"]:
            out(f"             error: {r['sample_error']}")
    out("")

    with open(os.path.join(RESULTS, "exp_b_outcomes.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "worker", "status", "error"])
        for name, _ in CASES:
            for i, (ok, err) in enumerate(b[name]["outcomes"]):
                w.writerow([name, i, "ok" if ok else "fail", err])

    # ---- C
    out("=" * 62)
    out("EXPERIMENT C  backend process count & memory footprint")
    out("=" * 62)
    out(f"  holding {C_HOLD} clients doing brief active work, sampled mid-window")
    out(f"  {'':10} {'client backends':>16} {'backend RSS (KB)':>18}")
    for name, _ in CASES:
        r = c[name]
        out(f"  {name:10} {r['backends']:>16} {r['rss_kb']:>18}")
    out(f"  => direct spawns ~{c['without']['backends']} backends; "
        f"pooled stays near default_pool_size ({c['with']['backends']})")

    with open(os.path.join(RESULTS, "exp_c_backends.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "held_clients", "client_backends", "backend_rss_kb"])
        for name, _ in CASES:
            r = c[name]
            w.writerow([name, r["held_clients"], r["backends"], r["rss_kb"]])

    # ---- metadata
    pg_digest = image_digest("postgres:16-alpine")
    pgb_digest = image_digest("edoburu/pgbouncer:latest")
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "postgres_version", "pgbouncer_version",
            "postgres_image_digest", "pgbouncer_image_digest",
            "max_connections", "pool_mode", "default_pool_size", "max_client_conn",
            "units", "a_concurrency", "b_concurrency", "c_hold",
            "a_direct_throughput", "a_pooled_throughput",
            "a_direct_p99_ms", "a_pooled_p99_ms",
            "b_direct_fail", "b_pooled_fail",
            "c_direct_backends", "c_pooled_backends",
            "c_direct_rss_kb", "c_pooled_rss_kb",
        ])
        w.writerow([
            pg_version, pgb_version, pg_digest, pgb_digest,
            max_conn, "transaction", 10, 1000,
            UNITS, A_CONCURRENCY, B_CONCURRENCY, C_HOLD,
            f"{a['without']['throughput']:.1f}", f"{a['with']['throughput']:.1f}",
            f"{a['without']['p99_ms']:.3f}", f"{a['with']['p99_ms']:.3f}",
            b["without"]["fail"], b["with"]["fail"],
            c["without"]["backends"], c["with"]["backends"],
            c["without"]["rss_kb"], c["with"]["rss_kb"],
        ])

    out("")
    out(f"  postgres {pg_version} | pgbouncer {pgb_version} | artifacts in results/")

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def pgbouncer_version():
    try:
        out = subprocess.run(
            ["docker", "exec", "pgbouncer_bench_pgbouncer", "pgbouncer", "--version"],
            capture_output=True, text=True, timeout=15)
        first = out.stdout.strip().splitlines()[0]
        return first.replace("PgBouncer ", "").strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def image_digest(ref):
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    main()
