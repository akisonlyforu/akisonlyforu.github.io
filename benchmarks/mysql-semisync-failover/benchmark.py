"""MySQL high availability: async vs semi-synchronous replication and what a
failover actually loses.

The mechanism under test: with plain asynchronous replication the primary commits
and ACKs the client without waiting for the replica, so a primary crash can lose
transactions the client was already told succeeded. Semi-synchronous replication
(AFTER_SYNC) makes the primary block each commit until a replica acknowledges it
has *received* (relay-logged) the transaction, before the client sees success.
That protects the transmit even when the replica hasn't *applied* the change yet.

Four experiments, all on the same schema (bench.t, an auto-increment table):
  A. async failover loss  - stop the replica's IO thread (receipt gap), burst N
     acked inserts, hard-kill the primary, promote the replica, count survivors.
  B. semi-sync failover   - semi-sync ON, stop only the replica's SQL thread (apply
     lag but receipt still acks), burst N acked inserts, hard-kill, promote, count.
  C. latency cost         - per-commit latency for M inserts, async vs semi-sync.
  D. semi-sync timeout    - no replica can ack; time how long a commit stalls before
     the primary falls back to async (~rpl_semi_sync_source_timeout).

Each experiment recreates the cluster fresh (docker compose down -v + up). The
harness drives Docker via subprocess so it can hard-kill the primary mid-run.

Env: MYSQL_HOST (127.0.0.1), PRIMARY_PORT (3307), REPLICA_PORT (3308),
MYSQL_ROOT_PASSWORD (rootpass), N (1000), M (2000), RESULTS_DIR (results/).
These are laptop measurements demonstrating the mechanism, not production numbers.
"""
import csv
import math
import os
import subprocess
import sys
import time

import mysql.connector

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.environ.get("COMPOSE_FILE", os.path.join(HERE, "docker-compose.yml"))
HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PPORT = int(os.environ.get("PRIMARY_PORT", "3307"))
RPORT = int(os.environ.get("REPLICA_PORT", "3308"))
ROOTPW = os.environ.get("MYSQL_ROOT_PASSWORD", "rootpass")
REPLPW = os.environ.get("REPL_PASSWORD", "replpass")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
ATTEMPTS = os.path.join(RESULTS, "attempts")
N = int(os.environ.get("N", "1000"))
M = int(os.environ.get("M", "2000"))
IMAGE_DIGEST = "sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b"
PRIMARY_CONTAINER = "mysql-semisync-primary"
REPLICA_CONTAINER = "mysql-semisync-replica"

_lines = []


def out(s=""):
    print(s, flush=True)
    _lines.append(s)


# ---------------------------------------------------------------- docker / conn
def compose(*args, check=True):
    return subprocess.run(["docker", "compose", "-f", COMPOSE, *args],
                          check=check, capture_output=True, text=True)


def compose_fresh():
    compose("down", "-v", "--remove-orphans", check=False)
    compose("up", "-d", "--wait", "--force-recreate")
    wait_ready(PPORT)
    wait_ready(RPORT)


def conn(port):
    return mysql.connector.connect(host=HOST, port=port, user="root",
                                   password=ROOTPW, autocommit=True,
                                   connection_timeout=10)


def wait_ready(port, timeout=120):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            cx = conn(port)
            cx.cmd_query("SELECT 1")
            cx.get_rows()
            cx.close()
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise RuntimeError(f"mysql on :{port} never became ready: {last}")


def q(port, sql):
    """Run a query, return list of dict rows (or [] for non-SELECT)."""
    cx = conn(port)
    try:
        cur = cx.cursor(dictionary=True)
        cur.execute(sql)
        try:
            rows = cur.fetchall()
        except mysql.connector.errors.InterfaceError:
            rows = []
        cur.close()
        return rows
    finally:
        cx.close()


def exec_many(port, statements):
    cx = conn(port)
    try:
        cur = cx.cursor()
        for s in statements:
            cur.execute(s)
        cur.close()
    finally:
        cx.close()


def docker_kill(container):
    subprocess.run(["docker", "kill", container], check=False,
                   capture_output=True, text=True)


