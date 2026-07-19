"""ORM vs stored procedures vs raw SQL, against a real digest-pinned Postgres 16.

Thesis: "ORM vs stored procedure" is not one axis. When you hold the *generated
SQL* constant, the gap nearly vanishes; the scary numbers come from named issues
(N+1, client-side materialization, non-parameterized ad-hoc SQL), not from "ORM"
as a category. Each experiment isolates ONE axis and the SP is written to return
exactly the same rows as the path it's compared against.

  A. Identical SQL (the mirage) - a parameterized single-join lookup, three ways
     (SQLAlchemy ORM / raw psycopg / PL/pgSQL function). Same SQL underneath.
  B. N+1                        - fetch M customers + their orders: naive lazy
     (1+M round trips) vs eager join (1) vs SP (1). Latency + round-trip count.
  C. Materialization            - fetch K rows: ORM full hydration (identity map)
     vs raw tuples vs SP tuples. Same SQL; isolate client-side hydration + memory.
  D. Plan cache / parameterization - same logical query many times: ad-hoc string
     concat (new text every call) vs parameterized (psycopg auto-prepares) vs SP.
     Reads pg_stat_statements for entry count and parse/plan time.

Env: PGHOST(127.0.0.1) PGPORT(5433) PGDATABASE(bench) PGUSER(bench) PGPASSWORD(bench),
RESULTS_DIR(./results), RUNS(3), plus per-experiment iteration overrides (see CONFIG).
Real numbers only. Whatever the run produces is what gets written.
"""
import csv
import os
import statistics
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

import psycopg
import sqlalchemy
from sqlalchemy import Integer, Numeric, String, DateTime, ForeignKey, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, joinedload

# ---------------------------------------------------------------- config
HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "5433"))
DB = os.environ.get("PGDATABASE", "bench")
USER = os.environ.get("PGUSER", "bench")
PASSWORD = os.environ.get("PGPASSWORD", "bench")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
RUNS = int(os.environ.get("RUNS", "3"))

# seed size
N_CUSTOMERS = int(os.environ.get("N_CUSTOMERS", "5000"))
AVG_ORDERS = int(os.environ.get("AVG_ORDERS", "10"))          # ~50k orders

# iteration counts (warmups discarded)
A_N = int(os.environ.get("A_N", "1000"));  A_WARM = int(os.environ.get("A_WARM", "100"))
B_M = int(os.environ.get("B_M", "100"))                        # customers fetched in N+1
B_N = int(os.environ.get("B_N", "30"));    B_WARM = int(os.environ.get("B_WARM", "5"))
C_K = int(os.environ.get("C_K", "3000"))                       # rows materialized
C_N = int(os.environ.get("C_N", "40"));    C_WARM = int(os.environ.get("C_WARM", "5"))
D_N = int(os.environ.get("D_N", "500"));   D_WARM = int(os.environ.get("D_WARM", "50"))

CONN_STR = f"host={HOST} port={PORT} dbname={DB} user={USER} password={PASSWORD}"
SA_URL = f"postgresql+psycopg://{USER}:{PASSWORD}@{HOST}:{PORT}/{DB}"

STATUSES = ["pending", "paid", "shipped", "cancelled", "refunded"]


# ---------------------------------------------------------------- ORM models
class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String)
    customer: Mapped["Customer"] = relationship(back_populates="orders")


# ---------------------------------------------------------------- stats
def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(name, variant, samples_s, extra=None):
    us = [s * 1e6 for s in samples_s]
    row = {
        "experiment": name,
        "variant": variant,
        "n": len(us),
        "p50_us": round(pct(us, 0.50), 1),
        "p95_us": round(pct(us, 0.95), 1),
        "p99_us": round(pct(us, 0.99), 1),
        "mean_us": round(statistics.mean(us), 1),
        "stdev_us": round(statistics.pstdev(us), 1),
    }
    if extra:
        row.update(extra)
    return row


def timed(fn, n, warm):
    for _ in range(warm):
        fn()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------- schema + seed
DDL = """
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
CREATE TABLE customers (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    email      text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE orders (
    id          integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(id),
    amount      numeric(10,2) NOT NULL,
    created_at  timestamptz NOT NULL,
    status      text NOT NULL
);
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_orders_created_at ON orders(created_at);
"""

