#!/usr/bin/env python3
"""Measure what PostgreSQL's transaction isolation levels actually buy you.

READ COMMITTED is the default. It is not a safety guarantee, it is a guarantee
about *torn reads* and nothing else: a naive read-modify-write under RC silently
loses writes, and no error is raised. REPEATABLE READ and SERIALIZABLE do not
make that pattern correct -- they make it *loud*, by aborting one side with
SQLSTATE 40001 and handing you the retry.

Four experiments, all against one real digest-pinned PostgreSQL 17:
  A. Lost update      - N workers read-modify-write one row, no retries, per level.
  B. Read stability   - non-repeatable read + phantom, inside one transaction.
  C. Write skew       - the on-call doctors invariant, RR vs SERIALIZABLE (SSI).
  D. Cost of SER      - retry-on-40001 workload, throughput/latency across levels.

Env: PGHOST (127.0.0.1), PGPORT (55446), PGDATABASE (isobench), PGUSER (bench),
RESULTS_DIR, SEED (1234).
"""
import csv
import os
import platform
import random
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.errorcodes

HERE = os.path.dirname(os.path.abspath(__file__))
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = int(os.environ.get("PGPORT", "55446"))
PGDATABASE = os.environ.get("PGDATABASE", "isobench")
PGUSER = os.environ.get("PGUSER", "bench")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
SEED = int(os.environ.get("SEED", "1234"))

IMAGE_DIGEST = "sha256:a426e44bac0b759c95894d68e1a0ac03ecc20b619f498a91aae373bf06d8508d"
LEVELS = ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]
SERIALIZATION_FAILURE = "40001"

# Experiment A
A_WORKERS = 8
A_ITERS = 50

# Experiment C
C_TRIALS = 200

# Experiment D
D_CONCURRENCY = [2, 4, 8, 16]
D_KEYSPACES = [8, 128]        # small = hot contention, large = mild
D_TXNS_PER_WORKER = 100
D_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------- plumbing
def connect(autocommit=True, level=None):
    conn = psycopg2.connect(host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=PGUSER)
    if level:
        conn.set_session(isolation_level=level, autocommit=False)
    else:
        conn.autocommit = autocommit
    return conn