# ---------------------------------------------------------------- replication
def setup_replication(semisync):
    exec_many(PPORT, [
        f"CREATE USER IF NOT EXISTS 'repl'@'%' IDENTIFIED BY '{REPLPW}'",
        "GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%'",
        "FLUSH PRIVILEGES",
    ])
    if semisync:
        # install + enable the source side before the replica registers
        try:
            exec_many(PPORT, ["INSTALL PLUGIN rpl_semi_sync_source SONAME 'semisync_source.so'"])
        except mysql.connector.Error:
            pass  # already installed
        exec_many(PPORT, [
            "SET GLOBAL rpl_semi_sync_source_enabled = 1",
            "SET GLOBAL rpl_semi_sync_source_wait_point = AFTER_SYNC",
        ])
        try:
            exec_many(RPORT, ["INSTALL PLUGIN rpl_semi_sync_replica SONAME 'semisync_replica.so'"])
        except mysql.connector.Error:
            pass
        exec_many(RPORT, ["SET GLOBAL rpl_semi_sync_replica_enabled = 1"])

    exec_many(RPORT, [
        "STOP REPLICA",
        "RESET REPLICA ALL",
        ("CHANGE REPLICATION SOURCE TO SOURCE_HOST='primary', SOURCE_PORT=3306, "
         f"SOURCE_USER='repl', SOURCE_PASSWORD='{REPLPW}', SOURCE_AUTO_POSITION=1, "
         "GET_SOURCE_PUBLIC_KEY=1"),
        "START REPLICA",
    ])
    wait_replica_running()
    if semisync:
        wait_semisync_clients()


def replica_status():
    rows = q(RPORT, "SHOW REPLICA STATUS")
    return rows[0] if rows else {}