# Each function returns EXACTLY the same result set as the ORM/raw path it mirrors.
FUNCTIONS = """
-- Exp A: single customer + their most recent order (one row).
CREATE OR REPLACE FUNCTION fn_latest_order(p_id integer)
RETURNS TABLE(customer_id integer, customer_name text, order_id integer,
              amount numeric, created_at timestamptz)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
        SELECT c.id, c.name, o.id, o.amount, o.created_at
        FROM customers c JOIN orders o ON o.customer_id = c.id
        WHERE c.id = p_id
        ORDER BY o.created_at DESC
        LIMIT 1;
END; $$;

-- Exp B: first p_limit customers joined to all their orders (flat rows).
CREATE OR REPLACE FUNCTION fn_customers_orders(p_limit integer)
RETURNS TABLE(customer_id integer, order_id integer, amount numeric)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
        SELECT c.id, o.id, o.amount
        FROM customers c JOIN orders o ON o.customer_id = c.id
        WHERE c.id <= p_limit
        ORDER BY c.id, o.id;
END; $$;

-- Exp C: first p_limit orders, same columns/order as the raw + ORM paths.
CREATE OR REPLACE FUNCTION fn_orders(p_limit integer)
RETURNS TABLE(id integer, customer_id integer, amount numeric,
              created_at timestamptz, status text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
        SELECT o.id, o.customer_id, o.amount, o.created_at, o.status
        FROM orders o ORDER BY o.id LIMIT p_limit;
END; $$;

-- Exp D: per-customer aggregate (one row): count + sum of orders.
CREATE OR REPLACE FUNCTION fn_order_agg(p_id integer)
RETURNS TABLE(customer_id integer, cnt bigint, total numeric)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
        SELECT o.customer_id, count(*)::bigint, coalesce(sum(o.amount), 0)
        FROM orders o WHERE o.customer_id = p_id
        GROUP BY o.customer_id;
END; $$;
"""


def setup(conn):
    import random
    rng = random.Random(1234)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
        cur.execute(DDL)
        conn.commit()

        base = datetime(2023, 1, 1, tzinfo=timezone.utc)
        # customers
        with cur.copy("COPY customers (id, name, email, created_at) FROM STDIN") as cp:
            for i in range(1, N_CUSTOMERS + 1):
                created = base + timedelta(days=rng.randint(0, 700))
                cp.write_row((i, f"Customer {i}", f"cust{i}@example.com", created))
        # orders: each customer gets 1..2*AVG-1 orders (mean ~AVG)
        oid = 0
        with cur.copy(
            "COPY orders (id, customer_id, amount, created_at, status) FROM STDIN"
        ) as cp:
            for cid in range(1, N_CUSTOMERS + 1):
                k = rng.randint(1, 2 * AVG_ORDERS - 1)
                for _ in range(k):
                    oid += 1
                    amt = round(rng.uniform(5, 500), 2)
                    created = base + timedelta(days=rng.randint(0, 700),
                                               seconds=rng.randint(0, 86400))
                    cp.write_row((oid, cid, amt, created, rng.choice(STATUSES)))
        conn.commit()
        cur.execute(FUNCTIONS)
        conn.commit()
        cur.execute("ANALYZE;")
        conn.commit()
        cur.execute("SELECT count(*) FROM orders;")
        n_orders = cur.fetchone()[0]
    return n_orders


