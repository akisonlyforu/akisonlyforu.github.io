"""Reproduce SQL Server index fragmentation and REBUILD vs REORGANIZE remediation.

Four experiments against a real SQL Server 2022:
  A. Build fragmentation and measure it - random-order (hashed, non-sequential)
     inserts against a nonclustered index packed at FILLFACTOR=100 fragment fast
     via page splits; track avg_fragmentation_in_percent, avg_page_space_used_in_
     percent, and range-query cost across 8 churn checkpoints.
  B. REBUILD vs REORGANIZE - from the SAME deterministic high-fragmentation state
     (two independently-built, identically-churned tables - no NEWID(), so the
     comparison is apples to apples), rebuild one and reorganize the other.
     Compare elapsed time, resulting fragmentation, and transaction log growth.
  C. Does REORGANIZE keep up at high fragmentation? - reorganize (and rebuild, as
     a control) at a LOW starting fragmentation level and compare against the
     HIGH-level pair measured in experiment B.
  D. Post-fix query performance - the experiment A/B query, fragmented vs after
     REBUILD vs after REORGANIZE.

customer_id (the nonclustered index's leading key) is derived from
HASHBYTES('MD5', row_counter) rather than NEWID(), so churn is fully
deterministic: two tables built with the same base row count and the same
churn schedule land in an identical physical fragmentation state.

pymssql does not surface SET STATISTICS IO/TIME messages (they come back as
TDS info messages, not result rows) - confirmed during calibration against
this harness, where sqlcmd's STATISTICS IO output matched sys.dm_exec_query_
stats.last_logical_reads exactly. So query cost here is read from
sys.dm_exec_query_stats (logical reads, server-side elapsed time) with a
wall-clock fallback available via time.time() if that DMV is ever empty.

Env: MSSQL_HOST (127.0.0.1), MSSQL_PORT (11434), MSSQL_SA_PASSWORD, RESULTS_DIR.
"""
import csv
import os
import time

import pymssql

HOST = os.environ.get("MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MSSQL_PORT", "11434"))
PWD = os.environ.get("MSSQL_SA_PASSWORD", "Str0ng_P@ssw0rd!")
DB = "frag_bench"
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
IMAGE_DIGEST = "sha256:ba4c8329f48fb8f02e1416be6a930ebfd71268caee78aa985f3af4315e457c89"

BASE_ROWS = 2_000_000
NUM_CUSTOMERS = 200_000
# cumulative churn schedule for experiment A's checkpoints / experiment B's
# "high fragmentation" state. Calibrated so the curve runs from ~0% to ~99%
# fragmentation over 8 steps on a freshly FILLFACTOR=100 index.
CHURN_STEPS = [200, 400, 800, 1600, 3200, 6400, 12800, 25600]
# a much smaller churn schedule for experiment C's "low fragmentation" state
# (lands around ~19%, inside the doc's 5-30% "moderate" band).
LOW_STEPS = [200, 400]
QUERY_LO, QUERY_HI = 40001, 50000
N_QUERY_RUNS = 15


def connect(db="master"):
    return pymssql.connect(server=HOST, port=PORT, user="sa", password=PWD,
                            database=db, autocommit=True, timeout=180, login_timeout=30)


def scalar(cur, sql):
    cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def churn_batch(cur, table, start_rn, batch_size):
    """Insert `batch_size` new orders with a hashed (non-sequential) customer_id
    - the leading key of the nonclustered index. Landing on already-full
    FILLFACTOR=100 leaf pages forces page splits, which is how this
    fragmentation happens on a real system taking out-of-order inserts."""
    cur.execute(f""";WITH n AS (
        SELECT TOP ({batch_size}) {start_rn} + ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) rn
        FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c
    )
    INSERT INTO {table}(customer_id, created_at, amount)
    SELECT (ABS(CHECKSUM(HASHBYTES('MD5', CAST(rn AS VARCHAR(20))))) % {NUM_CUSTOMERS}) + 1,
           DATEADD(second, rn, '2020-01-01'),
           (rn % 100000) / 100.0
    FROM n""")