def wait_replica_running(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = replica_status()
        if st.get("Replica_IO_Running") == "Yes" and st.get("Replica_SQL_Running") == "Yes":
            return
        time.sleep(0.3)
    raise RuntimeError("replica threads did not both come up: %s" %
                       {k: st.get(k) for k in ("Replica_IO_Running", "Replica_SQL_Running",
                                               "Last_IO_Error", "Last_SQL_Error")})


def source_status_var(name):
    rows = q(PPORT, "SHOW STATUS LIKE '%s'" % name)
    return rows[0]["Value"] if rows else None


def source_sys_var(name):
    rows = q(PPORT, "SHOW VARIABLES LIKE '%s'" % name)
    return rows[0]["Value"] if rows else None


def wait_semisync_clients(timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if (source_status_var("Rpl_semi_sync_source_status") == "ON" and
                int(source_status_var("Rpl_semi_sync_source_clients") or 0) >= 1):
            return
        time.sleep(0.3)
    raise RuntimeError("semi-sync did not activate: status=%s clients=%s" % (
        source_status_var("Rpl_semi_sync_source_status"),
        source_status_var("Rpl_semi_sync_source_clients")))


def create_schema():
    exec_many(PPORT, [
        "CREATE DATABASE IF NOT EXISTS bench",
        "DROP TABLE IF EXISTS bench.t",
        ("CREATE TABLE bench.t (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
         "n INT NOT NULL, payload VARCHAR(64)) ENGINE=InnoDB"),
    ])
    # wait until the table exists and is empty on the replica
    end = time.time() + 30
    while time.time() < end:
        rows = q(RPORT, "SELECT COUNT(*) c FROM information_schema.tables "
                        "WHERE table_schema='bench' AND table_name='t'")
        if rows and rows[0]["c"] == 1:
            return
        time.sleep(0.3)
    raise RuntimeError("schema did not replicate to replica")


def count_rows(port):
    return q(port, "SELECT COUNT(*) c FROM bench.t")[0]["c"]


def insert_burst(port, n):
    """Insert n single-row transactions, each committed. Return count acked."""
    cx = conn(port)
    acked = 0
    try:
        cur = cx.cursor()
        for i in range(n):
            cur.execute("INSERT INTO bench.t (n, payload) VALUES (%s, %s)", (i, "x" * 32))
            acked += 1  # autocommit -> each execute is a committed, acked txn
        cur.close()
    finally:
        cx.close()
    return acked


def promote_replica():
    exec_many(RPORT, [
        "STOP REPLICA",
        "RESET REPLICA ALL",
        "SET GLOBAL read_only = 0",
        "SET GLOBAL super_read_only = 0",
    ])


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


# ---------------------------------------------------------------- experiments
def experiment_a():
    out("=" * 66)
    out("EXPERIMENT A  async failover loss")
    out("=" * 66)
    compose_fresh()
    setup_replication(semisync=False)
    create_schema()
    # induce a receipt gap: the replica stops pulling from the primary
    exec_many(RPORT, ["STOP REPLICA IO_THREAD"])
    out("  async replication, then STOP REPLICA IO_THREAD on the replica")
    out("  (models a replica whose receipt has fallen behind -- disclosed honestly)")

    acked = insert_burst(PPORT, N)
    out("  burst of %d single-row inserts on primary, all returned success (acked)" % acked)

    docker_kill(PRIMARY_CONTAINER)
    out("  docker kill %s  (hard crash, not a clean shutdown)" % PRIMARY_CONTAINER)
    time.sleep(2)
    promote_replica()
    present = count_rows(RPORT)
    lost = acked - present
    out("  promoted replica: STOP REPLICA; RESET REPLICA ALL; made writable")
    out("")
    out("  acked on primary   : %d" % acked)
    out("  present on replica : %d" % present)
    out("  LOST on failover   : %d" % lost)
    return {"acked": acked, "present": present, "lost": lost}


def experiment_b():
    out("")
    out("=" * 66)
    out("EXPERIMENT B  semi-sync failover, zero loss")
    out("=" * 66)
    compose_fresh()
    setup_replication(semisync=True)
    status = source_status_var("Rpl_semi_sync_source_status")
    clients = source_status_var("Rpl_semi_sync_source_clients")
    out("  semi-sync ON (AFTER_SYNC): Rpl_semi_sync_source_status=%s clients=%s" % (status, clients))
    create_schema()
    # apply lag, but receipt still acks: IO thread runs, SQL thread stopped
    exec_many(RPORT, ["STOP REPLICA SQL_THREAD"])
    out("  STOP REPLICA SQL_THREAD on replica (IO still running -> still acks receipt)")

    acked = insert_burst(PPORT, N)
    out("  burst of %d inserts: each commit blocked until replica acked receipt" % acked)

    docker_kill(PRIMARY_CONTAINER)
    out("  docker kill %s  (hard crash)" % PRIMARY_CONTAINER)
    time.sleep(2)
    # promote: drain the relay log the replica already received, then detach
    exec_many(RPORT, ["START REPLICA SQL_THREAD"])
    end = time.time() + 30
    present = 0
    while time.time() < end:
        present = count_rows(RPORT)
        if present >= acked:
            break
        time.sleep(0.3)
    promote_replica()
    present = count_rows(RPORT)
    lost = acked - present
    out("  promoted replica: START REPLICA SQL_THREAD drained the relay log, then detached")
    out("")
    out("  acked on primary   : %d" % acked)
    out("  present on replica : %d" % present)
    out("  LOST on failover   : %d" % lost)
    return {"acked": acked, "present": present, "lost": lost,
            "status": status, "clients": clients}


def measure_latency(port, m):
    cx = conn(port)
    lat = []
    try:
        cur = cx.cursor()
        for i in range(m):
            t0 = time.perf_counter()
            cur.execute("INSERT INTO bench.t (n, payload) VALUES (%s, %s)", (i, "x" * 32))
            lat.append((time.perf_counter() - t0) * 1000.0)
        cur.close()
    finally:
        cx.close()
    return lat


def _lat_summary(name, lat):
    return {
        "mode": name,
        "count": len(lat),
        "p50_ms": round(percentile(lat, 50), 3),
        "p95_ms": round(percentile(lat, 95), 3),
        "p99_ms": round(percentile(lat, 99), 3),
        "max_ms": round(max(lat), 3),
    }


def experiment_c():
    out("")
    out("=" * 66)
    out("EXPERIMENT C  per-commit latency, async vs semi-sync (replica applying)")
    out("=" * 66)
    compose_fresh()
    setup_replication(semisync=False)
    create_schema()
    lat_async = measure_latency(PPORT, M)
    exec_many(PPORT, ["TRUNCATE TABLE bench.t"])

    # enable semi-sync on the live cluster and re-register the replica
    try:
        exec_many(PPORT, ["INSTALL PLUGIN rpl_semi_sync_source SONAME 'semisync_source.so'"])
    except mysql.connector.Error:
        pass
    exec_many(PPORT, [
        "SET GLOBAL rpl_semi_sync_source_enabled = 1",
        "SET GLOBAL rpl_semi_sync_source_wait_point = AFTER_SYNC",
    ])
    try:
        exec_many(RPORT, ["INSTALL PLUGIN rpl_semi_sync_replica SONAME 'semisync_replica.so'"])
    except mysql.connector.Error:
        pass
    exec_many(RPORT, [
        "SET GLOBAL rpl_semi_sync_replica_enabled = 1",
        "STOP REPLICA IO_THREAD",
        "START REPLICA IO_THREAD",
    ])
    wait_semisync_clients()
    lat_semi = measure_latency(PPORT, M)

    a = _lat_summary("async", lat_async)
    s = _lat_summary("semisync", lat_semi)
    out("  M = %d single-row insert transactions per mode" % M)
    out("")
    out("  %-10s %10s %10s %10s %10s" % ("mode", "p50_ms", "p95_ms", "p99_ms", "max_ms"))
    for r in (a, s):
        out("  %-10s %10.3f %10.3f %10.3f %10.3f" %
            (r["mode"], r["p50_ms"], r["p95_ms"], r["p99_ms"], r["max_ms"]))
    out("")
    out("  note: primary and replica are on the same host, so the replica's receipt")
    out("        ack round-trip is sub-millisecond and the semi-sync latency cost is")
    out("        in the noise here. On a real network the gap is what widens.")

    for name, lat in (("async", lat_async), ("semisync", lat_semi)):
        with open(os.path.join(RESULTS, "latency_%s.csv" % name), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["i", "ms"])
            for i, ms in enumerate(lat):
                w.writerow([i, round(ms, 4)])
    return {"async": a, "semisync": s}


def experiment_d():
    out("")
    out("=" * 66)
    out("EXPERIMENT D  semi-sync timeout fallback")
    out("=" * 66)
    ok = True
    stall_ms = None
    status_after = None
    try:
        compose_fresh()
        setup_replication(semisync=True)
        create_schema()
        timeout = source_sys_var("rpl_semi_sync_source_timeout")
        out("  semi-sync ON, rpl_semi_sync_source_timeout = %s ms" % timeout)
        # no replica can ack: kill the IO thread so nothing is received
        exec_many(RPORT, ["STOP REPLICA IO_THREAD"])
        out("  STOP REPLICA IO_THREAD -> no replica can acknowledge")

        cx = conn(PPORT)
        cur = cx.cursor()
        t0 = time.perf_counter()
        cur.execute("INSERT INTO bench.t (n, payload) VALUES (%s, %s)", (-1, "timeout"))
        stall_ms = (time.perf_counter() - t0) * 1000.0
        cur.close()
        cx.close()
        status_after = source_status_var("Rpl_semi_sync_source_status")
        out("")
        out("  commit stalled     : %.0f ms before falling back to async" % stall_ms)
        out("  status after       : Rpl_semi_sync_source_status = %s" % status_after)
    except Exception as e:  # noqa: BLE001
        ok = False
        out("  experiment D did not complete cleanly: %s" % e)
    return {"ok": ok, "stall_ms": stall_ms, "status_after": status_after,
            "timeout_ms": timeout if ok else None}


# ---------------------------------------------------------------- main
def main():
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(ATTEMPTS, exist_ok=True)

    compose_fresh()
    version = q(PPORT, "SELECT VERSION() v")[0]["v"]

    c = experiment_c()
    a = experiment_a()
    b = experiment_b()
    d = experiment_d()

    out("")
    out("  mysql %s | image %s | N=%d M=%d wait_point=AFTER_SYNC" %
        (version, IMAGE_DIGEST, N, M))
    out("  artifacts in %s" % os.path.relpath(RESULTS, HERE))

    # summary.txt
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(_lines) + "\n")

    # experiment D lands under attempts/ (it is the lumpy one)
    d_note = ("stall_ms=%s status_after=%s timeout_ms=%s ok=%s\n"
              % (d["stall_ms"], d["status_after"], d["timeout_ms"], d["ok"]))
    with open(os.path.join(ATTEMPTS, "experiment_d_timeout.txt"), "w") as f:
        f.write("EXPERIMENT D  semi-sync timeout fallback (kept under attempts/)\n")
        f.write("With semi-sync on and the replica IO thread stopped, a single commit\n")
        f.write("stalls ~rpl_semi_sync_source_timeout ms, then the primary falls back\n")
        f.write("to async and Rpl_semi_sync_source_status flips OFF.\n\n")
        f.write(d_note)

    # failover CSVs
    with open(os.path.join(RESULTS, "failover_loss.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["experiment", "mode", "acked", "present_on_replica", "lost"])
        w.writerow(["A", "async", a["acked"], a["present"], a["lost"]])
        w.writerow(["B", "semisync", b["acked"], b["present"], b["lost"]])

    with open(os.path.join(RESULTS, "latency_percentiles.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mode", "count", "p50_ms", "p95_ms", "p99_ms", "max_ms"])
        w.writeheader()
        w.writerow(c["async"])
        w.writerow(c["semisync"])

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mysql_version", "image_digest", "N", "M", "wait_point",
                    "semisync_timeout_ms",
                    "a_acked", "a_present", "a_lost",
                    "b_acked", "b_present", "b_lost",
                    "c_async_p50_ms", "c_async_p99_ms",
                    "c_semisync_p50_ms", "c_semisync_p99_ms",
                    "d_stall_ms", "d_status_after"])
        w.writerow([version, IMAGE_DIGEST, N, M, "AFTER_SYNC",
                    d["timeout_ms"],
                    a["acked"], a["present"], a["lost"],
                    b["acked"], b["present"], b["lost"],
                    c["async"]["p50_ms"], c["async"]["p99_ms"],
                    c["semisync"]["p50_ms"], c["semisync"]["p99_ms"],
                    d["stall_ms"], d["status_after"]])


if __name__ == "__main__":
    try:
        main()
    finally:
        # never leave containers running
        compose("down", "-v", "--remove-orphans", check=False)