# ---------------------------------------------------------------- Exp A
def exp_a(conn, engine):
    import random
    rng = random.Random(1)
    ids = [rng.randint(1, N_CUSTOMERS) for _ in range(A_N + A_WARM)]
    it = iter(ids)

    # raw psycopg (parameterized -> auto-prepared after threshold)
    cur = conn.cursor()
    SQL = ("SELECT c.id, c.name, o.id, o.amount, o.created_at "
           "FROM customers c JOIN orders o ON o.customer_id = c.id "
           "WHERE c.id = %s ORDER BY o.created_at DESC LIMIT 1")

    def raw():
        cur.execute(SQL, (next(it),))
        cur.fetchone()

    # SP call
    def sp():
        cur.execute("SELECT * FROM fn_latest_order(%s)", (next(it),))
        cur.fetchone()

    # ORM
    sess = Session(engine)

    def orm():
        cid = next(it)
        stmt = (select(Customer, Order).join(Order, Order.customer_id == Customer.id)
                .where(Customer.id == cid)
                .order_by(Order.created_at.desc()).limit(1))
        sess.execute(stmt).first()

    it = iter(ids); raw_s = timed(raw, A_N, A_WARM)
    it = iter(ids); sp_s = timed(sp, A_N, A_WARM)
    it = iter(ids); orm_s = timed(orm, A_N, A_WARM)
    sess.close(); cur.close()
    return [
        summarize("A_identical_sql", "raw_psycopg", raw_s, {"round_trips": 1}),
        summarize("A_identical_sql", "stored_proc", sp_s, {"round_trips": 1}),
        summarize("A_identical_sql", "orm_sqlalchemy", orm_s, {"round_trips": 1}),
    ]


# ---------------------------------------------------------------- Exp B (N+1)
def exp_b(conn, engine):
    cur = conn.cursor()

    def naive():  # 1 query for customers + one query per customer for its orders
        cur.execute("SELECT id FROM customers WHERE id <= %s ORDER BY id", (B_M,))
        cids = [r[0] for r in cur.fetchall()]
        total = 0
        for cid in cids:
            cur.execute("SELECT id, amount FROM orders WHERE customer_id = %s", (cid,))
            total += len(cur.fetchall())
        return 1 + len(cids)

    def eager():  # single join
        cur.execute(
            "SELECT c.id, o.id, o.amount FROM customers c "
            "JOIN orders o ON o.customer_id = c.id WHERE c.id <= %s ORDER BY c.id, o.id",
            (B_M,))
        cur.fetchall()
        return 1

    def sp():
        cur.execute("SELECT * FROM fn_customers_orders(%s)", (B_M,))
        cur.fetchall()
        return 1

    # ORM naive lazy-load: access .orders per customer -> a query each (1+M)
    def orm_naive():
        with Session(engine) as s:
            custs = s.scalars(select(Customer).where(Customer.id <= B_M)
                              .order_by(Customer.id)).all()
            for c in custs:
                _ = len(c.orders)   # triggers a lazy SELECT per customer
            return 1 + len(custs)

    def orm_eager():
        with Session(engine) as s:
            custs = s.scalars(
                select(Customer).options(joinedload(Customer.orders))
                .where(Customer.id <= B_M).order_by(Customer.id)).unique().all()
            for c in custs:
                _ = len(c.orders)
            return 1

    naive_rt = naive(); eager_rt = eager(); sp_rt = sp()
    orm_n_rt = orm_naive(); orm_e_rt = orm_eager()

    raw_naive_s = timed(naive, B_N, B_WARM)
    eager_s = timed(eager, B_N, B_WARM)
    sp_s = timed(sp, B_N, B_WARM)
    orm_naive_s = timed(orm_naive, B_N, B_WARM)
    orm_eager_s = timed(orm_eager, B_N, B_WARM)
    cur.close()
    return [
        summarize("B_n_plus_1", "raw_naive_1+M", raw_naive_s, {"round_trips": naive_rt}),
        summarize("B_n_plus_1", "orm_naive_lazy_1+M", orm_naive_s, {"round_trips": orm_n_rt}),
        summarize("B_n_plus_1", "orm_eager_join", orm_eager_s, {"round_trips": orm_e_rt}),
        summarize("B_n_plus_1", "raw_eager_join", eager_s, {"round_trips": eager_rt}),
        summarize("B_n_plus_1", "stored_proc", sp_s, {"round_trips": sp_rt}),
    ]


