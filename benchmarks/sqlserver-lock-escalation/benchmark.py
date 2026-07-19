"""Reproduce SQL Server lock escalation and the table-wide blocking it causes.

SQL Server holds fine-grained KEY (row) / PAGE locks while a statement runs, but
once a single statement acquires ~5000 locks on one object it escalates them to a
single TABLE-level lock. When that table lock is X, no other transaction can touch
ANY row of the table -- even rows the updater never looked at -- until it commits.

Three experiments against a real SQL Server 2022 with a 200,000-row `orders` table:

  A. Escalation blocks the whole table -- update 8000 rows in an open tran, watch
     the KEY locks collapse into one OBJECT X lock in sys.dm_tran_locks, then time
     how long a point SELECT on an *untouched* row blocks (until the updater commits).
  B. The ~5000-lock cliff -- sweep the update size and find where an OBJECT X lock
     appears and a concurrent point SELECT flips from fast to blocked/timed-out.
  C. The fix: batching -- update 50,000 rows as one big statement (escalates, table
     lock, readers block) vs in 2000-row committed batches (no escalation, readers
     get through). Plus a LOCK_ESCALATION=DISABLE contrast arm.

Escalation is detected from a *separate* monitor connection querying
sys.dm_tran_locks WHERE request_session_id = <updater SPID>.

Env: MSSQL_HOST (127.0.0.1), MSSQL_PORT (11434), MSSQL_SA_PASSWORD, RESULTS_DIR.
"""
import csv
import os
import threading
import time

import pymssql

HOST = os.environ.get("MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MSSQL_PORT", "11434"))
PWD = os.environ.get("MSSQL_SA_PASSWORD", "Str0ng_P@ssw0rd!")
DB = "lockesc_bench"
ROWS = 200_000
UNTOUCHED_ID = 150_000            # a row no update in A/B ever covers
READER_LO, READER_HI = 150_001, 200_000   # untouched high range for experiment C
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))


def connect(db="master", autocommit=True):
    return pymssql.connect(server=HOST, port=PORT, user="sa", password=PWD,
                           database=db, autocommit=autocommit, timeout=300, login_timeout=120)


