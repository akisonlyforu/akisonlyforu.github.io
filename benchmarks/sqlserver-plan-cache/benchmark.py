"""Reproduce the SQL Server plan-cache problem with parameterization.

Three experiments against a real SQL Server 2022:
  A. Plan-cache bloat - 500 ad-hoc queries with baked-in literals produce ~500
     single-use cached plans; 500 runs of the same parameterized query produce one.
  B. Optimize for ad hoc - turning on "optimize for ad hoc workloads" caches a
     stub instead of a full plan on first sight, shrinking the bloat.
  C. Parameter sniffing - a parameterized plan compiled for a rare value performs
     badly when reused for a common one; OPTION(RECOMPILE) fixes it.

Env: MSSQL_HOST (127.0.0.1), MSSQL_PORT (11433), MSSQL_SA_PASSWORD.
"""
import csv
import os
import time

import pymssql

HOST = os.environ.get("MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MSSQL_PORT", "11433"))
PWD = os.environ.get("MSSQL_SA_PASSWORD", "Str0ng_P@ssw0rd!")
DB = "plancache_bench"
N = 500
ROWS = 2_000_000
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))


def connect(db="master"):
    return pymssql.connect(server=HOST, port=PORT, user="sa", password=PWD,
                           database=db, autocommit=True, timeout=120, login_timeout=30)


def scalar(cur, sql, args=None):
    cur.execute(sql, args) if args else cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def cache_snapshot(cur):
    """Cached plans that mention our table, grouped by plan type."""
    cur.execute("""
        SELECT p.objtype, COUNT(*) AS plans,
               SUM(CAST(p.size_in_bytes AS bigint))/1024 AS kb,
               SUM(CAST(p.usecounts AS bigint)) AS total_use
        FROM sys.dm_exec_cached_plans p
        CROSS APPLY sys.dm_exec_sql_text(p.plan_handle) t
        WHERE t.text LIKE '%FROM orders%'
          AND t.text NOT LIKE '%dm_exec_cached_plans%'
        GROUP BY p.objtype
    """)
    return {r[0]: {"plans": r[1], "kb": int(r[2] or 0), "use": int(r[3] or 0)} for r in cur.fetchall()}


def setup(cur):
    cur.execute("IF DB_ID('plancache_bench') IS NOT NULL BEGIN ALTER DATABASE plancache_bench "
                "SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE plancache_bench; END")
    cur.execute("CREATE DATABASE plancache_bench")
    cur.execute("USE plancache_bench")
    cur.execute("CREATE TABLE customers (customer_id INT PRIMARY KEY, region VARCHAR(20))")
    cur.execute("CREATE TABLE orders (id INT IDENTITY PRIMARY KEY, customer_id INT NOT NULL, "
                "status VARCHAR(16) NOT NULL, amount DECIMAL(10,2) NOT NULL, created_at DATETIME2 NOT NULL)")
    cur.execute("""INSERT INTO customers(customer_id, region)
                   SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                          CHOOSE(1+ABS(CHECKSUM(NEWID()))%4,'us','eu','apac','latam')
                   FROM sys.all_objects a CROSS JOIN sys.all_objects b""")
    cur.execute(f""";WITH n AS (SELECT TOP ({ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) rn
                     FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c)
                   INSERT INTO orders(customer_id, status, amount, created_at)
                   SELECT (ABS(CHECKSUM(NEWID())) % 5000) + 1,
                          CASE WHEN rn % 1000 = 0 THEN 'refunded'
                               WHEN rn % 200 = 0 THEN 'cancelled' ELSE 'shipped' END,
                          (ABS(CHECKSUM(NEWID())) % 100000)/100.0,
                          DATEADD(minute, -rn, SYSUTCDATETIME())
                   FROM n""")
    cur.execute("CREATE INDEX ix_orders_customer ON orders(customer_id)")
    cur.execute("CREATE INDEX ix_orders_status ON orders(status)")
    total = scalar(cur, "SELECT COUNT(*) FROM orders")
    shipped = scalar(cur, "SELECT COUNT(*) FROM orders WHERE status='shipped'")
    refunded = scalar(cur, "SELECT COUNT(*) FROM orders WHERE status='refunded'")
    print(f"  seeded {total} orders / 5000 customers | shipped={shipped} refunded={refunded}")
    return total, shipped, refunded


# a join keeps SQL Server from auto-parameterizing the ad-hoc form, so each literal
# gets its own cached plan (the whole point).
ADHOC = ("SELECT o.id, o.amount, c.region FROM orders o "
         "JOIN customers c ON c.customer_id = o.customer_id WHERE o.customer_id = {}")
PARAM_INNER = ("SELECT o.id, o.amount, c.region FROM orders o "
               "JOIN customers c ON c.customer_id = o.customer_id WHERE o.customer_id = @cid")