def build_table(cur, table, idx, churn_steps):
    """Fresh clustered PK on an IDENTITY id, plus a nonclustered index on
    (customer_id, created_at) - a realistic "recent orders for this customer"
    index. Base load is one set-based insert; the index is then built at
    FILLFACTOR=100 (fully packed, baseline/unfragmented), and churn_steps are
    applied in order."""
    cur.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")
    cur.execute(f"""CREATE TABLE {table} (
        id INT IDENTITY(1,1) NOT NULL,
        customer_id INT NOT NULL,
        created_at DATETIME2 NOT NULL,
        amount DECIMAL(10,2) NOT NULL,
        CONSTRAINT pk_{table} PRIMARY KEY CLUSTERED (id)
    )""")
    cur.execute(f""";WITH n AS (
        SELECT TOP ({BASE_ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) rn
        FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c
    )
    INSERT INTO {table}(customer_id, created_at, amount)
    SELECT (ABS(CHECKSUM(HASHBYTES('MD5', CAST(rn AS VARCHAR(20))))) % {NUM_CUSTOMERS}) + 1,
           DATEADD(second, rn, '2020-01-01'),
           (rn % 100000) / 100.0
    FROM n""")
    cur.execute(f"CREATE INDEX {idx} ON {table}(customer_id, created_at) WITH (FILLFACTOR = 100)")
    rn = BASE_ROWS
    for batch in churn_steps:
        churn_batch(cur, table, rn, batch)
        rn += batch
    return rn


def frag_stats(cur, table, idx):
    cur.execute(f"""
        SELECT ips.avg_fragmentation_in_percent, ips.avg_page_space_used_in_percent, ips.page_count
        FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID('{table}'), NULL, NULL, 'DETAILED') ips
        JOIN sys.indexes i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
        WHERE i.name = '{idx}'
    """)
    row = cur.fetchone()
    return {"frag_pct": row[0], "page_space_used_pct": row[1], "page_count": row[2]}


def checkpoint(cur):
    cur.execute("CHECKPOINT")


def log_used_mb(cur, dbname):
    """DBCC SQLPERF(LOGSPACE) reports log file size and percent used per
    database. Call checkpoint() explicitly at the call site *before* the
    operation you want to isolate (never in between the operation and this
    read) - that way the delta between two log_used_mb() reads captures the
    log volume the operation generated, before anything gets a chance to
    reclaim it."""
    cur.execute("DBCC SQLPERF(LOGSPACE)")
    for row in cur.fetchall():
        if row[0] == dbname:
            return row[1] * row[2] / 100.0
    return None


def query_stats_snapshot(cur, sql):
    """Exact-text match against sys.dm_exec_sql_text, SUMmed across every
    matching row - a fuzzy LIKE on just the index name also matches this
    harness's own monitoring queries (they reference the index name too, e.g.
    in frag_stats()'s WHERE clause), which silently corrupts the before/after
    delta. Summing matters too: REBUILD/REORGANIZE and auto-stats updates
    after a big churn batch invalidate the cached plan, so the *same* sql
    text can accumulate more than one (sql_handle, plan_handle) row over the
    run; fetchone() on a single row would arbitrarily pick one and produce a
    nonsense delta."""
    cur.execute("""
        SELECT COALESCE(SUM(qs.execution_count), 0), COALESCE(SUM(qs.total_logical_reads), 0),
               COALESCE(SUM(qs.total_elapsed_time), 0)
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
        WHERE t.text = %s
    """, (sql,))
    row = cur.fetchone()
    return row if row else (0, 0, 0)


def measure_query(cur, table, idx, n=N_QUERY_RUNS):
    """Run the representative range-scan query n times and report the average
    logical reads and server-side elapsed time from sys.dm_exec_query_stats.

    A before/after delta on that DMV turned out not to be safe here: a big
    churn batch (auto-updated stats) or a REBUILD/REORGANIZE invalidates the
    cached plan for this exact statement, and SQL Server can *evict* the old
    (sql_handle, plan_handle) row entirely rather than leave it frozen -
    which produced a negative delta and a false "no data" result once per
    run. FREEPROCCACHE right before the loop guarantees a clean slate, so the
    post-loop totals (summed across any mid-loop recompile) are exactly this
    call's n executions - no delta arithmetic needed."""
    sql = (f"SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM {table} "
           f"WITH (INDEX({idx})) WHERE customer_id BETWEEN {QUERY_LO} AND {QUERY_HI}")
    cur.execute("DBCC FREEPROCCACHE WITH NO_INFOMSGS")
    t0 = time.time()
    for _ in range(n):
        cur.execute(sql)
        cur.fetchall()
    wall_ms = (time.time() - t0) * 1000 / n
    total = query_stats_snapshot(cur, sql)
    d_exec = total[0]
    if d_exec <= 0:
        return {"avg_logical_reads": None, "avg_elapsed_ms": round(wall_ms, 3)}
    return {"avg_logical_reads": round(total[1] / d_exec, 1),
            "avg_elapsed_ms": round((total[2] / d_exec) / 1000.0, 3)}


