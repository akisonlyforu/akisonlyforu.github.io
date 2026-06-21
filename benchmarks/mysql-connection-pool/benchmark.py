"""Scaling database reads: connection-per-client vs a bounded connection pool.

The thesis under test: throwing more concurrent MySQL connections at a read
workload does NOT scale throughput past a small point. A read query that makes
the server do real per-row CPU work saturates the box's cores at a low
concurrency (~core count). Beyond that, every extra concurrent connection is
pure overhead -- context switching, internal mutex/lock contention, buffer-pool
churn -- so throughput DECLINES and tail latency explodes. The fix is not more
connections: it is a small, bounded POOL of backend connections that thousands
of client callers multiplex over. A pool sized near the core count recovers
most of the peak throughput at a fraction of the tail latency.

Three experiments, all read-only, all against the SAME single mysql instance:

  A. the curve (connection-per-client) -- for offered concurrency C in a sweep,
     spawn C worker threads, EACH owning its own dedicated connection, all
     hammering the read query for a fixed window. Record QPS, p50, p99 and the
     server's Threads_connected/Threads_running. The curve rises, peaks near the
     core count, then declines while p99 climbs.
  B. the collapse -- the C=512 row of A, highlighted as the pathological point.
  C. the pool fix -- keep 512 offered client threads, but route every query
     through a bounded pool of P persistent connections (P in a sweep). Each
     client borrows a connection, runs the query, returns it. Measure the same
     window. A pool near the core count beats direct-512 on both QPS and p99.

The read query does a bounded PK-range scan and aggregates a NON-indexed column,
so the server must fetch and evaluate `val` for every row in the range -- real
CPU work, not an index lookup that returns a constant. The scan window is varied
every call so nothing collapses to a cached constant.

Env: MYSQL_HOST (127.0.0.1), MYSQL_PORT (3310), MYSQL_ROOT_PASSWORD (rootpass),
RESULTS_DIR (results/), ROWS (200000), SCAN_W (rows scanned per query),
WINDOW_S (8), WARMUP_S (2), CLIENT_THREADS (512). These are laptop measurements
demonstrating the mechanism, not capacity planning.
"""
import csv
import math
import os
import subprocess
import threading
import time

import mysql.connector

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.environ.get("COMPOSE_FILE", os.path.join(HERE, "docker-compose.yml"))
HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYSQL_PORT", "3310"))
ROOTPW = os.environ.get("MYSQL_ROOT_PASSWORD", "rootpass")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
ATTEMPTS = os.path.join(RESULTS, "attempts")

ROWS = int(os.environ.get("ROWS", "200000"))
SCAN_W = int(os.environ.get("SCAN_W", "20000"))       # rows scanned per query
WINDOW_S = float(os.environ.get("WINDOW_S", "8"))     # measured window per level
WARMUP_S = float(os.environ.get("WARMUP_S", "2"))     # discarded warmup per level
CLIENT_THREADS = int(os.environ.get("CLIENT_THREADS", "512"))
CONC_LEVELS = [int(x) for x in os.environ.get(
    "CONC_LEVELS", "1,2,4,8,16,32,64,128,256,512").split(",")]
POOL_SIZES = [int(x) for x in os.environ.get("POOL_SIZES", "8,16,32,64").split(",")]

IMAGE_DIGEST = "sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b"
CONTAINER = "mysql-connection-pool"

# per-query varying constants (large odd multipliers -> different window each call)
_A = 2246822519
_B = 3266489917

_lines = []


def out(s=""):
    print(s, flush=True)
    _lines.append(s)


# ---------------------------------------------------------------- docker / conn
def compose(*args, check=True):
    return subprocess.run(["docker", "compose", "-f", COMPOSE, *args],
                          check=check, capture_output=True, text=True)


def conn():
    return mysql.connector.connect(host=HOST, port=PORT, user="root",
                                   password=ROOTPW, database="bench",
                                   autocommit=True, connection_timeout=30)


def conn_nodb():
    return mysql.connector.connect(host=HOST, port=PORT, user="root",
                                   password=ROOTPW, autocommit=True,
                                   connection_timeout=30)