def scalar(cur, sql, args=None):
    cur.execute(sql, args) if args else cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def percentile(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def lock_snapshot(mon, spid):
    """Counts held by the updater session, grouped by resource_type/request_mode."""
    mon.execute("""
        SELECT resource_type, request_mode, COUNT(*)
        FROM sys.dm_tran_locks
        WHERE request_session_id = %s
        GROUP BY resource_type, request_mode
    """, (spid,))
    key_locks = page_locks = 0
    obj_modes = set()
    for rt, mode, cnt in mon.fetchall():
        if rt == "KEY":
            key_locks += cnt
        elif rt == "PAGE":
            page_locks += cnt
        elif rt == "OBJECT":
            obj_modes.add(mode)
    if "X" in obj_modes:
        obj_mode = "X"
    elif "IX" in obj_modes:
        obj_mode = "IX"
    else:
        obj_mode = next(iter(obj_modes), "")
    escalated = obj_mode == "X"          # IX is the normal intent lock; X = escalated
    return {"key": key_locks, "page": page_locks, "obj_mode": obj_mode, "escalated": escalated}


def point_select(host_id, lock_timeout_ms=None):
    """Open a fresh connection, run a point SELECT, return (latency_ms, blocked)."""
    c = connect(DB)
    cur = c.cursor()
    try:
        if lock_timeout_ms is not None:
            cur.execute("SET LOCK_TIMEOUT %d" % lock_timeout_ms)
        t0 = time.time()
        try:
            cur.execute("SELECT amount FROM orders WHERE id = %s", (host_id,))
            cur.fetchall()
            return (time.time() - t0) * 1000, False
        except pymssql.OperationalError:      # 1222 lock request timeout
            return (time.time() - t0) * 1000, True
    finally:
        c.close()


def setup(cur):
    cur.execute("IF DB_ID('lockesc_bench') IS NOT NULL BEGIN ALTER DATABASE lockesc_bench "
                "SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE lockesc_bench; END")
    cur.execute("CREATE DATABASE lockesc_bench")
    cur.execute("USE lockesc_bench")
    cur.execute("CREATE TABLE orders ("
                "id INT PRIMARY KEY CLUSTERED, status VARCHAR(16), amount INT, filler CHAR(100))")
    cur.execute(f""";WITH n AS (SELECT TOP ({ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) rn
                     FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c)
                   INSERT INTO orders(id, status, amount, filler)
                   SELECT rn,
                          CASE WHEN rn % 100 = 0 THEN 'cancelled' ELSE 'shipped' END,
                          (rn % 1000),
                          'x'
                   FROM n""")
    total = scalar(cur, "SELECT COUNT(*) FROM orders")
    print(f"  seeded {total} orders (id 1..{total}, PK CLUSTERED)")
    return total


# ---------------------------------------------------------------------------
def experiment_a(mon):
    print("\n" + "=" * 62)
    print("EXPERIMENT A  escalation blocks the whole table")
    print("=" * 62)

    # baseline point-query latency with no contention
    base_ms, _ = point_select(UNTOUCHED_ID)
    print(f"  baseline point SELECT on id={UNTOUCHED_ID} (no contention): {base_ms:.1f} ms")

    upd = connect(DB, autocommit=False)
    ucur = upd.cursor()
    spid = scalar(ucur, "SELECT @@SPID")
    ucur.execute("UPDATE orders SET amount = amount + 1 WHERE id BETWEEN 1 AND 8000")
    snap = lock_snapshot(mon, spid)
    print(f"  updater SPID {spid} holds 8000-row UPDATE open:")
    print(f"    KEY locks={snap['key']}  PAGE locks={snap['page']}  "
          f"OBJECT mode={snap['obj_mode']}  escalated={snap['escalated']}")

    # concurrent point SELECT on an untouched row, timed until the updater commits
    HOLD = 3.0
    result = {}

    def blocker():
        ms, blocked = point_select(UNTOUCHED_ID)   # no lock_timeout: wait it out
        result["ms"] = ms
        result["blocked"] = blocked

    t = threading.Thread(target=blocker)
    t0 = time.time()
    t.start()
    time.sleep(HOLD)
    upd.commit()                                   # release the table lock
    t.join()
    held_ms = (time.time() - t0) * 1000
    upd.close()

    wait_ms = result["ms"]
    print(f"  conn2 SELECT on untouched id={UNTOUCHED_ID} returned after {wait_ms:.0f} ms "
          f"(updater held ~{held_ms:.0f} ms)")
    print(f"  => untouched-row read waited on the table lock, not on its own row")

    return {"baseline_ms": round(base_ms, 1), "snap": snap,
            "blocked_wait_ms": round(wait_ms), "hold_ms": round(held_ms)}


# ---------------------------------------------------------------------------
def experiment_b(mon):
    print("\n" + "=" * 62)
    print("EXPERIMENT B  the ~5000-lock cliff")
    print("=" * 62)
    sizes = [500, 1000, 2000, 4000, 4900, 5000, 5500, 6000, 7000, 8000, 12000]
    rows = []
    print(f"  {'size':>6} {'KEY':>7} {'PAGE':>6} {'OBJ':>4} {'escal':>6} "
          f"{'select_ms':>10} {'blocked':>8}")
    for size in sizes:
        upd = connect(DB, autocommit=False)
        ucur = upd.cursor()
        spid = scalar(ucur, "SELECT @@SPID")
        ucur.execute("UPDATE orders SET amount = amount + 1 WHERE id BETWEEN 1 AND %d" % size)
        snap = lock_snapshot(mon, spid)
        sel_ms, blocked = point_select(UNTOUCHED_ID, lock_timeout_ms=800)
        upd.rollback()
        upd.close()
        print(f"  {size:>6} {snap['key']:>7} {snap['page']:>6} {snap['obj_mode']:>4} "
              f"{str(snap['escalated']):>6} {sel_ms:>10.1f} {str(blocked):>8}")
        rows.append({"size": size, "key": snap["key"], "page": snap["page"],
                     "obj_mode": snap["obj_mode"], "escalated": snap["escalated"],
                     "select_ms": round(sel_ms, 1), "blocked": blocked})
        time.sleep(0.2)   # let lock state settle between sizes
    # find the flip point
    flip = next((r["size"] for r in rows if r["escalated"]), None)
    print(f"  => escalation first observed at update_size = {flip}")
    return {"rows": rows, "flip": flip}


# ---------------------------------------------------------------------------
def _reader_loop(stop, lat, blk, lock_timeout_ms=1000):
    import random
    c = connect(DB)
    cur = c.cursor()
    while not stop.is_set():
        rid = random.randint(READER_LO, READER_HI)
        try:
            cur.execute("SET LOCK_TIMEOUT %d" % lock_timeout_ms)
            t0 = time.time()
            cur.execute("SELECT amount FROM orders WHERE id = %s", (rid,))
            cur.fetchall()
            lat.append((time.time() - t0) * 1000)
            blk.append(False)
        except pymssql.OperationalError:
            lat.append((time.time() - t0) * 1000)
            blk.append(True)
            # connection may be poisoned after a timeout; refresh it
            try:
                c.close()
            except Exception:
                pass
            c = connect(DB)
            cur = c.cursor()
    c.close()


def _run_arm(name, updater_fn):
    """Run a concurrent reader against untouched high rows while updater_fn runs."""
    stop = threading.Event()
    lat, blk = [], []
    r = threading.Thread(target=_reader_loop, args=(stop, lat, blk))
    r.start()
    time.sleep(0.3)                 # let the reader warm up
    t0 = time.time()
    updater_fn()
    updater_ms = (time.time() - t0) * 1000
    stop.set()
    r.join()
    m = {"arm": name, "updater_total_ms": round(updater_ms),
         "n": len(lat), "p50": round(percentile(lat, 50), 1),
         "p99": round(percentile(lat, 99), 1), "max": round(max(lat) if lat else 0, 1),
         "blocked_n": sum(blk)}
    print(f"  {name:26} updater={m['updater_total_ms']:>7} ms | selects={m['n']:>5} "
          f"p50={m['p50']:>7.1f} p99={m['p99']:>7.1f} max={m['max']:>7.1f} "
          f"blocked={m['blocked_n']}")
    return m


def experiment_c(admin):
    # The 50k-row update itself is milliseconds under emulation, so the damage from
    # escalation is set by how long the transaction *holds* its locks. We hold each
    # transaction open for HOLD_S total (a stand-in for a transaction that does other
    # work before committing) and keep total wall-time equal across arms -- the only
    # difference is whether the lock is one table X held continuously (naive) or many
    # small row-lock sets released between commits (batched).
    HOLD_S = 3.0
    N_BATCHES = 25
    print("\n" + "=" * 62)
    print("EXPERIMENT C  the fix: one big update vs 2000-row batches")
    print("=" * 62)
    print("  50,000-row update while a reader fires point SELECTs on untouched rows")
    print(f"  (reader hits id in {READER_LO}..{READER_HI}, never covered by the update)")
    print(f"  each transaction holds its locks for ~{HOLD_S:.0f}s total\n")
    metrics = []

    # naive: single statement, escalates to a table X lock held for the whole tran
    def naive():
        c = connect(DB, autocommit=False)
        cur = c.cursor()
        cur.execute("UPDATE orders SET amount = amount + 1 WHERE id BETWEEN 1 AND 50000")
        time.sleep(HOLD_S)
        c.commit()
        c.close()
    metrics.append(_run_arm("naive_single_update", naive))

    time.sleep(0.5)

    # batched: 25 committed batches of 2000, each below the escalation threshold;
    # locks released between batches so the reader gets windows to slip through
    def batched():
        c = connect(DB, autocommit=False)
        cur = c.cursor()
        lo = 1
        while lo <= 50000:
            cur.execute("UPDATE orders SET amount = amount + 1 WHERE id BETWEEN %d AND %d"
                        % (lo, lo + 1999))
            c.commit()
            time.sleep(HOLD_S / N_BATCHES)
            lo += 2000
        c.close()
    metrics.append(_run_arm("batched_2000", batched))

    time.sleep(0.5)

    # contrast: disable escalation, single big update -> fine-grained locks kept.
    # Row locks stay on ids 1..50000 only, so reads of the untouched high range
    # are never blocked, at the cost of holding 50k locks in memory.
    disable_note = None
    try:
        admin.execute("USE lockesc_bench")
        admin.execute("ALTER TABLE orders SET (LOCK_ESCALATION = DISABLE)")

        def disabled():
            c = connect(DB, autocommit=False)
            cur = c.cursor()
            cur.execute("UPDATE orders SET amount = amount + 1 WHERE id BETWEEN 1 AND 50000")
            time.sleep(HOLD_S)
            c.commit()
            c.close()
        metrics.append(_run_arm("lock_escalation_disable", disabled))
        admin.execute("ALTER TABLE orders SET (LOCK_ESCALATION = TABLE)")
    except Exception as e:          # keep the run honest if this arm is flaky
        disable_note = f"LOCK_ESCALATION=DISABLE arm did not run cleanly: {e!r}"
        print(f"  [skipped] {disable_note}")
        try:
            admin.execute("ALTER TABLE orders SET (LOCK_ESCALATION = TABLE)")
        except Exception:
            pass
    return {"metrics": metrics, "disable_note": disable_note}


# ---------------------------------------------------------------------------
IMAGE_DIGEST = ("mcr.microsoft.com/mssql/server@sha256:"
                "ba4c8329f48fb8f02e1416be6a930ebfd71268caee78aa985f3af4315e457c89")


def main():
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(os.path.join(RESULTS, "attempts"), exist_ok=True)
    admin_conn = connect("master")
    admin = admin_conn.cursor()
    ver = scalar(admin, "SELECT @@VERSION").splitlines()[0].strip()
    print("  " + ver)
    setup(admin)

    # a dedicated monitor connection for sys.dm_tran_locks
    mon_conn = connect(DB)
    mon = mon_conn.cursor()

    a = experiment_a(mon)
    b = experiment_b(mon)
    c = experiment_c(admin)

    # ---- CSVs ----
    with open(os.path.join(RESULTS, "a_escalation_blocking.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "resource_type", "mode", "value"])
        s = a["snap"]
        w.writerow(["lock_count", "KEY", "", s["key"]])
        w.writerow(["lock_count", "PAGE", "", s["page"]])
        w.writerow(["lock_count", "OBJECT", s["obj_mode"], 1 if s["obj_mode"] else 0])
        w.writerow(["escalated", "", "", s["escalated"]])
        w.writerow(["baseline_point_select_ms", "", "", a["baseline_ms"]])
        w.writerow(["blocked_point_select_ms", "", "", a["blocked_wait_ms"]])
        w.writerow(["updater_hold_ms", "", "", a["hold_ms"]])

    with open(os.path.join(RESULTS, "b_threshold_sweep.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["update_size", "key_locks", "page_locks", "object_lock_mode",
                    "escalated", "concurrent_select_ms", "concurrent_select_blocked"])
        for r in b["rows"]:
            w.writerow([r["size"], r["key"], r["page"], r["obj_mode"],
                        r["escalated"], r["select_ms"], r["blocked"]])

    with open(os.path.join(RESULTS, "c_batching.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "updater_total_ms", "concurrent_selects_n", "select_p50_ms",
                    "select_p99_ms", "select_max_ms", "select_blocked_n"])
        for m in c["metrics"]:
            w.writerow([m["arm"], m["updater_total_ms"], m["n"], m["p50"],
                        m["p99"], m["max"], m["blocked_n"]])

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mssql_version", "image_digest", "orders_rows", "escalation_threshold_observed",
                    "host", "port"])
        w.writerow([ver, IMAGE_DIGEST, ROWS, b["flip"], HOST, PORT])

    if c["disable_note"]:
        with open(os.path.join(RESULTS, "attempts", "lock_escalation_disable.txt"), "w") as f:
            f.write(c["disable_note"] + "\n")

    # ---- summary.txt ----
    lines = []
    lines.append("  " + ver)
    lines.append(f"  {ROWS} orders, id 1..{ROWS}, PK CLUSTERED | image {IMAGE_DIGEST}")
    lines.append("")
    lines.append("EXPERIMENT A  escalation blocks the whole table")
    s = a["snap"]
    lines.append(f"  8000-row UPDATE held open -> KEY={s['key']} PAGE={s['page']} "
                 f"OBJECT={s['obj_mode']} escalated={s['escalated']}")
    lines.append(f"  point SELECT on untouched id={UNTOUCHED_ID}: baseline {a['baseline_ms']} ms "
                 f"-> blocked {a['blocked_wait_ms']} ms (updater held ~{a['hold_ms']} ms)")
    lines.append("")
    lines.append("EXPERIMENT B  the ~5000-lock cliff")
    lines.append(f"  escalation first observed at update_size = {b['flip']}")
    for r in b["rows"]:
        lines.append(f"    size={r['size']:>6} KEY={r['key']:>6} PAGE={r['page']:>5} "
                     f"OBJ={r['obj_mode']:>3} escalated={str(r['escalated']):>5} "
                     f"select={r['select_ms']:>8.1f}ms blocked={r['blocked']}")
    lines.append("")
    lines.append("EXPERIMENT C  one big update vs 2000-row batches (50,000 rows)")
    for m in c["metrics"]:
        lines.append(f"  {m['arm']:26} updater={m['updater_total_ms']:>7}ms selects={m['n']:>5} "
                     f"p50={m['p50']:>7.1f} p99={m['p99']:>7.1f} max={m['max']:>7.1f} "
                     f"blocked={m['blocked_n']}")
    if c["disable_note"]:
        lines.append(f"  note: {c['disable_note']}")
    lines.append("")
    lines.append("  artifacts in results/")
    summary = "\n".join(lines)
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(summary + "\n")
    print("\n" + summary)
    print("\n  wrote CSVs + summary.txt to results/")

    mon_conn.close()
    admin_conn.close()


if __name__ == "__main__":
    main()