def experiment_a(cur):
    print("\n" + "=" * 72)
    print("EXPERIMENT A  building fragmentation and watching it happen")
    print("=" * 72)
    table, idx = "orders", "ix_orders_cust_date"
    build_table(cur, table, idx, [])  # base load + baseline FILLFACTOR=100 index, no churn yet
    rows = []
    print(f"  {'checkpoint':<10}{'cum churn':>11}{'frag %':>9}{'page use %':>12}"
          f"{'pages':>8}{'reads':>8}{'ms':>9}")

    def emit(label, cum_churn):
        s = frag_stats(cur, table, idx)
        q = measure_query(cur, table, idx)
        print(f"  {label:<10}{cum_churn:>11}{s['frag_pct']:>9.2f}{s['page_space_used_pct']:>12.2f}"
              f"{s['page_count']:>8}{(q['avg_logical_reads'] or 0):>8.0f}{q['avg_elapsed_ms']:>9.3f}")
        rows.append({"checkpoint": label, "cum_churn_rows": cum_churn,
                     "avg_fragmentation_in_percent": round(s["frag_pct"], 2),
                     "avg_page_space_used_in_percent": round(s["page_space_used_pct"], 2),
                     "page_count": s["page_count"],
                     "avg_logical_reads": q["avg_logical_reads"],
                     "avg_elapsed_ms": q["avg_elapsed_ms"]})

    emit("baseline", 0)
    cum = 0
    for i, batch in enumerate(CHURN_STEPS, 1):
        churn_batch(cur, table, BASE_ROWS + cum, batch)
        cum += batch
        emit(f"churn-{i}", cum)
    return rows


def experiment_b(cur):
    print("\n" + "=" * 72)
    print("EXPERIMENT B  REBUILD vs REORGANIZE from an identical fragmented state")
    print("=" * 72)
    print("  building two independent tables with the same deterministic base load + churn...")
    build_table(cur, "orders_rebuild", "ix_orders_rebuild_cust_date", CHURN_STEPS)
    build_table(cur, "orders_reorg", "ix_orders_reorg_cust_date", CHURN_STEPS)

    before_rebuild = frag_stats(cur, "orders_rebuild", "ix_orders_rebuild_cust_date")
    before_reorg = frag_stats(cur, "orders_reorg", "ix_orders_reorg_cust_date")
    print(f"  starting fragmentation   rebuild-copy={before_rebuild['frag_pct']:.2f}%   "
          f"reorg-copy={before_reorg['frag_pct']:.2f}%  (should match closely - same churn)")
    q_fragmented = measure_query(cur, "orders_rebuild", "ix_orders_rebuild_cust_date")

    # checkpoint right before each operation (never in between the operation and
    # the "after" read) so the delta captures log the operation generated,
    # before anything gets a chance to reclaim it.
    checkpoint(cur)
    log0 = log_used_mb(cur, DB)
    t0 = time.time()
    cur.execute("ALTER INDEX ix_orders_rebuild_cust_date ON orders_rebuild REBUILD WITH (FILLFACTOR = 100)")
    rebuild_s = time.time() - t0
    log1 = log_used_mb(cur, DB)
    after_rebuild = frag_stats(cur, "orders_rebuild", "ix_orders_rebuild_cust_date")
    q_after_rebuild = measure_query(cur, "orders_rebuild", "ix_orders_rebuild_cust_date")

    checkpoint(cur)
    log1b = log_used_mb(cur, DB)
    t0 = time.time()
    cur.execute("ALTER INDEX ix_orders_reorg_cust_date ON orders_reorg REORGANIZE")
    reorg_s = time.time() - t0
    log2 = log_used_mb(cur, DB)
    after_reorg = frag_stats(cur, "orders_reorg", "ix_orders_reorg_cust_date")
    q_after_reorg = measure_query(cur, "orders_reorg", "ix_orders_reorg_cust_date")

    print(f"\n  {'operation':<12}{'elapsed s':>11}{'frag % after':>14}{'page use % after':>18}{'log growth MB':>15}")
    print(f"  {'REBUILD':<12}{rebuild_s:>11.2f}{after_rebuild['frag_pct']:>14.2f}"
          f"{after_rebuild['page_space_used_pct']:>18.2f}{(log1 - log0):>15.2f}")
    print(f"  {'REORGANIZE':<12}{reorg_s:>11.2f}{after_reorg['frag_pct']:>14.2f}"
          f"{after_reorg['page_space_used_pct']:>18.2f}{(log2 - log1b):>15.2f}")
    ratio_time = reorg_s / rebuild_s if rebuild_s else 0
    ratio_log = (log2 - log1b) / (log1 - log0) if (log1 - log0) else 0
    print(f"  => REORGANIZE took {ratio_time:.1f}x as long and generated {ratio_log:.1f}x the log "
          f"of REBUILD for a comparable fragmentation result")

    return {
        "before_rebuild": before_rebuild, "before_reorg": before_reorg,
        "after_rebuild": after_rebuild, "after_reorg": after_reorg,
        "rebuild_s": rebuild_s, "reorg_s": reorg_s,
        "log_mb_rebuild": log1 - log0, "log_mb_reorg": log2 - log1b,
        "q_fragmented": q_fragmented, "q_after_rebuild": q_after_rebuild, "q_after_reorg": q_after_reorg,
    }