def wait_ready(timeout=180):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            cx = conn_nodb()
            cur = cx.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
            cx.close()
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise RuntimeError("mysql on :%d never became ready: %s" % (PORT, last))


def status_var(cur, name):
    cur.execute("SHOW GLOBAL STATUS LIKE %s", (name,))
    r = cur.fetchall()
    return r[0][1] if r else None


def sys_var(cur, name):
    cur.execute("SHOW VARIABLES LIKE %s", (name,))
    r = cur.fetchall()
    return r[0][1] if r else None


def percentile(data, p):
    s = sorted(data)
    if not s:
        return 0.0
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


# ---------------------------------------------------------------- schema / seed
def create_and_seed():
    cx = conn_nodb()
    cur = cx.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS bench")
    cur.execute("USE bench")
    cur.execute("DROP TABLE IF EXISTS reads_test")
    cur.execute(
        "CREATE TABLE reads_test ("
        "  id INT PRIMARY KEY,"
        "  val INT NOT NULL,"           # NON-indexed numeric column
        "  payload VARCHAR(255) NOT NULL"
        ") ENGINE=InnoDB")
    out("  seeding %d rows (val = id*2654435761 %% 100000, deterministic)..." % ROWS)
    batch = 5000
    pad = "x" * 200
    i = 1
    t0 = time.time()
    while i <= ROWS:
        rows = []
        j = i
        hi = min(i + batch, ROWS + 1)
        while j < hi:
            val = (j * 2654435761) % 100000
            rows.append((j, val, "row-%d-%s" % (j, pad)))
            j += 1
        cur.executemany(
            "INSERT INTO reads_test (id, val, payload) VALUES (%s,%s,%s)", rows)
        i = hi
    cur.execute("ANALYZE TABLE reads_test")
    cur.fetchall()
    n = None
    cur.execute("SELECT COUNT(*) FROM reads_test")
    n = cur.fetchone()[0]
    cur.close()
    cx.close()
    out("  seeded %d rows in %.1fs" % (n, time.time() - t0))
    return n


QUERY = ("SELECT id, val FROM reads_test "
         "WHERE id BETWEEN %s AND %s AND val >= %s "
         "ORDER BY val DESC LIMIT 50")


def query_params(worker, it):
    """Deterministic but varied per call: a SCAN_W-wide PK window at a moving
    offset, plus a low, moving val threshold so nearly every row in the window
    matches. That makes ORDER BY val a real filesort over ~SCAN_W rows (val is
    NOT indexed) -- the per-connection CPU + sort-buffer work that turns extra
    concurrency into contention. The moving offset keeps every call distinct so
    nothing collapses to a cached constant."""
    span = max(1, ROWS - SCAN_W)
    start = ((worker * _A + it * _B) % span) + 1
    end = start + SCAN_W
    thr = (it * 40503) % 20000     # low threshold -> large filesort each call
    return (start, end, thr)


def run_one(cur, worker, it):
    cur.execute(QUERY, query_params(worker, it))
    cur.fetchall()