def q1(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row[0] if row else None


def exe(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def is_serialization_failure(err):
    return getattr(err, "pgcode", None) == SERIALIZATION_FAILURE


def wait_for_pg(timeout=120):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            c = connect()
            q1(c, "SELECT 1")
            c.close()
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"postgres never came up: {last}")


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


# ------------------------------------------------- A. lost update, no retries
def a_reset(admin):
    exe(admin, "DROP TABLE IF EXISTS accounts")
    exe(admin, "CREATE TABLE accounts (id int PRIMARY KEY, balance bigint NOT NULL)")
    exe(admin, "INSERT INTO accounts VALUES (1, 0)")


def a_worker(level, atomic, iters, barrier, out, idx):
    conn = connect(level=level)
    serfail = 0
    other = 0
    barrier.wait()
    for _ in range(iters):
        try:
            if atomic:
                # the correct RC pattern: one statement, the read happens inside
                # the same UPDATE that takes the row lock.
                exe(conn, "UPDATE accounts SET balance = balance + 1 WHERE id = 1")
            else:
                # the naive pattern: read into the app, add in Python, write back.
                bal = q1(conn, "SELECT balance FROM accounts WHERE id = 1")
                exe(conn, "UPDATE accounts SET balance = %s WHERE id = 1", (bal + 1,))
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            if is_serialization_failure(e):
                serfail += 1
            else:
                other += 1
    conn.close()
    out[idx] = (serfail, other)


def experiment_a(admin):
    print("=" * 74)
    print("EXPERIMENT A  lost update: read-modify-write, NO retries")
    print("=" * 74)
    print(f"  {A_WORKERS} workers x {A_ITERS} iterations = {A_WORKERS * A_ITERS} intended increments")
    rows = []
    cases = [(lv, False) for lv in LEVELS] + [("READ COMMITTED", True)]
    for level, atomic in cases:
        a_reset(admin)
        expected = A_WORKERS * A_ITERS
        out = [None] * A_WORKERS
        barrier = threading.Barrier(A_WORKERS)
        threads = [threading.Thread(target=a_worker,
                                    args=(level, atomic, A_ITERS, barrier, out, i))
                   for i in range(A_WORKERS)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        final = q1(admin, "SELECT balance FROM accounts WHERE id = 1")
        serfail = sum(o[0] for o in out)
        other = sum(o[1] for o in out)
        lost = expected - final
        # increments that vanished without any error being raised
        silent = lost - serfail - other
        rows.append({
            "isolation_level": level,
            "pattern": "atomic UPDATE ... = balance + 1" if atomic
                       else "SELECT then UPDATE (read-modify-write)",
            "workers": A_WORKERS,
            "iterations_per_worker": A_ITERS,
            "expected_increments": expected,
            "final_balance": final,
            "lost_updates": lost,
            "silent_lost_updates": silent,
            "serialization_failures": serfail,
            "other_errors": other,
            "wall_seconds": round(elapsed, 3),
            "throughput_txn_per_s": round(expected / elapsed, 1),
        })
        tag = "atomic" if atomic else "naive "
        print(f"  {level:<16} {tag}  final={final:<4} lost={lost:<4} "
              f"silent={silent:<4} 40001={serfail:<4} "
              f"{elapsed:6.2f}s  {expected / elapsed:7.1f} txn/s")
    write_csv("lost_update.csv", rows)
    return rows


# --------------------------------- B. non-repeatable read + phantom, one txn
def b_reset(admin):
    exe(admin, "DROP TABLE IF EXISTS items")
    exe(admin, "CREATE TABLE items (id int PRIMARY KEY, category text NOT NULL, val int NOT NULL)")
    exe(admin, "INSERT INTO items SELECT g, 'widgets', 100 FROM generate_series(1, 10) g")
    exe(admin, "UPDATE items SET val = 100 WHERE id = 1")


def experiment_b(admin):
    print("\n" + "=" * 74)
    print("EXPERIMENT B  non-repeatable read + phantom inside one transaction")
    print("=" * 74)
    rows = []
    for level in ["READ COMMITTED", "REPEATABLE READ"]:
        b_reset(admin)
        reader = connect(level=level)
        writer = connect(autocommit=True)
        try:
            first_val = q1(reader, "SELECT val FROM items WHERE id = 1")
            first_count = q1(reader, "SELECT count(*) FROM items WHERE category = 'widgets'")

            # a second connection commits an UPDATE and an INSERT in between
            exe(writer, "UPDATE items SET val = 999 WHERE id = 1")
            exe(writer, "INSERT INTO items VALUES (11, 'widgets', 500)")

            second_val = q1(reader, "SELECT val FROM items WHERE id = 1")
            second_count = q1(reader, "SELECT count(*) FROM items WHERE category = 'widgets'")
            reader.commit()
        finally:
            reader.close()
            writer.close()
        rows.append({
            "isolation_level": level,
            "first_read_val": first_val,
            "second_read_val": second_val,
            "value_changed": first_val != second_val,
            "first_count": first_count,
            "second_count": second_count,
            "phantom_appeared": first_count != second_count,
        })
        print(f"  {level:<16} val {first_val} -> {second_val}   "
              f"count {first_count} -> {second_count}   "
              f"non-repeatable={first_val != second_val}  "
              f"phantom={first_count != second_count}")
    write_csv("read_stability.csv", rows)
    return rows


# ------------------------------------------------------------- C. write skew
def c_reset(admin):
    exe(admin, "UPDATE doctors SET on_call = true")


def c_worker(level, doctor_id, gate, trials, out):
    """Two doctors, both on call. Each transaction checks 'at least 2 on call'
    then takes itself off call. Barrier-synchronised so both read before either
    writes -- otherwise the second one sees the first one's commit and behaves."""
    conn = connect(level=level)
    aborts = 0
    other = 0
    for _ in range(trials):
        gate.wait()                       # main has reset both doctors to on_call
        ok = False
        try:
            n = q1(conn, "SELECT count(*) FROM doctors WHERE on_call = true")
            ok = n >= 2
        except psycopg2.Error:
            conn.rollback()
        gate.wait()                       # both sides have taken their snapshot
        try:
            if ok:
                exe(conn, "UPDATE doctors SET on_call = false WHERE id = %s", (doctor_id,))
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            if is_serialization_failure(e):
                aborts += 1
            else:
                other += 1
        gate.wait()                       # main inspects the invariant
    conn.close()
    out[doctor_id] = (aborts, other)


def experiment_c(admin):
    print("\n" + "=" * 74)
    print("EXPERIMENT C  write skew: two doctors, 'at least one must stay on call'")
    print("=" * 74)
    exe(admin, "DROP TABLE IF EXISTS doctors")
    exe(admin, "CREATE TABLE doctors (id int PRIMARY KEY, name text, on_call boolean NOT NULL)")
    exe(admin, "INSERT INTO doctors VALUES (1, 'alice', true), (2, 'bob', true)")

    rows = []
    for level in ["REPEATABLE READ", "SERIALIZABLE"]:
        out = {}
        gate = threading.Barrier(3)       # doctor 1, doctor 2, and main
        threads = [threading.Thread(target=c_worker, args=(level, d, gate, C_TRIALS, out))
                   for d in (1, 2)]
        violations = 0
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for _ in range(C_TRIALS):
            c_reset(admin)
            gate.wait()                   # release both readers
            gate.wait()                   # both have read; let them write
            gate.wait()                   # both are done
            if q1(admin, "SELECT count(*) FROM doctors WHERE on_call = true") == 0:
                violations += 1
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        aborts = sum(v[0] for v in out.values())
        other = sum(v[1] for v in out.values())
        rows.append({
            "isolation_level": level,
            "trials": C_TRIALS,
            "zero_on_call_trials": violations,
            "zero_on_call_pct": pct(violations, C_TRIALS),
            "serialization_failures": aborts,
            "other_errors": other,
            "transactions": C_TRIALS * 2,
            "wall_seconds": round(elapsed, 3),
        })
        print(f"  {level:<16} trials={C_TRIALS}  invariant violated (0 on call)="
              f"{violations} ({pct(violations, C_TRIALS)}%)  40001 aborts={aborts}")
    write_csv("write_skew.csv", rows)
    return rows


# ------------------------------------- D. cost of serializable under contention
def d_reset(admin, keyspace):
    exe(admin, "DROP TABLE IF EXISTS counters")
    exe(admin, "CREATE TABLE counters (k int PRIMARY KEY, v bigint NOT NULL)")
    exe(admin, "INSERT INTO counters SELECT g, 0 FROM generate_series(1, %s) g", (keyspace,))


def d_worker(level, keyspace, txns, seed, barrier, out, idx):
    conn = connect(level=level)
    rnd = random.Random(seed)
    committed = 0
    attempts = 0
    exhausted = 0
    lat = []
    barrier.wait()
    for _ in range(txns):
        k = rnd.randrange(1, keyspace + 1)
        t0 = time.perf_counter()
        for _attempt in range(D_MAX_ATTEMPTS):
            attempts += 1
            try:
                v = q1(conn, "SELECT v FROM counters WHERE k = %s", (k,))
                exe(conn, "UPDATE counters SET v = %s WHERE k = %s", (v + 1, k))
                conn.commit()
                committed += 1
                lat.append((time.perf_counter() - t0) * 1000.0)
                break
            except psycopg2.Error as e:
                conn.rollback()
                if not is_serialization_failure(e):
                    raise
        else:
            exhausted += 1
    conn.close()
    out[idx] = (committed, attempts, exhausted, lat)


def experiment_d(admin):
    print("\n" + "=" * 74)
    print("EXPERIMENT D  cost of serializable: retry-on-40001 workload")
    print("=" * 74)
    print(f"  each worker: {D_TXNS_PER_WORKER} read-modify-write txns, "
          f"up to {D_MAX_ATTEMPTS} attempts each")
    print(f"  {'level':<16} {'keys':>5} {'wrk':>4} {'commit':>7} {'att':>6} "
          f"{'retry%':>7} {'giveup':>7} {'txn/s':>9} {'p50ms':>7} {'p99ms':>8}")
    rows = []
    for keyspace in D_KEYSPACES:
        for workers in D_CONCURRENCY:
            for level in LEVELS:
                d_reset(admin, keyspace)
                out = [None] * workers
                barrier = threading.Barrier(workers)
                threads = [threading.Thread(
                    target=d_worker,
                    args=(level, keyspace, D_TXNS_PER_WORKER, SEED + 1000 * i,
                          barrier, out, i)) for i in range(workers)]
                t0 = time.perf_counter()
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                elapsed = time.perf_counter() - t0

                committed = sum(o[0] for o in out)
                attempts = sum(o[1] for o in out)
                exhausted = sum(o[2] for o in out)
                lat = sorted(x for o in out for x in o[3])
                p50 = statistics.median(lat) if lat else 0.0
                p99 = lat[min(len(lat) - 1, int(0.99 * len(lat)))] if lat else 0.0
                total_v = q1(admin, "SELECT sum(v) FROM counters")
                rows.append({
                    "isolation_level": level,
                    "keyspace_rows": keyspace,
                    "workers": workers,
                    "txns_per_worker": D_TXNS_PER_WORKER,
                    "committed_txns": committed,
                    "total_attempts": attempts,
                    # every txn costs one attempt; everything above that is a retry
                    "retries": attempts - (committed + exhausted),
                    "retry_rate_pct": pct(attempts - committed, attempts),
                    "aborted_after_max_retries": exhausted,
                    "wall_seconds": round(elapsed, 3),
                    "throughput_txn_per_s": round(committed / elapsed, 1),
                    "latency_p50_ms": round(p50, 3),
                    "latency_p99_ms": round(p99, 3),
                    "counter_sum": total_v,
                })
                print(f"  {level:<16} {keyspace:>5} {workers:>4} {committed:>7} "
                      f"{attempts:>6} {pct(attempts - committed, attempts):>7} "
                      f"{exhausted:>7} {committed / elapsed:>9.1f} "
                      f"{p50:>7.2f} {p99:>8.2f}")
    write_csv("serializable_cost.csv", rows)
    return rows


# ------------------------------------------------------------------ artifacts
def write_csv(name, rows):
    path = os.path.join(RESULTS, name)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  -> wrote results/{name}")


def docker_version():
    try:
        return subprocess.check_output(["docker", "--version"], text=True).strip()
    except Exception:                                # noqa: BLE001
        return "unknown"


def main():
    os.makedirs(RESULTS, exist_ok=True)
    print(f"seed={SEED}  pg={PGHOST}:{PGPORT}/{PGDATABASE}")
    wait_for_pg()
    admin = connect()
    pg_version = q1(admin, "SHOW server_version").split()[0]
    version_str = q1(admin, "SELECT version()")
    print("  postgres:", version_str, "\n")

    a = experiment_a(admin)
    b = experiment_b(admin)
    c = experiment_c(admin)
    d = experiment_d(admin)

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("PostgreSQL transaction isolation benchmark\n")
        f.write(f"{version_str}\nimage {IMAGE_DIGEST}, seed={SEED}\n\n")

        f.write(f"EXPERIMENT A  lost update ({A_WORKERS} workers x {A_ITERS} iters "
                f"= {A_WORKERS * A_ITERS} intended, NO retries)\n")
        f.write(f"  {'level':<16} {'pattern':<8} {'final':>6} {'lost':>5} {'silent':>7} "
                f"{'40001':>6} {'sec':>6} {'txn/s':>8}\n")
        for r in a:
            pat = "atomic" if r["pattern"].startswith("atomic") else "naive"
            f.write(f"  {r['isolation_level']:<16} {pat:<8} {r['final_balance']:>6} "
                    f"{r['lost_updates']:>5} {r['silent_lost_updates']:>7} "
                    f"{r['serialization_failures']:>6} {r['wall_seconds']:>6} "
                    f"{r['throughput_txn_per_s']:>8}\n")

        f.write("\nEXPERIMENT B  read stability inside one transaction\n")
        for r in b:
            f.write(f"  {r['isolation_level']:<16} val {r['first_read_val']} -> "
                    f"{r['second_read_val']}   count {r['first_count']} -> "
                    f"{r['second_count']}   non_repeatable={r['value_changed']}  "
                    f"phantom={r['phantom_appeared']}\n")

        f.write(f"\nEXPERIMENT C  write skew ({C_TRIALS} trials, 2 concurrent txns each)\n")
        for r in c:
            f.write(f"  {r['isolation_level']:<16} zero_on_call={r['zero_on_call_trials']}"
                    f"/{r['trials']} ({r['zero_on_call_pct']}%)  "
                    f"40001_aborts={r['serialization_failures']}\n")

        f.write(f"\nEXPERIMENT D  cost of serializable "
                f"({D_TXNS_PER_WORKER} txns/worker, retry up to {D_MAX_ATTEMPTS}x)\n")
        f.write(f"  {'level':<16} {'keys':>5} {'wrk':>4} {'commit':>7} {'att':>6} "
                f"{'retry%':>7} {'giveup':>7} {'txn/s':>9} {'p50ms':>7} {'p99ms':>8}\n")
        for r in d:
            f.write(f"  {r['isolation_level']:<16} {r['keyspace_rows']:>5} {r['workers']:>4} "
                    f"{r['committed_txns']:>7} {r['total_attempts']:>6} "
                    f"{r['retry_rate_pct']:>7} {r['aborted_after_max_retries']:>7} "
                    f"{r['throughput_txn_per_s']:>9} {r['latency_p50_ms']:>7} "
                    f"{r['latency_p99_ms']:>8}\n")

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["postgres_version", pg_version])
        w.writerow(["postgres_version_full", version_str])
        w.writerow(["image", "postgres:17"])
        w.writerow(["image_digest", IMAGE_DIGEST])
        w.writerow(["port", PGPORT])
        w.writerow(["a_workers", A_WORKERS])
        w.writerow(["a_iterations_per_worker", A_ITERS])
        w.writerow(["c_trials", C_TRIALS])
        w.writerow(["d_concurrency", "|".join(str(x) for x in D_CONCURRENCY)])
        w.writerow(["d_keyspaces", "|".join(str(x) for x in D_KEYSPACES)])
        w.writerow(["d_txns_per_worker", D_TXNS_PER_WORKER])
        w.writerow(["d_max_attempts", D_MAX_ATTEMPTS])
        w.writerow(["seed", SEED])
        w.writerow(["docker_version", docker_version()])
        w.writerow(["python_version", platform.python_version()])
        w.writerow(["run_utc", datetime.now(timezone.utc).isoformat()])

    admin.close()
    print("\n  done. artifacts in", os.path.relpath(RESULTS, HERE) + "/")


if __name__ == "__main__":
    main()
