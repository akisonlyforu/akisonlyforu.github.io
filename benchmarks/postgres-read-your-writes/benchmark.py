#!/usr/bin/env python3
"""Measure PostgreSQL streaming-replication lag and the LSN gate that fixes it.

An app that writes to the primary and then immediately reads from a read-replica
can get a STALE read: the row it just wrote has not been replayed on the replica
yet. The fix is read-your-writes routing gated on the WAL LSN: capture the
primary's pg_current_wal_lsn() at write time, and before serving a read from the
replica check pg_last_wal_replay_lsn() >= that LSN. Caught up -> read the replica.
Behind -> fall back to the primary.

The replica runs with recovery_min_apply_delay=250ms, so the lag is deterministic
and the stale window is reproducible instead of lumpy.

Two experiments against a real primary + streaming replica:
  A. The stale window - sweep read-after-write delay D, measure stale%. A cliff.
  B. The gate         - same op stream, NAIVE (always replica) vs GATED (LSN gate).

Env: PRIMARY_HOST/PRIMARY_PORT (127.0.0.1/55442), REPLICA_HOST/REPLICA_PORT
(127.0.0.1/55444), RESULTS_DIR, SEED (1234).
"""
import csv
import os
import platform
import random
import statistics
import subprocess
import time
from datetime import datetime, timezone

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
PRIMARY_HOST = os.environ.get("PRIMARY_HOST", "127.0.0.1")
PRIMARY_PORT = int(os.environ.get("PRIMARY_PORT", "55442"))
REPLICA_HOST = os.environ.get("REPLICA_HOST", "127.0.0.1")
REPLICA_PORT = int(os.environ.get("REPLICA_PORT", "55444"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
SEED = int(os.environ.get("SEED", "1234"))

DB, USER, PW = "ryw_bench", "bench", "bench"
APPLY_DELAY = "250ms"

# Experiment A
A_USERS = 1000
A_TRIALS = 300
A_DELAYS_MS = [0, 50, 100, 200, 250, 300, 500, 1000]

# Experiment B
B_USERS = 300
B_HOT = 10            # writes only touch users [0, B_HOT); browse reads hit anyone
B_OPS = 4000
B_P_WRITE = 0.40      # fraction of ops that are writes
B_P_OWNREAD = 0.85    # a write is usually followed by an own-recent read


def connect(host, port):
    conn = psycopg2.connect(host=host, port=port, dbname=DB, user=USER, password=PW)
    conn.autocommit = True
    return conn


def q1(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row[0] if row else None


def exe(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def wait_for(host, port, timeout=120):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            c = connect(host, port)
            q1(c, "SELECT 1")
            c.close()
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1)
    raise RuntimeError(f"{host}:{port} not ready: {last}")


def verify_streaming(prim, repl):
    in_recovery = q1(repl, "SELECT pg_is_in_recovery()")
    state = q1(prim, "SELECT state FROM pg_stat_replication LIMIT 1")
    n = q1(prim, "SELECT count(*) FROM pg_stat_replication")
    print("  replica pg_is_in_recovery() :", in_recovery)
    print("  primary pg_stat_replication :", n, "walsender(s), state =", state)
    if not in_recovery or state != "streaming" or n < 1:
        raise RuntimeError("replica is not streaming from primary")


def wait_caught_up(prim, repl, timeout=30):
    """Block until the replica has replayed everything committed so far."""
    target = q1(prim, "SELECT pg_current_wal_lsn()")
    end = time.time() + timeout
    while time.time() < end:
        if q1(repl, "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (target,)):
            return
        time.sleep(0.05)
    raise RuntimeError("replica never caught up to primary")


def observed_lag_ms(repl):
    v = q1(repl, "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) * 1000")
    return float(v) if v is not None else None


# --------------------------------------------------------------------------- A
def experiment_a(prim, repl):
    print("=" * 66)
    print("EXPERIMENT A  the stale window (replica apply delay = %s)" % APPLY_DELAY)
    print("=" * 66)
    rng = random.Random(SEED)
    exe(prim, "DROP TABLE IF EXISTS user_counter")
    exe(prim, "CREATE TABLE user_counter (user_id int PRIMARY KEY, val bigint)")
    with prim.cursor() as cur:
        cur.executemany(
            "INSERT INTO user_counter (user_id, val) VALUES (%s, 0)",
            [(i,) for i in range(A_USERS)],
        )
    wait_caught_up(prim, repl)

    counter = 0
    rows = []
    for d in A_DELAYS_MS:
        stale = 0
        lags = []
        for _ in range(A_TRIALS):
            counter += 1
            uid = rng.randrange(A_USERS)
            written = counter
            exe(prim, "UPDATE user_counter SET val = %s WHERE user_id = %s", (written, uid))
            # (LSN of this write is implicit; we just gate on wall-clock delay here)
            if d:
                time.sleep(d / 1000.0)
            replica_val = q1(repl, "SELECT val FROM user_counter WHERE user_id = %s", (uid,))
            if replica_val != written:
                stale += 1
            lag = observed_lag_ms(repl)
            if lag is not None:
                lags.append(lag)
        pct = 100.0 * stale / A_TRIALS
        lag_med = round(statistics.median(lags), 1) if lags else ""
        rows.append((d, A_TRIALS, stale, round(pct, 1), lag_med))
        print(f"  D={d:>5}ms  trials={A_TRIALS}  stale={stale:>3}  "
              f"stale%={pct:5.1f}  median_lag_ms={lag_med}")

    path = os.path.join(RESULTS, "experiment_a_stale_window.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["read_after_write_ms", "trials", "stale", "stale_pct",
                    "replica_lag_ms_observed"])
        w.writerows(rows)
    print("  -> wrote", os.path.relpath(path, HERE))
    return rows


# --------------------------------------------------------------------------- B
def build_op_stream():
    """Deterministic mixed stream. Writes touch only hot users; own-recent reads
    hit the just-written hot user; browse reads hit any user."""
    rng = random.Random(SEED + 1)
    ops = []
    val = 0
    for _ in range(B_OPS):
        if rng.random() < B_P_WRITE:
            uid = rng.randrange(B_HOT)
            val += 1
            ops.append(("write", uid, val))
            if rng.random() < B_P_OWNREAD:
                ops.append(("read_own", uid, None))
        else:
            ops.append(("read_browse", rng.randrange(B_USERS), None))
    return ops


def seed_b(prim, repl):
    exe(prim, "DROP TABLE IF EXISTS user_counter")
    exe(prim, "CREATE TABLE user_counter (user_id int PRIMARY KEY, val bigint)")
    with prim.cursor() as cur:
        cur.executemany(
            "INSERT INTO user_counter (user_id, val) VALUES (%s, 0)",
            [(i,) for i in range(B_USERS)],
        )
    wait_caught_up(prim, repl)


def run_mode(prim, repl, ops, gated):
    truth = {}                      # user_id -> last committed val (ground truth)
    last_lsn = {}                   # user_id -> LSN captured right after its write
    total_reads = stale = replica_served = primary_fallback = 0

    for kind, uid, val in ops:
        if kind == "write":
            exe(prim, "UPDATE user_counter SET val = %s WHERE user_id = %s", (val, uid))
            truth[uid] = val
            if gated:
                last_lsn[uid] = q1(prim, "SELECT pg_current_wal_lsn()")
            continue

        # a read
        total_reads += 1
        true_val = truth.get(uid, 0)     # seeded rows start at 0
        use_replica = True
        if gated and uid in last_lsn:
            caught = q1(repl, "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (last_lsn[uid],))
            use_replica = bool(caught)

        if use_replica:
            served = q1(repl, "SELECT val FROM user_counter WHERE user_id = %s", (uid,))
            replica_served += 1
        else:
            served = q1(prim, "SELECT val FROM user_counter WHERE user_id = %s", (uid,))
            primary_fallback += 1

        if served != true_val:
            stale += 1

    def pct(x):
        return round(100.0 * x / total_reads, 1) if total_reads else 0.0

    return {
        "mode": "gated" if gated else "naive",
        "total_reads": total_reads,
        "stale_reads": stale,
        "stale_pct": pct(stale),
        "replica_served": replica_served,
        "replica_served_pct": pct(replica_served),
        "primary_fallback": primary_fallback,
        "primary_fallback_pct": pct(primary_fallback),
    }


def experiment_b(prim, repl):
    print("\n" + "=" * 66)
    print("EXPERIMENT B  the gate: naive vs LSN-gated routing (same op stream)")
    print("=" * 66)
    ops = build_op_stream()
    reads = sum(1 for o in ops if o[0] != "write")
    print(f"  {len(ops)} ops  ({len(ops) - reads} writes, {reads} reads)  "
          f"users={B_USERS}, hot={B_HOT}")

    seed_b(prim, repl)
    naive = run_mode(prim, repl, ops, gated=False)

    seed_b(prim, repl)
    gated = run_mode(prim, repl, ops, gated=True)

    fields = ["mode", "total_reads", "stale_reads", "stale_pct",
              "replica_served", "replica_served_pct",
              "primary_fallback", "primary_fallback_pct"]
    for r in (naive, gated):
        print(f"  {r['mode']:>5}: reads={r['total_reads']}  "
              f"stale={r['stale_reads']} ({r['stale_pct']}%)  "
              f"replica={r['replica_served']} ({r['replica_served_pct']}%)  "
              f"primary_fallback={r['primary_fallback']} ({r['primary_fallback_pct']}%)")

    path = os.path.join(RESULTS, "experiment_b_gate.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow(naive)
        w.writerow(gated)
    print("  -> wrote", os.path.relpath(path, HERE))
    return naive, gated


# ------------------------------------------------------------------------ meta
def docker_version():
    try:
        return subprocess.check_output(["docker", "--version"], text=True).strip()
    except Exception:                                # noqa: BLE001
        return "unknown"


def main():
    os.makedirs(RESULTS, exist_ok=True)
    print(f"seed={SEED}  primary={PRIMARY_HOST}:{PRIMARY_PORT}  "
          f"replica={REPLICA_HOST}:{REPLICA_PORT}")
    wait_for(PRIMARY_HOST, PRIMARY_PORT)
    wait_for(REPLICA_HOST, REPLICA_PORT)
    prim, repl = connect(PRIMARY_HOST, PRIMARY_PORT), connect(REPLICA_HOST, REPLICA_PORT)

    pg_version = q1(prim, "SHOW server_version").split()[0]
    delay_guc = q1(repl, "SHOW recovery_min_apply_delay")
    print("  postgres:", pg_version, " replica recovery_min_apply_delay:", delay_guc)
    verify_streaming(prim, repl)

    a_rows = experiment_a(prim, repl)
    naive, gated = experiment_b(prim, repl)

    # summary + metadata
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("PostgreSQL read-your-writes / LSN-gated routing benchmark\n")
        f.write(f"postgres {pg_version}, replica recovery_min_apply_delay={delay_guc}, "
                f"seed={SEED}\n\n")
        f.write("EXPERIMENT A  the stale window\n")
        f.write("  read_after_write_ms  trials  stale  stale%  median_lag_ms\n")
        for d, t, s, p, lag in a_rows:
            f.write(f"  {d:>18}  {t:>6}  {s:>5}  {p:>5}  {lag}\n")
        f.write("\nEXPERIMENT B  the gate\n")
        for r in (naive, gated):
            f.write(f"  {r['mode']:>5}: total_reads={r['total_reads']} "
                    f"stale={r['stale_reads']} ({r['stale_pct']}%) "
                    f"replica_served={r['replica_served']} ({r['replica_served_pct']}%) "
                    f"primary_fallback={r['primary_fallback']} ({r['primary_fallback_pct']}%)\n")

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["postgres_version", pg_version])
        w.writerow(["image_digest",
                    "sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20"])
        w.writerow(["recovery_min_apply_delay", delay_guc])
        w.writerow(["a_users", A_USERS])
        w.writerow(["a_trials_per_delay", A_TRIALS])
        w.writerow(["a_delays_ms", "|".join(str(x) for x in A_DELAYS_MS)])
        w.writerow(["b_users", B_USERS])
        w.writerow(["b_hot_users", B_HOT])
        w.writerow(["b_ops", B_OPS])
        w.writerow(["seed", SEED])
        w.writerow(["docker_version", docker_version()])
        w.writerow(["python_version", platform.python_version()])
        w.writerow(["run_utc", datetime.now(timezone.utc).isoformat()])

    print("\n  done. artifacts in", os.path.relpath(RESULTS, HERE) + "/")


if __name__ == "__main__":
    main()