def warm_buffer_pool():
    """Run the workload briefly on a few connections; discard results."""
    out("  warming buffer pool (%d rows, %d-wide scans)..." % (ROWS, SCAN_W))
    stop = time.time() + 4.0
    threads = []

    def _warm(w):
        cx = conn()
        cur = cx.cursor()
        it = 0
        while time.time() < stop:
            run_one(cur, w, it)
            it += 1
        cur.close()
        cx.close()

    for w in range(4):
        t = threading.Thread(target=_warm, args=(w,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def calibrate():
    """Report single-query latency so the scan cost is on the record."""
    cx = conn()
    cur = cx.cursor()
    lat = []
    for it in range(200):
        t0 = time.perf_counter()
        run_one(cur, 7, it)
        lat.append((time.perf_counter() - t0) * 1000.0)
    cur.close()
    cx.close()
    p50 = percentile(lat, 50)
    p99 = percentile(lat, 99)
    out("  calibration: single-query serial latency p50=%.2fms p99=%.2fms "
        "(SCAN_W=%d rows)" % (p50, p99, SCAN_W))
    return p50, p99


# ---------------------------------------------------------------- status monitor
class Monitor(threading.Thread):
    """Samples Threads_connected / Threads_running during a run; keeps the max."""

    def __init__(self):
        super().__init__()
        self.stop_flag = False
        self.max_connected = 0
        self.max_running = 0

    def run(self):
        try:
            cx = conn_nodb()
            cur = cx.cursor()
            while not self.stop_flag:
                c = int(status_var(cur, "Threads_connected") or 0)
                r = int(status_var(cur, "Threads_running") or 0)
                self.max_connected = max(self.max_connected, c)
                self.max_running = max(self.max_running, r)
                time.sleep(0.25)
            cur.close()
            cx.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- experiment A
def run_level_direct(C):
    """C worker threads, each with its OWN dedicated connection, hammer the query
    for WARMUP_S (discarded) + WINDOW_S (measured). Returns (qps, p50, p99,
    threads_connected, threads_running).

    Two phases: b_conn rendezvous once every worker has its connection open, then
    main sets the timing gate and fires `go` so no worker can read a gate of 0."""
    b_conn = threading.Barrier(C + 1)
    go = threading.Event()
    latencies = [None] * C
    counts = [0] * C
    errors = [0] * C
    gate = {"measure_start": 0.0, "measure_end": 0.0, "hard_stop": 0.0}

    def worker(wid):
        cx = conn()
        cur = cx.cursor()
        my_lat = []
        my_count = 0
        my_err = 0
        b_conn.wait()            # phase 1: connection established
        go.wait()                # phase 2: gate populated, start together
        ms = gate["measure_start"]
        me = gate["measure_end"]
        hs = gate["hard_stop"]
        it = 0
        while time.perf_counter() < hs:
            t0 = time.perf_counter()
            try:
                run_one(cur, wid, it)
            except Exception:    # noqa: BLE001
                my_err += 1
                it += 1
                continue
            t1 = time.perf_counter()
            if t0 >= ms and t1 <= me:   # only queries fully inside the window
                my_lat.append((t1 - t0) * 1000.0)
                my_count += 1
            it += 1
        latencies[wid] = my_lat
        counts[wid] = my_count
        errors[wid] = my_err
        cur.close()
        cx.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(C)]
    for t in threads:
        t.start()

    b_conn.wait()                # all C connections are open
    now = time.perf_counter()
    gate["measure_start"] = now + WARMUP_S
    gate["measure_end"] = now + WARMUP_S + WINDOW_S
    gate["hard_stop"] = now + WARMUP_S + WINDOW_S
    go.set()

    mon = Monitor()
    time.sleep(WARMUP_S)         # sample only during the measured window
    mon.start()
    for t in threads:
        t.join()
    mon.stop_flag = True
    mon.join()

    all_lat = [x for sub in latencies if sub for x in sub]
    total = sum(counts)
    qps = total / WINDOW_S
    p50 = percentile(all_lat, 50)
    p99 = percentile(all_lat, 99)
    return qps, p50, p99, mon.max_connected, mon.max_running


def experiment_a():
    out("=" * 70)
    out("EXPERIMENT A  the curve: connection-per-client")
    out("=" * 70)
    out("  each of C worker threads owns its own dedicated MySQL connection")
    out("  window=%.0fs measured after %.0fs warmup, per level" % (WINDOW_S, WARMUP_S))
    out("")
    out("  %6s %10s %9s %9s %12s %10s" %
        ("C", "qps", "p50_ms", "p99_ms", "conns", "running"))
    rows = []
    for C in CONC_LEVELS:
        qps, p50, p99, tc, tr = run_level_direct(C)
        out("  %6d %10.0f %9.2f %9.2f %12d %10d" % (C, qps, p50, p99, tc, tr))
        rows.append({"concurrency": C, "qps": round(qps, 1),
                     "p50_ms": round(p50, 3), "p99_ms": round(p99, 3),
                     "threads_connected": tc, "threads_running": tr})
    return rows


# ---------------------------------------------------------------- experiment C
def run_level_pool(P, client_threads):
    """client_threads worker threads share a bounded pool of P persistent
    connections (a Queue is the semaphore). Client-observed latency includes the
    wait to borrow a connection. Returns (qps, p50, p99)."""
    import queue
    pool = queue.Queue()
    conns = []
    for _ in range(P):
        cx = conn()
        cur = cx.cursor()
        conns.append((cx, cur))
        pool.put((cx, cur))

    b_ready = threading.Barrier(client_threads + 1)
    go = threading.Event()
    latencies = [None] * client_threads
    counts = [0] * client_threads
    gate = {"measure_start": 0.0, "measure_end": 0.0, "hard_stop": 0.0}

    def worker(wid):
        my_lat = []
        my_count = 0
        b_ready.wait()           # phase 1: thread ready
        go.wait()                # phase 2: gate populated
        ms = gate["measure_start"]
        me = gate["measure_end"]
        hs = gate["hard_stop"]
        it = 0
        while time.perf_counter() < hs:
            t0 = time.perf_counter()          # client-observed: includes borrow wait
            cx, cur = pool.get()
            try:
                cur.execute(QUERY, query_params(wid, it))
                cur.fetchall()
            finally:
                pool.put((cx, cur))
            t1 = time.perf_counter()
            if t0 >= ms and t1 <= me:
                my_lat.append((t1 - t0) * 1000.0)
                my_count += 1
            it += 1
        latencies[wid] = my_lat
        counts[wid] = my_count

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(client_threads)]
    for t in threads:
        t.start()

    b_ready.wait()
    now = time.perf_counter()
    gate["measure_start"] = now + WARMUP_S
    gate["measure_end"] = now + WARMUP_S + WINDOW_S
    gate["hard_stop"] = now + WARMUP_S + WINDOW_S
    go.set()

    for t in threads:
        t.join()

    for cx, cur in conns:
        try:
            cur.close()
            cx.close()
        except Exception:  # noqa: BLE001
            pass

    all_lat = [x for sub in latencies if sub for x in sub]
    total = sum(counts)
    qps = total / WINDOW_S
    return qps, percentile(all_lat, 50), percentile(all_lat, 99)


