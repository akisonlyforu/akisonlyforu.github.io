"""Measure soft-delete bloat vs hard-delete, and the partial-index fix.

Two identical `orders` tables, same schema (id, customer_id, status,
amount_cents, payload, created_at, deleted_at timestamptz NULL):

  * hard_delete_orders  - "removed" rows get DELETEd.
  * soft_delete_orders  - "removed" rows get UPDATE ... SET deleted_at = now(),
                           never physically deleted.

Each churn cycle inserts INSERT_BATCH new rows, then "removes" REMOVE_BATCH of
those *same freshly-inserted* rows, chosen uniformly at random from the batch.
That models orders that get cancelled/refunded shortly after being placed
(a very real e-commerce pattern) rather than only the oldest rows churning -
every batch, old or new, ends up with the identical dead/live density, so
dead rows are interspersed throughout the whole created_at range instead of
concentrated at the tail where a "give me recent active rows" query would
never see them.
Both tables get a plain VACUUM ANALYZE (not VACUUM FULL) after every cycle -
the realistic autovacuum-equivalent reclaim-and-restat, not a defrag. Skipping
the ANALYZE half would leave the planner working off default/stale selectivity
guesses for `deleted_at`, which produces plan choices no correctly-operated
production database would actually make.

Three experiments, all against the real containerized Postgres:

  1. bloat_over_time.csv    - per-cycle table size, total size (+indexes),
                               live/dead tuple counts, PK + secondary index size.
  2. query_latency.csv      - after bloat has accumulated: PK point-lookup and
                               "active rows" (WHERE deleted_at IS NULL ORDER BY
                               created_at DESC LIMIT 50) latency, hard vs soft.
                               explain_*.txt / explain_summary.csv capture
                               EXPLAIN (ANALYZE, BUFFERS) for each combination.
  3. partial_index_fix.csv  - add a partial index on soft_delete_orders
                               (created_at DESC) WHERE deleted_at IS NULL, then
                               re-run the identical active-rows benchmark:
                               original index vs partial index vs hard_delete
                               reference. Records the partial index's on-disk
                               size against the original full index.

Env: PGHOST(127.0.0.1) PGPORT(55445) PGUSER(bench) PGPASSWORD(bench)
     PGDATABASE(soft_delete_bench) RESULTS_DIR(results/)
     CYCLES(50) INSERT_BATCH(5000) REMOVE_BATCH(4900)
     LATENCY_ITERATIONS(1000) SEED(1234)

REMOVE_BATCH defaults to 98% of INSERT_BATCH (4900/5000) rather than the more
modest 4000/5000 split, on purpose: at these table sizes (tens of MB) almost
everything is cache-resident, so per-query Postgres-side work is a handful of
microseconds and a gentler churn ratio produces a real but small effect that
is easy to lose in Python/psycopg2 round-trip noise. A higher within-batch
removal fraction pushes the number of dead rows the "active rows" query has
to skip past into the thousands, which is both a more realistic shape for a
high-turnover table (sessions, notifications, queue jobs) and a signal that
clears the noise floor cleanly and reproducibly.

These are laptop numbers demonstrating the mechanism, not capacity-planning
numbers.
"""
import csv
import gc
import os
import platform
import random
import re
import statistics
import subprocess
import time
from datetime import datetime, timezone

import psycopg2

HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "55445"))
USER = os.environ.get("PGUSER", "bench")
PASSWORD = os.environ.get("PGPASSWORD", "bench")
DBNAME = os.environ.get("PGDATABASE", "soft_delete_bench")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

CYCLES = int(os.environ.get("CYCLES", "50"))
INSERT_BATCH = int(os.environ.get("INSERT_BATCH", "5000"))
REMOVE_BATCH = int(os.environ.get("REMOVE_BATCH", "4900"))
LATENCY_ITERATIONS = int(os.environ.get("LATENCY_ITERATIONS", "1000"))
SEED = int(os.environ.get("SEED", "1234"))