def experiment_c(cur, high):
    print("\n" + "=" * 72)
    print("EXPERIMENT C  does REORGANIZE keep up as starting fragmentation rises?")
    print("=" * 72)
    build_table(cur, "orders_low_rebuild", "ix_orders_lorb_cust_date", LOW_STEPS)
    build_table(cur, "orders_low_reorg", "ix_orders_loro_cust_date", LOW_STEPS)

    low_rebuild_before = frag_stats(cur, "orders_low_rebuild", "ix_orders_lorb_cust_date")
    low_reorg_before = frag_stats(cur, "orders_low_reorg", "ix_orders_loro_cust_date")

    t0 = time.time()
    cur.execute("ALTER INDEX ix_orders_lorb_cust_date ON orders_low_rebuild REBUILD WITH (FILLFACTOR = 100)")
    low_rebuild_s = time.time() - t0
    low_rebuild_after = frag_stats(cur, "orders_low_rebuild", "ix_orders_lorb_cust_date")

    t0 = time.time()
    cur.execute("ALTER INDEX ix_orders_loro_cust_date ON orders_low_reorg REORGANIZE")
    low_reorg_s = time.time() - t0
    low_reorg_after = frag_stats(cur, "orders_low_reorg", "ix_orders_loro_cust_date")

    rows = [
        {"level": "low", "operation": "REBUILD",
         "frag_before": round(low_rebuild_before["frag_pct"], 2),
         "frag_after": round(low_rebuild_after["frag_pct"], 2),
         "elapsed_s": round(low_rebuild_s, 3)},
        {"level": "low", "operation": "REORGANIZE",
         "frag_before": round(low_reorg_before["frag_pct"], 2),
         "frag_after": round(low_reorg_after["frag_pct"], 2),
         "elapsed_s": round(low_reorg_s, 3)},
        {"level": "high", "operation": "REBUILD",
         "frag_before": round(high["before_rebuild"]["frag_pct"], 2),
         "frag_after": round(high["after_rebuild"]["frag_pct"], 2),
         "elapsed_s": round(high["rebuild_s"], 3)},
        {"level": "high", "operation": "REORGANIZE",
         "frag_before": round(high["before_reorg"]["frag_pct"], 2),
         "frag_after": round(high["after_reorg"]["frag_pct"], 2),
         "elapsed_s": round(high["reorg_s"], 3)},
    ]
    print(f"  {'level':<6}{'operation':<12}{'frag before':>13}{'frag after':>12}{'elapsed s':>11}")
    for r in rows:
        print(f"  {r['level']:<6}{r['operation']:<12}{r['frag_before']:>13.2f}"
              f"{r['frag_after']:>12.2f}{r['elapsed_s']:>11.3f}")
    reorg_low = next(r for r in rows if r["level"] == "low" and r["operation"] == "REORGANIZE")
    reorg_high = next(r for r in rows if r["level"] == "high" and r["operation"] == "REORGANIZE")
    rebuild_low = next(r for r in rows if r["level"] == "low" and r["operation"] == "REBUILD")
    rebuild_high = next(r for r in rows if r["level"] == "high" and r["operation"] == "REBUILD")
    print(f"  => REBUILD elapsed time barely moves with starting fragmentation "
          f"({rebuild_low['elapsed_s']:.2f}s low -> {rebuild_high['elapsed_s']:.2f}s high); "
          f"REORGANIZE's does ({reorg_low['elapsed_s']:.2f}s low -> {reorg_high['elapsed_s']:.2f}s high)")
    return rows