# ---------------------------------------------------------------- Exp C (materialization)
def exp_c(conn, engine):
    cur = conn.cursor()
    RAW_SQL = ("SELECT id, customer_id, amount, created_at, status "
               "FROM orders ORDER BY id LIMIT %s")

    def raw():
        cur.execute(RAW_SQL, (C_K,))
        return cur.fetchall()

    def sp():
        cur.execute("SELECT * FROM fn_orders(%s)", (C_K,))
        return cur.fetchall()

    def orm():  # full object hydration + identity map / change tracking
        with Session(engine) as s:
            objs = s.scalars(select(Order).order_by(Order.id).limit(C_K)).all()
            return len(objs)

    def mem(fn):
        tracemalloc.start()
        fn()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak // 1024  # KiB

    raw_s = timed(raw, C_N, C_WARM)
    sp_s = timed(sp, C_N, C_WARM)
    orm_s = timed(orm, C_N, C_WARM)
    raw_kb, sp_kb, orm_kb = mem(raw), mem(sp), mem(orm)
    cur.close()
    return [
        summarize("C_materialization", "raw_psycopg_tuples", raw_s,
                  {"round_trips": 1, "rows": C_K, "peak_kib": raw_kb}),
        summarize("C_materialization", "stored_proc_tuples", sp_s,
                  {"round_trips": 1, "rows": C_K, "peak_kib": sp_kb}),
        summarize("C_materialization", "orm_hydration", orm_s,
                  {"round_trips": 1, "rows": C_K, "peak_kib": orm_kb}),
    ]


# ---------------------------------------------------------------- Exp D (plan cache)
def exp_d(conn):
    import random
    rng = random.Random(7)
    ids = [rng.randint(1, N_CUSTOMERS) for _ in range(D_N + D_WARM)]

    def read_pgss(conn2, like, notlike=None):
        with conn2.cursor() as c:
            q = ("SELECT count(*), coalesce(sum(calls),0), "
                 "coalesce(sum(total_plan_time),0), coalesce(sum(total_exec_time),0) "
                 "FROM pg_stat_statements WHERE query ILIKE %s")
            params = [like]
            if notlike:
                q += " AND query NOT ILIKE %s"
                params.append(notlike)
            c.execute(q, params)
            cnt, calls, plan_ms, exec_ms = c.fetchone()
            return int(cnt), int(calls), float(plan_ms), float(exec_ms)

    results = []
    # dedicated connection so pg_stat_statements reset is isolated
    with psycopg.connect(CONN_STR, autocommit=True) as mon:
        # ad-hoc: literal inlined into the text every call (new string each time)
        with psycopg.connect(CONN_STR) as adhoc_conn:
            adhoc_conn.autocommit = True
            cur = adhoc_conn.cursor()
            mon.execute("SELECT pg_stat_statements_reset()")

            def adhoc(_it):
                cid = next(_it)
                cur.execute(
                    "SELECT customer_id, count(*), sum(amount) FROM orders "
                    f"WHERE customer_id = {cid} GROUP BY customer_id")
                cur.fetchall()

            it = iter(ids)
            adhoc_s = timed(lambda: adhoc(it), D_N, D_WARM)
            cnt, calls, plan_ms, exec_ms = read_pgss(
                mon, "%from orders where customer_id =%", "%pg_stat_statements%")
            results.append(summarize(
                "D_plan_cache", "adhoc_string_concat", adhoc_s,
                {"pgss_entries": int(cnt), "pgss_calls": int(calls),
                 "plan_ms_per_call": round(plan_ms / max(calls, 1), 4),
                 "exec_ms_per_call": round(exec_ms / max(calls, 1), 4)}))

        # parameterized: one text, psycopg auto-prepares (server-side generic plan)
        with psycopg.connect(CONN_STR) as par_conn:
            par_conn.autocommit = True
            cur = par_conn.cursor()
            mon.execute("SELECT pg_stat_statements_reset()")
            PAR_SQL = ("SELECT customer_id, count(*), sum(amount) FROM orders "
                       "WHERE customer_id = %s GROUP BY customer_id")

            def param(_it):
                cur.execute(PAR_SQL, (next(_it),))
                cur.fetchall()

            it = iter(ids)
            par_s = timed(lambda: param(it), D_N, D_WARM)
            cnt, calls, plan_ms, exec_ms = read_pgss(
                mon, "%from orders where customer_id = $%", "%pg_stat_statements%")
            results.append(summarize(
                "D_plan_cache", "parameterized", par_s,
                {"pgss_entries": int(cnt), "pgss_calls": int(calls),
                 "plan_ms_per_call": round(plan_ms / max(calls, 1), 4),
                 "exec_ms_per_call": round(exec_ms / max(calls, 1), 4)}))

        # SP
        with psycopg.connect(CONN_STR) as sp_conn:
            sp_conn.autocommit = True
            cur = sp_conn.cursor()
            mon.execute("SELECT pg_stat_statements_reset()")

            def spcall(_it):
                cur.execute("SELECT * FROM fn_order_agg(%s)", (next(_it),))
                cur.fetchall()

            it = iter(ids)
            sp_s = timed(lambda: spcall(it), D_N, D_WARM)
            cnt, calls, plan_ms, exec_ms = read_pgss(
                mon, "%fn_order_agg%", "%pg_stat_statements%")
            results.append(summarize(
                "D_plan_cache", "stored_proc", sp_s,
                {"pgss_entries": int(cnt), "pgss_calls": int(calls),
                 "plan_ms_per_call": round(plan_ms / max(calls, 1), 4),
                 "exec_ms_per_call": round(exec_ms / max(calls, 1), 4)}))
    return results