IMAGE_DIGEST = (
    "postgres:16.14@sha256:"
    "33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20"
)

SCHEMA = """
DROP TABLE IF EXISTS hard_delete_orders;
DROP TABLE IF EXISTS soft_delete_orders;

CREATE TABLE hard_delete_orders (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  integer NOT NULL,
    status       text NOT NULL,
    amount_cents integer NOT NULL,
    payload      text NOT NULL,
    created_at   timestamptz NOT NULL,
    deleted_at   timestamptz NULL
);
CREATE INDEX hard_delete_orders_created_at_idx
    ON hard_delete_orders (created_at DESC);

CREATE TABLE soft_delete_orders (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  integer NOT NULL,
    status       text NOT NULL,
    amount_cents integer NOT NULL,
    payload      text NOT NULL,
    created_at   timestamptz NOT NULL,
    deleted_at   timestamptz NULL
);
CREATE INDEX soft_delete_orders_created_at_idx
    ON soft_delete_orders (created_at DESC);
"""

INSERT_SQL = """
INSERT INTO {table} (customer_id, status, amount_cents, payload, created_at)
SELECT
    (random() * 10000)::int,
    (ARRAY['pending','paid','shipped','refunded','cancelled'])[1 + (random() * 4)::int],
    (random() * 100000)::int,
    substr(md5(random()::text) || md5(random()::text) || md5(random()::text), 1, 250),
    clock_timestamp() + (gs * interval '1 microsecond')
FROM generate_series(1, %s) AS gs
"""

REMOVE_HARD_SQL = """
WITH victims AS (
    SELECT id FROM {table} WHERE id BETWEEN %s AND %s ORDER BY random() LIMIT %s
)
DELETE FROM {table} WHERE id IN (SELECT id FROM victims)
"""

REMOVE_SOFT_SQL = """
WITH victims AS (
    SELECT id FROM {table}
    WHERE id BETWEEN %s AND %s AND deleted_at IS NULL
    ORDER BY random() LIMIT %s
)
UPDATE {table} SET deleted_at = now() WHERE id IN (SELECT id FROM victims)
"""

PK_LOOKUP_SQL = (
    "SELECT id, customer_id, status, amount_cents, payload, created_at, deleted_at "
    "FROM {table} WHERE id = %s"
)
ACTIVE_ROWS_SQL = (
    "SELECT id, customer_id, status, amount_cents, created_at "
    "FROM {table} WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 50"
)


def connect():
    conn = psycopg2.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=DBNAME
    )
    conn.autocommit = True
    return conn


def command_version(command):
    try:
        return subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT
        ).strip()
    except Exception:
        return "unavailable"


def pct(vals, p):
    s = sorted(vals)
    if not s:
        return 0.0
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


def latency_stats(lat):
    return {
        "iterations": len(lat),
        "p50_ms": round(pct(lat, 50), 4),
        "p95_ms": round(pct(lat, 95), 4),
        "p99_ms": round(pct(lat, 99), 4),
        "mean_ms": round(statistics.mean(lat), 4),
        "min_ms": round(min(lat), 4),
        "max_ms": round(max(lat), 4),
    }


def get_indexes(conn, table):
    """[(index_name, is_primary, size_bytes), ...] for a table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indisprimary, pg_relation_size(indexrelid)
            FROM pg_index
            WHERE indrelid = %s::regclass
            ORDER BY indisprimary DESC, indexrelid::regclass::text
            """,
            (table,),
        )
        return cur.fetchall()