def experiment_d(b):
    print("\n" + "=" * 72)
    print("EXPERIMENT D  range-query cost: fragmented vs after REBUILD vs after REORGANIZE")
    print("=" * 72)
    rows = [
        {"state": "fragmented", **b["q_fragmented"]},
        {"state": "after_rebuild", **b["q_after_rebuild"]},
        {"state": "after_reorganize", **b["q_after_reorg"]},
    ]
    print(f"  {'state':<18}{'avg logical reads':>20}{'avg elapsed ms':>16}")
    for r in rows:
        reads = r["avg_logical_reads"] if r["avg_logical_reads"] is not None else float("nan")
        print(f"  {r['state']:<18}{reads:>20.1f}{r['avg_elapsed_ms']:>16.3f}")
    return rows


def main():
    os.makedirs(RESULTS, exist_ok=True)
    admin = connect("master")
    acur = admin.cursor()
    ver = scalar(acur, "SELECT @@VERSION").splitlines()[0]
    print("  " + ver)
    acur.execute(f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE {DB} "
                 f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {DB}; END")
    acur.execute(f"CREATE DATABASE {DB}")
    acur.execute(f"ALTER DATABASE {DB} SET RECOVERY SIMPLE")
    print(f"  database {DB} created, recovery model SIMPLE "
          f"(keeps the log-growth comparison in experiment B clean)")

    conn = connect(DB)
    cur = conn.cursor()
    cur.execute("SET NOCOUNT ON")

    t_start = time.time()
    a_rows = experiment_a(cur)
    b = experiment_b(cur)
    c_rows = experiment_c(cur, b)
    d_rows = experiment_d(b)
    total_s = time.time() - t_start

    with open(os.path.join(RESULTS, "fragmentation_over_time.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["checkpoint", "cum_churn_rows", "avg_fragmentation_in_percent",
                                           "avg_page_space_used_in_percent", "page_count",
                                           "avg_logical_reads", "avg_elapsed_ms"])
        w.writeheader()
        w.writerows(a_rows)

    with open(os.path.join(RESULTS, "rebuild_vs_reorganize.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["operation", "frag_before_pct", "frag_after_pct", "page_space_used_after_pct",
                    "elapsed_s", "log_growth_mb"])
        w.writerow(["REBUILD", round(b["before_rebuild"]["frag_pct"], 2),
                    round(b["after_rebuild"]["frag_pct"], 2),
                    round(b["after_rebuild"]["page_space_used_pct"], 2),
                    round(b["rebuild_s"], 3), round(b["log_mb_rebuild"], 2)])
        w.writerow(["REORGANIZE", round(b["before_reorg"]["frag_pct"], 2),
                    round(b["after_reorg"]["frag_pct"], 2),
                    round(b["after_reorg"]["page_space_used_pct"], 2),
                    round(b["reorg_s"], 3), round(b["log_mb_reorg"], 2)])

    with open(os.path.join(RESULTS, "reorganize_by_level.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["level", "operation", "frag_before", "frag_after", "elapsed_s"])
        w.writeheader()
        w.writerows(c_rows)

    with open(os.path.join(RESULTS, "query_performance.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["state", "avg_logical_reads", "avg_elapsed_ms"])
        w.writeheader()
        w.writerows(d_rows)

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mssql_version", "image_digest", "base_rows", "num_customers",
                    "churn_rows_high", "churn_rows_low", "query_customer_range",
                    "n_query_runs", "total_runtime_s"])
        w.writerow([ver, IMAGE_DIGEST, BASE_ROWS, NUM_CUSTOMERS, sum(CHURN_STEPS), sum(LOW_STEPS),
                    f"{QUERY_LO}-{QUERY_HI}", N_QUERY_RUNS, round(total_s, 1)])

    print(f"\n  total runtime: {total_s:.1f}s")
    print(f"  artifacts in results/")


if __name__ == "__main__":
    main()