def experiment_c():
    out("")
    out("=" * 70)
    out("EXPERIMENT C  the pool fix: %d client threads over a bounded pool" % CLIENT_THREADS)
    out("=" * 70)
    out("  the SAME %d offered client threads, but only P backend connections" % CLIENT_THREADS)
    out("")
    out("  %6s %10s %10s %9s %9s" %
        ("pool", "clients", "qps", "p50_ms", "p99_ms"))
    rows = []
    for P in POOL_SIZES:
        qps, p50, p99 = run_level_pool(P, CLIENT_THREADS)
        out("  %6d %10d %10.0f %9.2f %9.2f" % (P, CLIENT_THREADS, qps, p50, p99))
        rows.append({"pool_size": P, "client_threads": CLIENT_THREADS,
                     "qps": round(qps, 1), "p50_ms": round(p50, 3),
                     "p99_ms": round(p99, 3)})
    return rows


# ---------------------------------------------------------------- main
def main():
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(ATTEMPTS, exist_ok=True)

    out("bringing up mysql (digest-pinned) ...")
    compose("down", "-v", "--remove-orphans", check=False)
    compose("up", "-d", "--wait", "--force-recreate")
    wait_ready()

    cx = conn_nodb()
    cur = cx.cursor()
    version = None
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    innodb_threads = sys_var(cur, "innodb_read_io_threads")
    cur.execute("SELECT @@version_compile_machine")
    arch = cur.fetchone()[0]
    cur.close()
    cx.close()

    # cores the DB can actually use (respecting the compose cpus limit)
    cores = subprocess.run(
        ["docker", "exec", CONTAINER, "nproc"],
        capture_output=True, text=True).stdout.strip()
    cpu_quota = subprocess.run(
        ["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", CONTAINER],
        capture_output=True, text=True).stdout.strip()
    try:
        effective_cores = int(cpu_quota) / 1e9 if cpu_quota and int(cpu_quota) > 0 else cores
    except ValueError:
        effective_cores = cores
    out("  mysql %s (%s) | container nproc=%s | cpu limit=%s cores" %
        (version, arch, cores, effective_cores))

    n = create_and_seed()
    warm_buffer_pool()
    cal_p50, cal_p99 = calibrate()
    out("")

    a_rows = experiment_a()
    c_rows = experiment_c()

    # ---- analysis -------------------------------------------------------
    peak = max(a_rows, key=lambda r: r["qps"])
    row512 = next((r for r in a_rows if r["concurrency"] == 512), a_rows[-1])
    best_pool = max(c_rows, key=lambda r: r["qps"])

    out("")
    out("=" * 70)
    out("SUMMARY")
    out("=" * 70)
    out("  Exp A peak      : %.0f QPS @ C=%d, p99=%.2fms" %
        (peak["qps"], peak["concurrency"], peak["p99_ms"]))
    out("  Exp B collapse  : C=512 -> %.0f QPS, p99=%.2fms  (the pathological point)" %
        (row512["qps"], row512["p99_ms"]))
    out("  peak/512 QPS    : %.2fx more throughput at the knee than at C=512" %
        (peak["qps"] / row512["qps"] if row512["qps"] else float("nan")))
    out("  512-conn p99    : %.0fx the p99 of the peak" %
        (row512["p99_ms"] / peak["p99_ms"] if peak["p99_ms"] else float("nan")))
    out("  Exp C best pool : %d clients over %d conns -> %.0f QPS, p99=%.2fms" %
        (CLIENT_THREADS, best_pool["pool_size"], best_pool["qps"], best_pool["p99_ms"]))
    out("  pool vs 512     : %.2fx the QPS of direct-512, at %.0fx lower p99" %
        (best_pool["qps"] / row512["qps"] if row512["qps"] else float("nan"),
         row512["p99_ms"] / best_pool["p99_ms"] if best_pool["p99_ms"] else float("nan")))
    out("  pool vs peak    : recovers %.0f%% of peak QPS" %
        (100.0 * best_pool["qps"] / peak["qps"] if peak["qps"] else float("nan")))

    # ---- write artifacts ------------------------------------------------
    with open(os.path.join(RESULTS, "expA_curve.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["concurrency", "qps", "p50_ms",
                                          "p99_ms", "threads_connected"])
        w.writeheader()
        for r in a_rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    with open(os.path.join(RESULTS, "expC_pool.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pool_size", "client_threads", "qps",
                                          "p50_ms", "p99_ms"])
        w.writeheader()
        for r in c_rows:
            w.writerow(r)

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["mysql_version", version])
        w.writerow(["image_digest", IMAGE_DIGEST])
        w.writerow(["arch", arch])
        w.writerow(["container_nproc", cores])
        w.writerow(["cpu_limit_cores", effective_cores])
        w.writerow(["table_rows", n])
        w.writerow(["scan_rows_per_query", SCAN_W])
        w.writerow(["query_shape", QUERY.replace("\n", " ")])
        w.writerow(["calibration_p50_ms", round(cal_p50, 3)])
        w.writerow(["calibration_p99_ms", round(cal_p99, 3)])
        w.writerow(["window_s", WINDOW_S])
        w.writerow(["warmup_s", WARMUP_S])
        w.writerow(["concurrency_levels", ",".join(str(c) for c in CONC_LEVELS)])
        w.writerow(["pool_sizes", ",".join(str(p) for p in POOL_SIZES)])
        w.writerow(["client_threads", CLIENT_THREADS])
        w.writerow(["expA_peak_qps", round(peak["qps"], 1)])
        w.writerow(["expA_peak_concurrency", peak["concurrency"]])
        w.writerow(["expA_peak_p99_ms", peak["p99_ms"]])
        w.writerow(["expA_c512_qps", round(row512["qps"], 1)])
        w.writerow(["expA_c512_p99_ms", row512["p99_ms"]])
        w.writerow(["expC_best_pool_size", best_pool["pool_size"]])
        w.writerow(["expC_best_qps", round(best_pool["qps"], 1)])
        w.writerow(["expC_best_p99_ms", best_pool["p99_ms"]])

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(_lines) + "\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        compose("down", "-v", "--remove-orphans", check=False)