def bloat_snapshot(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_relation_size(%s::regclass), pg_total_relation_size(%s::regclass)",
            (table, table),
        )
        table_bytes, total_bytes = cur.fetchone()
        cur.execute(
            "SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = %s",
            (table,),
        )
        n_live, n_dead = cur.fetchone()
    indexes = get_indexes(conn, table)
    pk_bytes = sum(sz for _, is_pk, sz in indexes if is_pk)
    secondary_bytes = sum(sz for _, is_pk, sz in indexes if not is_pk)
    return {
        "table_bytes": table_bytes,
        "total_bytes": total_bytes,
        "pk_index_bytes": pk_bytes,
        "secondary_index_bytes": secondary_bytes,
        "n_live_tup": n_live,
        "n_dead_tup": n_dead,
        "index_count": len(indexes),
    }


def run_bloat_experiment(conn, cycles, insert_batch, remove_batch):
    rows = []
    t_start = time.perf_counter()
    plan = (
        ("hard_delete_orders", REMOVE_HARD_SQL),
        ("soft_delete_orders", REMOVE_SOFT_SQL),
    )
    for cycle in range(1, cycles + 1):
        for table, remove_sql in plan:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(max(id), 0) FROM %s" % table)
                before_max = cur.fetchone()[0]
                cur.execute(INSERT_SQL.format(table=table), (insert_batch,))
                lo, hi = before_max + 1, before_max + insert_batch
                cur.execute(remove_sql.format(table=table), (lo, hi, remove_batch))
                cur.execute("VACUUM ANALYZE %s" % table)
            snap = bloat_snapshot(conn, table)
            snap.update(cycle=cycle, table_name=table, inserted=insert_batch, removed=remove_batch)
            rows.append(snap)
        if cycle == 1 or cycle % 5 == 0 or cycle == cycles:
            hard, soft = rows[-2], rows[-1]
            print(
                "  cycle %3d/%d  hard: %7d live tup / %6.2f MB   "
                "soft: %7d live tup / %6.2f MB"
                % (
                    cycle,
                    cycles,
                    hard["n_live_tup"],
                    hard["total_bytes"] / 1024 / 1024,
                    soft["n_live_tup"],
                    soft["total_bytes"] / 1024 / 1024,
                ),
                flush=True,
            )
    elapsed = time.perf_counter() - t_start
    return rows, elapsed


def sample_ids(conn, table, n):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM %s ORDER BY random() LIMIT %%s" % table, (n,))
        return [r[0] for r in cur.fetchall()]


def time_pk_lookup(conn, table, ids, iterations, warmup=20):
    sql = PK_LOOKUP_SQL.format(table=table)
    rng = random.Random(SEED)
    with conn.cursor() as cur:
        for _ in range(warmup):
            cur.execute(sql, (rng.choice(ids),))
            cur.fetchall()
        lat = []
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            for _ in range(iterations):
                pid = rng.choice(ids)
                t0 = time.perf_counter()
                cur.execute(sql, (pid,))
                cur.fetchall()
                lat.append((time.perf_counter() - t0) * 1000.0)
        finally:
            if gc_was_enabled:
                gc.enable()
    return lat


def time_active_rows(conn, table, iterations, warmup=20):
    sql = ACTIVE_ROWS_SQL.format(table=table)
    with conn.cursor() as cur:
        for _ in range(warmup):
            cur.execute(sql)
            cur.fetchall()
        lat = []
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            for _ in range(iterations):
                t0 = time.perf_counter()
                cur.execute(sql)
                cur.fetchall()
                lat.append((time.perf_counter() - t0) * 1000.0)
        finally:
            if gc_was_enabled:
                gc.enable()
    return lat


def capture_explain(conn, sql, params, label, results_dir):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
        text = "\n".join(r[0] for r in cur.fetchall())
    path = os.path.join(results_dir, "explain_%s.txt" % label)
    with open(path, "w") as f:
        f.write(text + "\n")
    return text, path