def experiment_a(cur, ids):
    print("\n" + "=" * 62)
    print("EXPERIMENT A  plan-cache bloat: ad-hoc literals vs parameterized")
    print("=" * 62)

    cur.execute("DBCC FREEPROCCACHE WITH NO_INFOMSGS")
    t0 = time.time()
    for k in ids:
        cur.execute(ADHOC.format(k)); cur.fetchall()
    adhoc_ms = (time.time() - t0) * 1000
    adhoc = cache_snapshot(cur)

    cur.execute("DBCC FREEPROCCACHE WITH NO_INFOMSGS")
    t0 = time.time()
    for k in ids:
        cur.execute("EXEC sp_executesql %s, N'@cid int', @cid=%d", (PARAM_INNER, k)); cur.fetchall()
    param_ms = (time.time() - t0) * 1000
    param = cache_snapshot(cur)

    a_plans = sum(v["plans"] for v in adhoc.values())
    a_kb = sum(v["kb"] for v in adhoc.values())
    p_plans = sum(v["plans"] for v in param.values())
    p_kb = sum(v["kb"] for v in param.values())
    print(f"  ran {len(ids)} distinct customer_id lookups each way\n")
    print(f"  {'':16}{'cached plans':>14}{'cache KB':>12}{'wall ms':>12}")
    print(f"  {'ad-hoc':16}{a_plans:>14}{a_kb:>12}{adhoc_ms:>12.0f}")
    print(f"  {'parameterized':16}{p_plans:>14}{p_kb:>12}{param_ms:>12.0f}")
    print(f"  plan types ad-hoc: { {k: v['plans'] for k,v in adhoc.items()} }")
    print(f"  plan types param : { {k: v['plans'] for k,v in param.items()} }")
    return {"adhoc_plans": a_plans, "adhoc_kb": a_kb, "adhoc_ms": round(adhoc_ms),
            "param_plans": p_plans, "param_kb": p_kb, "param_ms": round(param_ms)}


def experiment_b(cur, ids):
    print("\n" + "=" * 62)
    print("EXPERIMENT B  'optimize for ad hoc workloads' on the same ad-hoc flood")
    print("=" * 62)
    cur.execute("EXEC sp_configure 'show advanced options', 1"); cur.execute("RECONFIGURE")
    results = {}
    for setting in (0, 1):
        cur.execute(f"EXEC sp_configure 'optimize for ad hoc workloads', {setting}")
        cur.execute("RECONFIGURE")
        cur.execute("DBCC FREEPROCCACHE WITH NO_INFOMSGS")
        for k in ids:
            cur.execute(ADHOC.format(k)); cur.fetchall()
        snap = cache_snapshot(cur)
        kb = sum(v["kb"] for v in snap.values())
        results[setting] = kb
        label = "on" if setting else "off"
        print(f"  optimize-for-ad-hoc {label:3}: {kb:>7} KB of plan cache for {len(ids)} ad-hoc queries")
    cur.execute("EXEC sp_configure 'optimize for ad hoc workloads', 0"); cur.execute("RECONFIGURE")
    return {"cache_kb_off": results[0], "cache_kb_on": results[1]}


def timed_status_query(cur, status, recompile=False):
    opt = " OPTION (RECOMPILE)" if recompile else ""
    sql = ("EXEC sp_executesql N'SELECT COUNT(amount) FROM orders WHERE status=@s"
           + opt + "', N'@s varchar(16)', @s=%s")
    t0 = time.time()
    cur.execute(sql, (status,)); cur.fetchall()
    return (time.time() - t0) * 1000


def experiment_c(cur):
    print("\n" + "=" * 62)
    print("EXPERIMENT C  parameter sniffing: one plan, two very different values")
    print("=" * 62)
    # prime the cache with a RARE value -> optimizer builds a seek+lookup plan
    cur.execute("DBCC FREEPROCCACHE WITH NO_INFOMSGS")
    warm_rare = timed_status_query(cur, "refunded")
    # now the COMMON value reuses that seek+lookup plan (bad: thousands of lookups)
    sniffed_common = timed_status_query(cur, "shipped")
    # same common query, forced to recompile -> optimizer picks a scan (good)
    recompiled_common = timed_status_query(cur, "shipped", recompile=True)
    print(f"  primed plan on rare  value 'refunded' : {warm_rare:8.1f} ms")
    print(f"  common value 'shipped' on sniffed plan: {sniffed_common:8.1f} ms")
    print(f"  common value 'shipped' with RECOMPILE : {recompiled_common:8.1f} ms")
    factor = sniffed_common / recompiled_common if recompiled_common else 0
    print(f"  => the sniffed plan is {factor:.1f}x slower on the common value")
    return {"warm_rare_ms": round(warm_rare, 1), "sniffed_common_ms": round(sniffed_common, 1),
            "recompiled_common_ms": round(recompiled_common, 1), "factor": round(factor, 1)}


def main():
    os.makedirs(RESULTS, exist_ok=True)
    admin = connect("master")
    acur = admin.cursor()
    ver = scalar(acur, "SELECT @@VERSION").splitlines()[0]
    print("  " + ver)
    setup(acur)

    conn = connect(DB)
    cur = conn.cursor()
    cur.execute("SET NOCOUNT ON")
    # 500 distinct customer ids
    ids = list(range(1, N + 1))

    a = experiment_a(cur, ids)
    b = experiment_b(cur, ids)
    c = experiment_c(cur)

    with open(os.path.join(RESULTS, "plan_cache.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "cached_plans", "cache_kb", "wall_ms"])
        w.writerow(["adhoc", a["adhoc_plans"], a["adhoc_kb"], a["adhoc_ms"]])
        w.writerow(["parameterized", a["param_plans"], a["param_kb"], a["param_ms"]])
    with open(os.path.join(RESULTS, "optimize_adhoc.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["optimize_for_adhoc", "cache_kb"])
        w.writerow(["off", b["cache_kb_off"]]); w.writerow(["on", b["cache_kb_on"]])
    with open(os.path.join(RESULTS, "sniffing.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "elapsed_ms"])
        w.writerow(["primed_rare_refunded", c["warm_rare_ms"]])
        w.writerow(["sniffed_common_shipped", c["sniffed_common_ms"]])
        w.writerow(["recompile_common_shipped", c["recompiled_common_ms"]])
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mssql_version", "n_queries", "orders_rows"])
        w.writerow([ver, N, ROWS])
    print(f"\n  artifacts in results/")


if __name__ == "__main__":
    main()