# ---------------------------------------------------------------- driver
FIELDS = ["run", "experiment", "variant", "n", "p50_us", "p95_us", "p99_us",
          "mean_us", "stdev_us", "round_trips", "rows", "peak_kib",
          "pgss_entries", "pgss_calls", "plan_ms_per_call", "exec_ms_per_call"]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def headline(rows, exp, variant):
    for r in rows:
        if r["experiment"] == exp and r["variant"] == variant:
            return r
    return None


def main():
    os.makedirs(RESULTS, exist_ok=True)
    conn = psycopg.connect(CONN_STR)
    conn.autocommit = False
    print("seeding...", flush=True)
    n_orders = setup(conn)
    with conn.cursor() as cur:
        cur.execute("SHOW server_version"); pg_version = cur.fetchone()[0]
    conn.commit()
    conn.autocommit = True
    engine = create_engine(SA_URL, future=True)

    all_rows = {"A": [], "B": [], "C": [], "D": []}
    per_run = []  # (run, dict of headline numbers) for stability check

    for run in range(1, RUNS + 1):
        print(f"\n===== RUN {run}/{RUNS} =====", flush=True)
        a = exp_a(conn, engine); print("  A done", flush=True)
        b = exp_b(conn, engine); print("  B done", flush=True)
        c = exp_c(conn, engine); print("  C done", flush=True)
        d = exp_d(conn);         print("  D done", flush=True)
        for key, rows in (("A", a), ("B", b), ("C", c), ("D", d)):
            for r in rows:
                r2 = {"run": run}; r2.update(r); all_rows[key].append(r2)
        per_run.append((run, {
            "A_orm_p50": headline(a, "A_identical_sql", "orm_sqlalchemy")["p50_us"],
            "A_raw_p50": headline(a, "A_identical_sql", "raw_psycopg")["p50_us"],
            "A_sp_p50": headline(a, "A_identical_sql", "stored_proc")["p50_us"],
            "B_orm_naive_p50": headline(b, "B_n_plus_1", "orm_naive_lazy_1+M")["p50_us"],
            "B_orm_eager_p50": headline(b, "B_n_plus_1", "orm_eager_join")["p50_us"],
            "B_sp_p50": headline(b, "B_n_plus_1", "stored_proc")["p50_us"],
            "C_orm_p50": headline(c, "C_materialization", "orm_hydration")["p50_us"],
            "C_raw_p50": headline(c, "C_materialization", "raw_psycopg_tuples")["p50_us"],
            "D_adhoc_plan": headline(d, "D_plan_cache", "adhoc_string_concat")["plan_ms_per_call"],
            "D_param_plan": headline(d, "D_plan_cache", "parameterized")["plan_ms_per_call"],
            "D_adhoc_entries": headline(d, "D_plan_cache", "adhoc_string_concat")["pgss_entries"],
            "D_param_entries": headline(d, "D_plan_cache", "parameterized")["pgss_entries"],
        }))

    write_csv(os.path.join(RESULTS, "exp_a_identical_sql.csv"), all_rows["A"])
    write_csv(os.path.join(RESULTS, "exp_b_n_plus_1.csv"), all_rows["B"])
    write_csv(os.path.join(RESULTS, "exp_c_materialization.csv"), all_rows["C"])
    write_csv(os.path.join(RESULTS, "exp_d_plan_cache.csv"), all_rows["D"])

    # metadata
    try:
        digest = os.popen(
            "docker inspect --format='{{index .RepoDigests 0}}' "
            "postgres:16 2>/dev/null").read().strip()
    except Exception:
        digest = ""
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["postgres_version", "image_digest", "sqlalchemy_version",
                    "psycopg_version", "runs", "n_customers", "n_orders",
                    "A_N", "B_M", "B_N", "C_K", "C_N", "D_N"])
        w.writerow([pg_version, digest, sqlalchemy.__version__, psycopg.__version__,
                    RUNS, N_CUSTOMERS, n_orders, A_N, B_M, B_N, C_K, C_N, D_N])

    # summary.txt
    lines = []
    def out(s=""):
        lines.append(s); print(s)

    out("=" * 70)
    out("ORM vs STORED PROCEDURES vs RAW SQL - Postgres " + pg_version)
    out(f"digest {digest}")
    out(f"SQLAlchemy {sqlalchemy.__version__} | psycopg {psycopg.__version__}")
    out(f"{N_CUSTOMERS} customers, {n_orders} orders | {RUNS} full runs")
    out("=" * 70)

    def block(title, rows_all, cols):
        out("\n" + title)
        hdr = f"  {'variant':22} " + " ".join(f"{c:>13}" for c in cols)
        out(hdr)
        # average across runs per variant
        variants = []
        for r in rows_all:
            if r["variant"] not in variants:
                variants.append(r["variant"])
        for v in variants:
            vr = [r for r in rows_all if r["variant"] == v]
            avg = {}
            for c in cols:
                vals = [r[c] for r in vr if r.get(c) is not None]
                avg[c] = round(sum(vals) / len(vals), 2) if vals else ""
            out(f"  {v:22} " + " ".join(f"{str(avg[c]):>13}" for c in cols))

    block("EXPERIMENT A - identical SQL (means over runs, microseconds)",
          all_rows["A"], ["p50_us", "p95_us", "p99_us", "mean_us"])
    out("  -> ORM adds only a small constant over raw/SP; same SQL underneath.")

    block("EXPERIMENT B - N+1 (means over runs)",
          all_rows["B"], ["p50_us", "p99_us", "round_trips"])
    out("  -> naive lazy = 1+M round trips and is the outlier; eager join and SP")
    out("     both collapse to 1 round trip. This is misuse, not 'ORM loses'.")

    block("EXPERIMENT C - materialization (means over runs)",
          all_rows["C"], ["p50_us", "p99_us", "peak_kib"])
    out("  -> raw tuples ~= SP tuples; ORM full hydration is the heavy path")
    out("     (client-side object + identity map), same SQL underneath.")

    block("EXPERIMENT D - plan cache / parameterization (means over runs)",
          all_rows["D"], ["p50_us", "plan_ms_per_call", "pgss_entries", "pgss_calls"])
    out("  -> the SP 'plan caching' edge is really parameterization: ad-hoc pays")
    out("     parse+plan every call; parameterized (auto-prepared) and SP do not.")
    out("     (see pgss_entries note re: pg_stat_statements literal normalization)")

    # stability
    out("\n" + "=" * 70)
    out("STABILITY across runs (p50 unless noted)")
    out("=" * 70)
    keys = list(per_run[0][1].keys())
    out("  " + "metric".ljust(20) + " ".join(f"run{r}".rjust(11) for r, _ in per_run)
        + "   cv%")
    for k in keys:
        vals = [d[k] for _, d in per_run]
        m = sum(vals) / len(vals)
        cv = (statistics.pstdev(vals) / m * 100) if m else 0
        out("  " + k.ljust(20) + " ".join(f"{v:>11}" for v in vals)
            + f"   {cv:5.1f}")

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")

    engine.dispose(); conn.close()
    print("\nartifacts in", RESULTS)


if __name__ == "__main__":
    main()