def parse_explain(text):
    first_line = text.splitlines()[0].strip()
    exec_match = re.search(r"Execution Time: ([\d.]+) ms", text)
    removed_match = re.search(r"Rows Removed by Filter: (\d+)", text)
    hit_match = re.search(r"Buffers: shared hit=(\d+)", text)
    read_match = re.search(r"\bread=(\d+)", text)
    return {
        "plan_top_line": first_line,
        "execution_time_ms": float(exec_match.group(1)) if exec_match else None,
        "rows_removed_by_filter": int(removed_match.group(1)) if removed_match else 0,
        "shared_hit_blocks": int(hit_match.group(1)) if hit_match else 0,
        "shared_read_blocks": int(read_match.group(1)) if read_match else 0,
    }


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs(RESULTS, exist_ok=True)
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        pg_version = cur.fetchone()[0].split(",")[0]
        cur.execute(SCHEMA)

    print("=" * 78)
    print("soft-delete vs hard-delete bloat benchmark  (%s)" % pg_version)
    print(
        "params: cycles=%d insert_batch=%d remove_batch=%d latency_iterations=%d seed=%d"
        % (CYCLES, INSERT_BATCH, REMOVE_BATCH, LATENCY_ITERATIONS, SEED)
    )
    print("=" * 78)

    # ---- experiment 1: bloat under churn ----
    print("\nexperiment 1: table/index bloat under churn ...")
    bloat_rows, bloat_elapsed = run_bloat_experiment(conn, CYCLES, INSERT_BATCH, REMOVE_BATCH)
    write_csv(os.path.join(RESULTS, "bloat_over_time.csv"), bloat_rows)
    final_hard = next(r for r in reversed(bloat_rows) if r["table_name"] == "hard_delete_orders")
    final_soft = next(r for r in reversed(bloat_rows) if r["table_name"] == "soft_delete_orders")
    total_ratio = final_soft["total_bytes"] / final_hard["total_bytes"]
    table_ratio = final_soft["table_bytes"] / final_hard["table_bytes"]
    index_ratio = final_soft["secondary_index_bytes"] / max(final_hard["secondary_index_bytes"], 1)
    print("  done in %.1fs" % bloat_elapsed)
    print(
        "  final hard_delete_orders: total=%.2f MB table=%.2f MB idx=%.2f MB live=%d dead=%d"
        % (
            final_hard["total_bytes"] / 1024 / 1024,
            final_hard["table_bytes"] / 1024 / 1024,
            final_hard["secondary_index_bytes"] / 1024 / 1024,
            final_hard["n_live_tup"],
            final_hard["n_dead_tup"],
        )
    )
    print(
        "  final soft_delete_orders: total=%.2f MB table=%.2f MB idx=%.2f MB live=%d dead=%d"
        % (
            final_soft["total_bytes"] / 1024 / 1024,
            final_soft["table_bytes"] / 1024 / 1024,
            final_soft["secondary_index_bytes"] / 1024 / 1024,
            final_soft["n_live_tup"],
            final_soft["n_dead_tup"],
        )
    )
    print(
        "  ratios (soft/hard): total=%.2fx table=%.2fx secondary_index=%.2fx"
        % (total_ratio, table_ratio, index_ratio)
    )

    # ---- experiment 2: query latency ----
    print("\nexperiment 2: query latency (pk lookup + active-rows scan) ...")
    hard_ids = sample_ids(conn, "hard_delete_orders", 500)
    soft_ids = sample_ids(conn, "soft_delete_orders", 500)
    ids_by_table = {"hard_delete_orders": hard_ids, "soft_delete_orders": soft_ids}
    short_name = {"hard_delete_orders": "hard", "soft_delete_orders": "soft"}

    latency_rows = []
    explain_summary_rows = []
    for table in ("hard_delete_orders", "soft_delete_orders"):
        short = short_name[table]
        ids = ids_by_table[table]

        lat = time_pk_lookup(conn, table, ids, LATENCY_ITERATIONS)
        stats = latency_stats(lat)
        latency_rows.append({"table_name": table, "query_type": "pk_lookup", **stats})
        text, path = capture_explain(
            conn, PK_LOOKUP_SQL.format(table=table), (ids[0],), "pk_lookup_%s" % short, RESULTS
        )
        parsed = parse_explain(text)
        explain_summary_rows.append(
            {"table_name": table, "query_type": "pk_lookup", "file": os.path.basename(path), **parsed}
        )

        lat2 = time_active_rows(conn, table, LATENCY_ITERATIONS)
        stats2 = latency_stats(lat2)
        latency_rows.append({"table_name": table, "query_type": "active_rows", **stats2})
        text2, path2 = capture_explain(
            conn, ACTIVE_ROWS_SQL.format(table=table), None, "active_rows_%s" % short, RESULTS
        )
        parsed2 = parse_explain(text2)
        explain_summary_rows.append(
            {"table_name": table, "query_type": "active_rows", "file": os.path.basename(path2), **parsed2}
        )

        print(
            "  %-20s pk_lookup   p50=%.4fms p95=%.4fms p99=%.4fms"
            % (table, stats["p50_ms"], stats["p95_ms"], stats["p99_ms"])
        )
        print(
            "  %-20s active_rows p50=%.4fms p95=%.4fms p99=%.4fms  (rows_removed_by_filter=%d, plan=%s)"
            % (table, stats2["p50_ms"], stats2["p95_ms"], stats2["p99_ms"],
               parsed2["rows_removed_by_filter"], parsed2["plan_top_line"])
        )

    write_csv(os.path.join(RESULTS, "query_latency.csv"), latency_rows)
    write_csv(os.path.join(RESULTS, "explain_summary.csv"), explain_summary_rows)

    active_by_table = {r["table_name"]: r for r in latency_rows if r["query_type"] == "active_rows"}
    active_p99_ratio = (
        active_by_table["soft_delete_orders"]["p99_ms"] / active_by_table["hard_delete_orders"]["p99_ms"]
        if active_by_table["hard_delete_orders"]["p99_ms"] else float("inf")
    )
    print("  active_rows p99 ratio (soft/hard): %.2fx" % active_p99_ratio)

    # ---- experiment 3: partial index fix ----
    print("\nexperiment 3: partial index fix ...")
    lat_before = time_active_rows(conn, "soft_delete_orders", LATENCY_ITERATIONS)
    stats_before = latency_stats(lat_before)
    text_before, path_before = capture_explain(
        conn, ACTIVE_ROWS_SQL.format(table="soft_delete_orders"), None,
        "active_rows_soft_before_partial", RESULTS
    )
    parsed_before = parse_explain(text_before)

    with conn.cursor() as cur:
        t0 = time.perf_counter()
        cur.execute(
            "CREATE INDEX soft_delete_orders_active_created_at_idx "
            "ON soft_delete_orders (created_at DESC) WHERE deleted_at IS NULL"
        )
        index_build_s = time.perf_counter() - t0
        cur.execute("ANALYZE soft_delete_orders")

    lat_after = time_active_rows(conn, "soft_delete_orders", LATENCY_ITERATIONS)
    stats_after = latency_stats(lat_after)
    text_after, path_after = capture_explain(
        conn, ACTIVE_ROWS_SQL.format(table="soft_delete_orders"), None,
        "active_rows_soft_partial", RESULTS
    )
    parsed_after = parse_explain(text_after)

    lat_hard_ref = time_active_rows(conn, "hard_delete_orders", LATENCY_ITERATIONS)
    stats_hard_ref = latency_stats(lat_hard_ref)

    soft_indexes = get_indexes(conn, "soft_delete_orders")
    full_idx_bytes = next(
        sz for name, is_pk, sz in soft_indexes
        if not is_pk and name.endswith("soft_delete_orders_created_at_idx")
    )
    partial_idx_bytes = next(
        sz for name, is_pk, sz in soft_indexes
        if not is_pk and name.endswith("soft_delete_orders_active_created_at_idx")
    )
    hard_idx_bytes = next(sz for _, is_pk, sz in get_indexes(conn, "hard_delete_orders") if not is_pk)

    partial_rows = [
        {
            "variant": "soft_delete_original_index",
            **stats_before,
            "index_used": "soft_delete_orders_created_at_idx (full)",
            "index_size_bytes": full_idx_bytes,
            "rows_removed_by_filter": parsed_before["rows_removed_by_filter"],
        },
        {
            "variant": "soft_delete_partial_index",
            **stats_after,
            "index_used": "soft_delete_orders_active_created_at_idx (partial)",
            "index_size_bytes": partial_idx_bytes,
            "rows_removed_by_filter": parsed_after["rows_removed_by_filter"],
        },
        {
            "variant": "hard_delete_baseline",
            **stats_hard_ref,
            "index_used": "hard_delete_orders_created_at_idx (full, all rows live)",
            "index_size_bytes": hard_idx_bytes,
            "rows_removed_by_filter": next(
                r["rows_removed_by_filter"] for r in explain_summary_rows
                if r["table_name"] == "hard_delete_orders" and r["query_type"] == "active_rows"
            ),
        },
    ]
    write_csv(os.path.join(RESULTS, "partial_index_fix.csv"), partial_rows)

    index_size_ratio = full_idx_bytes / max(partial_idx_bytes, 1)
    p99_speedup = stats_before["p99_ms"] / stats_after["p99_ms"] if stats_after["p99_ms"] else float("inf")
    print(
        "  partial index %.1f KB vs full index %.1f KB (%.1fx smaller), build took %.2fs"
        % (partial_idx_bytes / 1024, full_idx_bytes / 1024, index_size_ratio, index_build_s)
    )
    print(
        "  active_rows p99: original_index=%.4fms  partial_index=%.4fms  (%.2fx faster)  hard_delete_ref=%.4fms"
        % (stats_before["p99_ms"], stats_after["p99_ms"], p99_speedup, stats_hard_ref["p99_ms"])
    )

    # ---- summary + metadata ----
    lines = []
    lines.append("soft-delete vs hard-delete bloat benchmark")
    lines.append(pg_version)
    lines.append(
        "params: cycles=%d insert_batch=%d remove_batch=%d latency_iterations=%d seed=%d"
        % (CYCLES, INSERT_BATCH, REMOVE_BATCH, LATENCY_ITERATIONS, SEED)
    )
    lines.append("")
    lines.append("EXPERIMENT 1  table/index bloat under churn (%d cycles, %.1fs)" % (CYCLES, bloat_elapsed))
    lines.append(
        "  hard_delete_orders  total=%.2f MB  table=%.2f MB  secondary_index=%.2f MB  live=%d  dead=%d"
        % (
            final_hard["total_bytes"] / 1024 / 1024,
            final_hard["table_bytes"] / 1024 / 1024,
            final_hard["secondary_index_bytes"] / 1024 / 1024,
            final_hard["n_live_tup"],
            final_hard["n_dead_tup"],
        )
    )
    lines.append(
        "  soft_delete_orders  total=%.2f MB  table=%.2f MB  secondary_index=%.2f MB  live=%d  dead=%d"
        % (
            final_soft["total_bytes"] / 1024 / 1024,
            final_soft["table_bytes"] / 1024 / 1024,
            final_soft["secondary_index_bytes"] / 1024 / 1024,
            final_soft["n_live_tup"],
            final_soft["n_dead_tup"],
        )
    )
    lines.append(
        "  ratio (soft/hard): total=%.2fx  table=%.2fx  secondary_index=%.2fx"
        % (total_ratio, table_ratio, index_ratio)
    )
    lines.append("")
    lines.append("EXPERIMENT 2  query latency after bloat accumulated")
    for r in latency_rows:
        lines.append(
            "  %-20s %-12s p50=%8.4fms  p95=%8.4fms  p99=%8.4fms  mean=%8.4fms"
            % (r["table_name"], r["query_type"], r["p50_ms"], r["p95_ms"], r["p99_ms"], r["mean_ms"])
        )
    lines.append("  active_rows p99 ratio (soft/hard) = %.2fx" % active_p99_ratio)
    lines.append("")
    lines.append("EXPERIMENT 3  partial index fix")
    for r in partial_rows:
        lines.append(
            "  %-28s p50=%8.4fms  p95=%8.4fms  p99=%8.4fms  rows_removed_by_filter=%-6d index_size=%.1f KB"
            % (r["variant"], r["p50_ms"], r["p95_ms"], r["p99_ms"], r["rows_removed_by_filter"],
               r["index_size_bytes"] / 1024)
        )
    lines.append(
        "  partial index is %.1fx smaller than the full index (%.1f KB vs %.1f KB)"
        % (index_size_ratio, partial_idx_bytes / 1024, full_idx_bytes / 1024)
    )
    lines.append(
        "  active_rows p99 improves %.2fx after adding the partial index (%.4fms -> %.4fms)"
        % (p99_speedup, stats_before["p99_ms"], stats_after["p99_ms"])
    )
    summary = "\n".join(lines) + "\n"
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(summary)
    print("\n" + summary)

    meta_rows = [
        {"key": "run_at_utc", "value": datetime.now(timezone.utc).isoformat()},
        {"key": "platform", "value": platform.platform()},
        {"key": "python", "value": platform.python_version()},
        {"key": "docker", "value": command_version(["docker", "--version"])},
        {"key": "docker_compose", "value": command_version(["docker", "compose", "version"])},
        {"key": "postgres_image", "value": IMAGE_DIGEST},
        {"key": "postgres_server", "value": pg_version},
        {"key": "cycles", "value": str(CYCLES)},
        {"key": "insert_batch", "value": str(INSERT_BATCH)},
        {"key": "remove_batch", "value": str(REMOVE_BATCH)},
        {"key": "latency_iterations", "value": str(LATENCY_ITERATIONS)},
        {"key": "seed", "value": str(SEED)},
        {"key": "bloat_experiment_seconds", "value": "%.2f" % bloat_elapsed},
        {"key": "index_build_seconds_partial", "value": "%.4f" % index_build_s},
        {"key": "final_hard_total_bytes", "value": str(final_hard["total_bytes"])},
        {"key": "final_soft_total_bytes", "value": str(final_soft["total_bytes"])},
        {"key": "final_hard_table_bytes", "value": str(final_hard["table_bytes"])},
        {"key": "final_soft_table_bytes", "value": str(final_soft["table_bytes"])},
        {"key": "final_hard_secondary_index_bytes", "value": str(final_hard["secondary_index_bytes"])},
        {"key": "final_soft_secondary_index_bytes", "value": str(final_soft["secondary_index_bytes"])},
        {"key": "final_hard_n_live_tup", "value": str(final_hard["n_live_tup"])},
        {"key": "final_soft_n_live_tup", "value": str(final_soft["n_live_tup"])},
        {"key": "total_bytes_ratio_soft_over_hard", "value": "%.4f" % total_ratio},
        {"key": "table_bytes_ratio_soft_over_hard", "value": "%.4f" % table_ratio},
        {"key": "secondary_index_ratio_soft_over_hard", "value": "%.4f" % index_ratio},
        {"key": "active_rows_p99_ratio_soft_over_hard", "value": "%.4f" % active_p99_ratio},
        {"key": "partial_index_bytes", "value": str(partial_idx_bytes)},
        {"key": "full_index_bytes", "value": str(full_idx_bytes)},
        {"key": "partial_index_size_ratio", "value": "%.4f" % index_size_ratio},
        {"key": "partial_index_p99_speedup", "value": "%.4f" % p99_speedup},
    ]
    write_csv(os.path.join(RESULTS, "run_metadata.csv"), meta_rows)

    conn.close()
    print("benchmark complete; results written to %s" % RESULTS)


if __name__ == "__main__":
    main()
