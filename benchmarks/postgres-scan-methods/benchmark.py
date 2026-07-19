#!/usr/bin/env python3
"""Reproduce Seq Scan vs Index Scan vs Index-Only Scan on PostgreSQL 16.

Three experiments against one large, deterministically seeded `events` table:

  1. Point lookup .............. Seq Scan (no index) vs Index Scan (b-tree)
  2. Covering query ............ Index Scan (heap fetch) vs Index-Only Scan
                                 (Heap Fetches: 0, but only after VACUUM)
  3. Selectivity crossover ..... where the planner drops the index for a
                                 Seq Scan as the predicate widens

Everything is measured, nothing is fabricated. Latency distributions come from
Python `perf_counter` reps around `cur.execute`; buffers / heap fetches / plan
node types come from `EXPLAIN (ANALYZE, BUFFERS)`. Each query is warmed once,
untimed, before it is measured, so we compare steady-state plan behaviour, not
cold-cache IO.
"""

import argparse
import csv
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = Path(os.environ.get("RESULTS_DIR", str(ROOT / "results")))

PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "55434")
PGUSER = os.environ.get("PGUSER", "scan_bench")
PGDATABASE = os.environ.get("PGDATABASE", "scan_bench")
PGPASSWORD = os.environ.get("PGPASSWORD", "scan_bench")

POSTGRES_IMAGE = (
    "postgres:16.14@sha256:"
    "33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20"
)

# Odd multiplier, coprime with the 1e6 modulus, so user_id spreads across the
# key space (~5 rows per user_id at the default 5,000,000 row / 1,000,000 user
# shape) while staying fully deterministic -- no random().
USER_MULT = 2654435761

# Session settings pinned so the story is about scan methods, not core count or
# JIT warmup. Disclosed in run_metadata.csv and summary.txt.
SESSION_SETTINGS = {
    "work_mem": "'64MB'",
    "max_parallel_workers_per_gather": "0",
    "max_parallel_maintenance_workers": "0",
    "jit": "off",
    "track_io_timing": "on",
}


# --------------------------------------------------------------------------- #
# connection / plumbing
# --------------------------------------------------------------------------- #
def db_connect():
    conn = psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER,
        dbname=PGDATABASE, password=PGPASSWORD,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        for name, value in SESSION_SETTINGS.items():
            cur.execute("SET %s = %s" % (name, value))
    return conn


def wait_for_postgres(timeout=90.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            conn = db_connect()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.close()
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("PostgreSQL did not become ready: %s" % last_error)


def verify_benchmark_database(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        database_name = cur.fetchone()[0]
        cur.execute(
            "SELECT marker FROM scan_benchmark_identity "
            "WHERE marker = 'scan_bench_v1'"
        )
        marker = cur.fetchone()
    if database_name != "scan_bench" or marker != ("scan_bench_v1",):
        raise RuntimeError(
            "refusing destructive seed: not the dedicated scan_bench database"
        )


def command_version(command):
    try:
        return subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT
        ).strip()
    except Exception:  # noqa: BLE001
        return "unavailable"


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
def seed(conn, rows, user_mod):
    print(
        "seeding %s rows (~%s distinct user_id, ~%.1f rows each)"
        % (f"{rows:,}", f"{user_mod:,}", rows / user_mod),
        flush=True,
    )
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SET synchronous_commit = off")
        cur.execute("SET maintenance_work_mem = '1GB'")
        cur.execute("TRUNCATE events")
        cur.execute("DROP INDEX IF EXISTS idx_events_user_id")
        cur.execute("DROP INDEX IF EXISTS idx_events_user_id_covering")
        cur.execute("DROP INDEX IF EXISTS idx_events_bucket")
        cur.execute(
            """
            INSERT INTO events (id, user_id, status, amount, bucket, created_at)
            SELECT
                g,
                ((g * {mult}) % {user_mod}) + 1,
                (g % 5)::smallint,
                ((g * 7) % 100000)::int,
                (g % 1000)::int,
                TIMESTAMPTZ '2026-01-01 00:00:00+00' + g * INTERVAL '1 second'
            FROM generate_series(1, {rows}) AS g
            """.format(mult=USER_MULT, user_mod=int(user_mod), rows=int(rows))
        )
        cur.execute("RESET maintenance_work_mem")
        cur.execute("RESET synchronous_commit")
        cur.execute("ANALYZE events")
    elapsed = time.monotonic() - started
    print("seed + ANALYZE finished in %.1fs" % elapsed, flush=True)
    return elapsed


def validate_seed(conn, rows):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT user_id) FROM events")
        total, distinct_users = cur.fetchone()
        cur.execute("SELECT min(bucket), max(bucket) FROM events")
        bmin, bmax = cur.fetchone()
    if total != rows:
        raise RuntimeError("seed row count wrong: expected %s got %s" % (rows, total))
    if (bmin, bmax) != (0, 999):
        raise RuntimeError("bucket range wrong: got %s..%s" % (bmin, bmax))
    print(
        "validated: %s rows, %s distinct user_id, buckets %s..%s"
        % (f"{total:,}", f"{distinct_users:,}", bmin, bmax),
        flush=True,
    )
    return total, distinct_users


# --------------------------------------------------------------------------- #
# EXPLAIN parsing + latency reps
# --------------------------------------------------------------------------- #
PLAN_NODE_ORDER = (
    "Index Only Scan",
    "Bitmap Heap Scan",
    "Bitmap Index Scan",
    "Index Scan",
    "Seq Scan",
)


def classify_plan(plan_text):
    for node in PLAN_NODE_ORDER:
        if node in plan_text:
            return node
    return "unknown"


def explain_capture(conn, query, params, plan_path):
    """Run EXPLAIN (ANALYZE, BUFFERS) once, save full text, parse key fields."""
    with conn.cursor() as cur:
        cur.execute(
            "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT TEXT) " + query, params
        )
        plan = "\n".join(row[0] for row in cur.fetchall()) + "\n"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan, encoding="utf-8")

    exec_match = re.search(r"Execution Time: ([0-9.]+) ms", plan)
    heap_match = re.search(r"Heap Fetches: (\d+)", plan)
    # first Buffers line = outermost node = cumulative total for the plan
    buffers_match = re.search(r"Buffers: shared ([^\n]+)", plan)
    detail = buffers_match.group(1) if buffers_match else ""
    hit_match = re.search(r"hit=(\d+)", detail)
    read_match = re.search(r"read=(\d+)", detail)
    return {
        "plan_type": classify_plan(plan),
        "execution_time_ms": float(exec_match.group(1)) if exec_match else None,
        "heap_fetches": int(heap_match.group(1)) if heap_match else None,
        "shared_hit": int(hit_match.group(1)) if hit_match else 0,
        "shared_read": int(read_match.group(1)) if read_match else 0,
        "plan_file": plan_path.name,
    }


def time_reps(conn, query, params, reps, warmups=1):
    """Warm the query untimed, then time `reps` steady-state executions (ms)."""
    with conn.cursor() as cur:
        for _ in range(warmups):
            cur.execute(query, params)
            cur.fetchall()
        samples = []
        for _ in range(reps):
            start = time.perf_counter()
            cur.execute(query, params)
            cur.fetchall()
            samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def summarize(samples):
    ordered = sorted(samples)
    n = len(ordered)
    p95_index = min(n - 1, int(round(0.95 * (n - 1))))
    return {
        "reps": n,
        "min_ms": round(ordered[0], 4),
        "median_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(ordered[p95_index], 4),
        "mean_ms": round(statistics.fmean(ordered), 4),
    }


def scan_row_count(conn, query, params):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return len(cur.fetchall())


# --------------------------------------------------------------------------- #
# Experiment 1 -- point lookup: Seq Scan vs Index Scan
# --------------------------------------------------------------------------- #
def experiment_1(conn, results_dir, reps):
    print("\n== Experiment 1: point lookup (Seq Scan vs Index Scan) ==", flush=True)
    query = "SELECT * FROM events WHERE user_id = %s"

    # Deterministically pick a user_id that certainly exists: the user_id of a
    # known id. It matches at least that row (and typically ~5).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events")
        total = cur.fetchone()[0]
        probe_id = total // 2
        cur.execute("SELECT user_id FROM events WHERE id = %s", (probe_id,))
        target_user = cur.fetchone()[0]
        cur.execute("DROP INDEX IF EXISTS idx_events_user_id")
    params = (target_user,)
    rows_returned = scan_row_count(conn, query, params)
    print("target user_id=%s matches %s rows" % (target_user, rows_returned), flush=True)

    rows = []
    latency_files = {}

    # --- no index -> Seq Scan ---
    seq = explain_capture(conn, query, params, results_dir / "explain_exp1_seqscan.txt")
    seq_samples = time_reps(conn, query, params, reps)
    seq_stats = summarize(seq_samples)
    latency_files["exp1_latency_seqscan.csv"] = seq_samples
    rows.append({
        "method": "seq_scan_no_index", "target_user_id": target_user,
        "rows_returned": rows_returned, **seq, **seq_stats,
    })
    print("  Seq Scan   : %-16s exec=%s ms  hit=%s read=%s  median=%s ms"
          % (seq["plan_type"], seq["execution_time_ms"], seq["shared_hit"],
             seq["shared_read"], seq_stats["median_ms"]), flush=True)

    # --- b-tree index -> Index Scan ---
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX idx_events_user_id ON events (user_id)")
        cur.execute("ANALYZE events")
    idx = explain_capture(conn, query, params, results_dir / "explain_exp1_indexscan.txt")
    idx_samples = time_reps(conn, query, params, reps)
    idx_stats = summarize(idx_samples)
    latency_files["exp1_latency_indexscan.csv"] = idx_samples
    rows.append({
        "method": "index_scan_btree", "target_user_id": target_user,
        "rows_returned": rows_returned, **idx, **idx_stats,
    })
    print("  Index Scan : %-16s exec=%s ms  hit=%s read=%s  median=%s ms"
          % (idx["plan_type"], idx["execution_time_ms"], idx["shared_hit"],
             idx["shared_read"], idx_stats["median_ms"]), flush=True)

    fieldnames = [
        "method", "target_user_id", "rows_returned", "plan_type",
        "execution_time_ms", "heap_fetches", "shared_hit", "shared_read",
        "reps", "min_ms", "median_ms", "p95_ms", "mean_ms", "plan_file",
    ]
    write_csv(results_dir / "exp1_point_lookup.csv", fieldnames, rows)
    for name, samples in latency_files.items():
        write_csv(results_dir / name, ["rep", "latency_ms"],
                  [{"rep": i + 1, "latency_ms": round(v, 5)}
                   for i, v in enumerate(samples)])
    return {"target_user": target_user, "rows_returned": rows_returned,
            "seq": rows[0], "index": rows[1]}


# --------------------------------------------------------------------------- #
# Experiment 2 -- covering query: Index Scan vs Index-Only Scan (VACUUM)
# --------------------------------------------------------------------------- #
def experiment_2(conn, results_dir, reps, range_width):
    print("\n== Experiment 2: covering query (Index Scan vs Index-Only Scan) ==",
          flush=True)
    query = "SELECT id FROM events WHERE user_id BETWEEN %s AND %s"

    # Disable bitmap scans for this experiment so the planner picks the pure
    # Index Scan (plain index -> heap access) vs Index Only Scan (covering
    # index -> no heap) contrast the experiment is about. A Bitmap Heap Scan is
    # the natural middle-ground pick for scattered rows, but it neither reports
    # a "Heap Fetches" line nor lets an index-only path show through -- so it
    # hides the exact mechanism (heap fetches N vs 0) this experiment isolates.
    # Experiment 3 leaves the planner completely free, bitmap included.
    with conn.cursor() as cur:
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SELECT max(user_id) FROM events")
        max_user = cur.fetchone()[0]
    lo = max_user // 2
    hi = lo + range_width
    params = (lo, hi)
    rows_returned = scan_row_count(conn, query, params)
    print("range user_id in [%s, %s] matches %s rows" % (lo, hi, rows_returned),
          flush=True)

    rows = []
    latency_files = {}

    # --- plain (user_id) index: id is not in the index -> heap fetch needed ---
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_events_user_id_covering")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_user_id ON events (user_id)")
        cur.execute("ANALYZE events")
    plain = explain_capture(conn, query, params,
                            results_dir / "explain_exp2_index_scan.txt")
    plain_samples = time_reps(conn, query, params, reps)
    plain_stats = summarize(plain_samples)
    latency_files["exp2_latency_index_scan.csv"] = plain_samples
    rows.append({
        "method": "plain_index_heap_fetch", "range_lo": lo, "range_hi": hi,
        "rows_returned": rows_returned, **plain, **plain_stats,
    })
    print("  plain index      : %-18s exec=%s ms  heap_fetches=%s  hit=%s read=%s  median=%s ms"
          % (plain["plan_type"], plain["execution_time_ms"], plain["heap_fetches"],
             plain["shared_hit"], plain["shared_read"], plain_stats["median_ms"]),
          flush=True)

    # --- covering index, BEFORE vacuum: index-only scan still hits the heap ---
    with conn.cursor() as cur:
        cur.execute("DROP INDEX idx_events_user_id")
        cur.execute(
            "CREATE INDEX idx_events_user_id_covering "
            "ON events (user_id) INCLUDE (id)"
        )
        cur.execute("ANALYZE events")
    before = explain_capture(
        conn, query, params,
        results_dir / "attempts" / "explain_exp2_index_only_before_vacuum.txt",
    )
    rows.append({
        "method": "covering_index_only_BEFORE_vacuum", "range_lo": lo, "range_hi": hi,
        "rows_returned": rows_returned, **before,
        "reps": 0, "min_ms": None, "median_ms": None, "p95_ms": None, "mean_ms": None,
    })
    print("  covering (before VACUUM): %-14s heap_fetches=%s  hit=%s read=%s"
          % (before["plan_type"], before["heap_fetches"],
             before["shared_hit"], before["shared_read"]), flush=True)

    # --- VACUUM sets the visibility map ---
    with conn.cursor() as cur:
        cur.execute("VACUUM (ANALYZE) events")

    # --- covering index, AFTER vacuum: Index-Only Scan, Heap Fetches: 0 ---
    after = explain_capture(conn, query, params,
                            results_dir / "explain_exp2_index_only.txt")
    after_samples = time_reps(conn, query, params, reps)
    after_stats = summarize(after_samples)
    latency_files["exp2_latency_index_only.csv"] = after_samples
    rows.append({
        "method": "covering_index_only_AFTER_vacuum", "range_lo": lo, "range_hi": hi,
        "rows_returned": rows_returned, **after, **after_stats,
    })
    print("  covering (after VACUUM) : %-14s exec=%s ms  heap_fetches=%s  hit=%s read=%s  median=%s ms"
          % (after["plan_type"], after["execution_time_ms"], after["heap_fetches"],
             after["shared_hit"], after["shared_read"], after_stats["median_ms"]),
          flush=True)

    with conn.cursor() as cur:
        cur.execute("RESET enable_bitmapscan")

    fieldnames = [
        "method", "range_lo", "range_hi", "rows_returned", "plan_type",
        "execution_time_ms", "heap_fetches", "shared_hit", "shared_read",
        "reps", "min_ms", "median_ms", "p95_ms", "mean_ms", "plan_file",
    ]
    write_csv(results_dir / "exp2_covering.csv", fieldnames, rows)
    for name, samples in latency_files.items():
        write_csv(results_dir / name, ["rep", "latency_ms"],
                  [{"rep": i + 1, "latency_ms": round(v, 5)}
                   for i, v in enumerate(samples)])
    return {"range": (lo, hi), "rows_returned": rows_returned,
            "plain": rows[0], "before": rows[1], "after": rows[2]}


# --------------------------------------------------------------------------- #
# Experiment 3 -- selectivity crossover: when the planner drops the index
# --------------------------------------------------------------------------- #
def experiment_3(conn, results_dir, reps, sweep):
    print("\n== Experiment 3: selectivity crossover (index vs Seq Scan) ==",
          flush=True)
    # sum(amount): amount is NOT in the bucket index, so the index path must
    # fetch every matching row from the heap. That heap access is what gets
    # expensive as the predicate widens and is what lets a Seq Scan eventually
    # win -- the classic crossover. (A count(*) here would be an Index-Only
    # Scan that never touches the heap and never loses, so it would show no
    # crossover at all -- see README.)
    query = "SELECT sum(amount) FROM events WHERE bucket < %s"
    count_query = "SELECT count(*) FROM events WHERE bucket < %s"

    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_events_bucket")
        cur.execute("CREATE INDEX idx_events_bucket ON events (bucket)")
        cur.execute("VACUUM (ANALYZE) events")  # all-visible heap: fair to both plans
        cur.execute("SELECT count(*) FROM events")
        total = cur.fetchone()[0]

    rows = []
    for n in sweep:
        params = (n,)
        with conn.cursor() as cur:
            cur.execute(count_query, params)
            matched = cur.fetchone()[0]
        selectivity = 100.0 * matched / total

        # planner's own choice (nothing forced)
        with conn.cursor() as cur:
            cur.execute("RESET enable_seqscan")
            cur.execute("RESET enable_indexscan")
            cur.execute("RESET enable_bitmapscan")
            cur.execute("RESET enable_indexonlyscan")
        planner = explain_capture(
            conn, query, params,
            results_dir / "attempts" / ("explain_exp3_planner_n%s.txt" % n),
        )
        planner_samples = time_reps(conn, query, params, reps)
        planner_stats = summarize(planner_samples)

        # force a pure Index Scan with heap access (no seq, no bitmap): this is
        # the curve whose per-row random heap fetches eventually lose to a Seq
        # Scan. Bitmap still shows up in the planner's free choice above.
        with conn.cursor() as cur:
            cur.execute("SET enable_seqscan = off")
            cur.execute("SET enable_bitmapscan = off")
            cur.execute("SET enable_indexonlyscan = off")
            cur.execute("RESET enable_indexscan")
        forced_index = explain_capture(
            conn, query, params,
            results_dir / "attempts" / ("explain_exp3_force_index_n%s.txt" % n),
        )
        index_samples = time_reps(conn, query, params, reps)
        index_stats = summarize(index_samples)

        # force the sequential path (no index of any kind)
        with conn.cursor() as cur:
            cur.execute("RESET enable_seqscan")
            cur.execute("SET enable_indexscan = off")
            cur.execute("SET enable_bitmapscan = off")
            cur.execute("SET enable_indexonlyscan = off")
        forced_seq = explain_capture(
            conn, query, params,
            results_dir / "attempts" / ("explain_exp3_force_seq_n%s.txt" % n),
        )
        seq_samples = time_reps(conn, query, params, reps)
        seq_stats = summarize(seq_samples)

        with conn.cursor() as cur:
            cur.execute("RESET enable_seqscan")
            cur.execute("RESET enable_indexscan")
            cur.execute("RESET enable_bitmapscan")
            cur.execute("RESET enable_indexonlyscan")

        winner = "index" if index_stats["median_ms"] < seq_stats["median_ms"] else "seq"
        row = {
            "n": n, "predicate": "bucket < %s" % n,
            "rows_matched": matched, "selectivity_pct": round(selectivity, 4),
            "planner_choice": planner["plan_type"],
            "planner_median_ms": planner_stats["median_ms"],
            "forced_index_plan": forced_index["plan_type"],
            "index_median_ms": index_stats["median_ms"],
            "index_min_ms": index_stats["min_ms"],
            "forced_seq_plan": forced_seq["plan_type"],
            "seq_median_ms": seq_stats["median_ms"],
            "seq_min_ms": seq_stats["min_ms"],
            "faster_path": winner,
        }
        rows.append(row)
        print("  n=%-3d sel=%5.1f%%  planner=%-16s  index=%8.3f ms (%s)  seq=%8.3f ms  -> %s"
              % (n, selectivity, planner["plan_type"], index_stats["median_ms"],
                 forced_index["plan_type"], seq_stats["median_ms"], winner),
              flush=True)

    fieldnames = [
        "n", "predicate", "rows_matched", "selectivity_pct",
        "planner_choice", "planner_median_ms",
        "forced_index_plan", "index_median_ms", "index_min_ms",
        "forced_seq_plan", "seq_median_ms", "seq_min_ms", "faster_path",
    ]
    write_csv(results_dir / "exp3_crossover.csv", fieldnames, rows)

    # find the crossover: first n where seq becomes the faster path
    crossover = None
    for prev, cur_row in zip(rows, rows[1:]):
        if prev["faster_path"] == "index" and cur_row["faster_path"] == "seq":
            crossover = (prev, cur_row)
            break
    bitmap_seen = any("Bitmap" in r["planner_choice"] for r in rows)
    return {"rows": rows, "crossover": crossover, "bitmap_seen": bitmap_seen,
            "total": total}


# --------------------------------------------------------------------------- #
# metadata + summary
# --------------------------------------------------------------------------- #
def write_metadata(results_dir, conn, args, total_rows, distinct_users, seed_seconds):
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        server_version = cur.fetchone()[0]
        cur.execute(
            """
            SELECT name, setting FROM pg_settings
            WHERE name IN (
                'random_page_cost', 'seq_page_cost', 'cpu_tuple_cost',
                'cpu_index_tuple_cost', 'effective_cache_size', 'shared_buffers',
                'work_mem', 'jit', 'max_parallel_workers_per_gather',
                'track_io_timing'
            )
            ORDER BY name
            """
        )
        settings = cur.fetchall()
        cur.execute(
            "SELECT pg_size_pretty(pg_total_relation_size('events')), "
            "pg_size_pretty(pg_relation_size('events'))"
        )
        total_size, heap_size = cur.fetchone()
    rows = [
        {"key": "run_at_utc", "value": datetime.now(timezone.utc).isoformat()},
        {"key": "platform", "value": platform.platform()},
        {"key": "python", "value": platform.python_version()},
        {"key": "docker", "value": command_version(["docker", "--version"])},
        {"key": "docker_compose",
         "value": command_version(["docker", "compose", "version"])},
        {"key": "postgres_image", "value": POSTGRES_IMAGE},
        {"key": "postgres_server", "value": server_version},
        {"key": "seed_rows_requested", "value": str(args.rows)},
        {"key": "seed_rows_actual", "value": str(total_rows)},
        {"key": "distinct_user_id", "value": str(distinct_users)},
        {"key": "user_id_modulus", "value": str(args.rows // 5)},
        {"key": "events_total_size", "value": total_size},
        {"key": "events_heap_size", "value": heap_size},
        {"key": "reps", "value": str(args.reps)},
        {"key": "reps_exp3", "value": str(args.reps_exp3)},
        {"key": "exp2_range_width", "value": str(args.range_width)},
        {"key": "exp3_sweep", "value": ";".join(map(str, args.sweep))},
        {"key": "query_warmup_runs", "value": "1"},
        {"key": "seed_seconds", "value": "%.3f" % seed_seconds},
    ]
    rows.extend({"key": "pg_" + name, "value": value} for name, value in settings)
    write_csv(results_dir / "run_metadata.csv", ["key", "value"], rows)
    return server_version, total_size


def write_summary(results_dir, exp1, exp2, exp3, server_version, total_rows,
                  distinct_users, events_size):
    lines = []
    w = lines.append
    w("PostgreSQL scan-method benchmark -- Seq Scan vs Index Scan vs Index-Only Scan")
    w("=" * 78)
    w("")
    w("server : %s" % server_version)
    w("image  : %s" % POSTGRES_IMAGE)
    w("table  : events, %s rows, %s distinct user_id, %s on disk"
      % (f"{total_rows:,}", f"{distinct_users:,}", events_size))
    w("method : each query warmed once (untimed), then timed steady-state over reps")
    w("         via perf_counter; buffers/heap-fetches/plan from EXPLAIN(ANALYZE,BUFFERS)")
    w("         parallelism OFF (max_parallel_workers_per_gather=0), jit OFF -- so the")
    w("         numbers isolate the scan method, not core count or JIT warmup.")
    w("")

    w("-- Experiment 1: point lookup  SELECT * FROM events WHERE user_id = k --")
    seq, idx = exp1["seq"], exp1["index"]
    w("target user_id=%s, %s rows returned" % (exp1["target_user"], exp1["rows_returned"]))
    w("  Seq Scan (no index) : plan=%s  median=%s ms  exec=%s ms  buffers hit=%s read=%s"
      % (seq["plan_type"], seq["median_ms"], seq["execution_time_ms"],
         seq["shared_hit"], seq["shared_read"]))
    w("  Index Scan (b-tree) : plan=%s  median=%s ms  exec=%s ms  buffers hit=%s read=%s"
      % (idx["plan_type"], idx["median_ms"], idx["execution_time_ms"],
         idx["shared_hit"], idx["shared_read"]))
    if idx["median_ms"] and seq["median_ms"]:
        w("  speedup (median)    : %.1fx   buffers touched: %s -> %s"
          % (seq["median_ms"] / idx["median_ms"],
             seq["shared_hit"] + seq["shared_read"],
             idx["shared_hit"] + idx["shared_read"]))
    w("")

    w("-- Experiment 2: covering query  SELECT id FROM events WHERE user_id BETWEEN a AND b --")
    plain, before, after = exp2["plain"], exp2["before"], exp2["after"]
    w("range matches %s rows" % exp2["rows_returned"])
    w("  plain index (heap fetch) : plan=%s  median=%s ms  heap_fetches=%s  buffers hit=%s read=%s"
      % (plain["plan_type"], plain["median_ms"], plain["heap_fetches"],
         plain["shared_hit"], plain["shared_read"]))
    w("  covering BEFORE VACUUM   : plan=%s  heap_fetches=%s  buffers hit=%s read=%s   (kept under attempts/)"
      % (before["plan_type"], before["heap_fetches"],
         before["shared_hit"], before["shared_read"]))
    w("  covering AFTER VACUUM    : plan=%s  median=%s ms  heap_fetches=%s  buffers hit=%s read=%s"
      % (after["plan_type"], after["median_ms"], after["heap_fetches"],
         after["shared_hit"], after["shared_read"]))
    w("  => VACUUM sets the visibility map; heap fetches %s -> %s, buffers %s -> %s"
      % (before["heap_fetches"], after["heap_fetches"],
         plain["shared_hit"] + plain["shared_read"],
         after["shared_hit"] + after["shared_read"]))
    w("")

    w("-- Experiment 3: selectivity crossover  SELECT sum(amount) FROM events WHERE bucket < n --")
    w("   (sum(amount) forces heap access; index_ms is a forced pure Index Scan,")
    w("    planner_choice is the planner's own free pick incl. bitmap/seq)")
    w("  %-4s %-8s %-18s %-12s %-12s %s"
      % ("n", "sel%", "planner_choice", "index_ms", "seq_ms", "faster"))
    for r in exp3["rows"]:
        w("  %-4d %-8.2f %-18s %-12s %-12s %s"
          % (r["n"], r["selectivity_pct"], r["planner_choice"],
             r["index_median_ms"], r["seq_median_ms"], r["faster_path"]))
    if exp3["crossover"]:
        prev, nxt = exp3["crossover"]
        w("  crossover: index wins up to sel=%.2f%% (n=%d); seq overtakes by sel=%.2f%% (n=%d)"
          % (prev["selectivity_pct"], prev["n"],
             nxt["selectivity_pct"], nxt["n"]))
    else:
        w("  crossover: no index->seq flip observed across the swept range")
    w("  bitmap heap scan appeared in a planner choice: %s" % exp3["bitmap_seen"])
    w("")
    w("These are laptop numbers for demonstrating plan mechanics, not capacity planning.")
    (results_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run_all(args):
    if not args.reset:
        raise RuntimeError("benchmark truncates the events table; rerun with --reset")
    if args.rows % 5:
        raise ValueError("--rows must be divisible by 5")
    results_dir = args.results
    (results_dir / "attempts").mkdir(parents=True, exist_ok=True)

    wait_for_postgres()
    conn = db_connect()
    try:
        verify_benchmark_database(conn)
        user_mod = args.rows // 5
        seed_seconds = seed(conn, args.rows, user_mod)
        total_rows, distinct_users = validate_seed(conn, args.rows)

        exp1 = experiment_1(conn, results_dir, args.reps)
        exp2 = experiment_2(conn, results_dir, args.reps, args.range_width)
        exp3 = experiment_3(conn, results_dir, args.reps_exp3, args.sweep)

        server_version, events_size = write_metadata(
            results_dir, conn, args, total_rows, distinct_users, seed_seconds
        )
        write_summary(results_dir, exp1, exp2, exp3, server_version,
                      total_rows, distinct_users, events_size)

        # honesty guards: the story must actually have happened locally
        if exp1["seq"]["plan_type"] != "Seq Scan":
            raise RuntimeError("Experiment 1 did not produce a Seq Scan without the index")
        if "Index" not in exp1["index"]["plan_type"]:
            raise RuntimeError("Experiment 1 did not switch to an index scan")
        if exp2["after"]["plan_type"] != "Index Only Scan" or exp2["after"]["heap_fetches"] != 0:
            raise RuntimeError(
                "Experiment 2 did not reach Index Only Scan with Heap Fetches: 0 "
                "after VACUUM (evidence kept)"
            )
        print("\nbenchmark complete; results in %s" % results_dir, flush=True)
    finally:
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("all",))
    parser.add_argument("--reset", action="store_true",
                        help="confirm the dedicated events table may be truncated")
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--reps", type=int, default=100,
                        help="timed reps for experiments 1 and 2")
    parser.add_argument("--reps-exp3", type=int, default=20,
                        help="timed reps per variant per predicate in experiment 3")
    parser.add_argument("--range-width", type=int, default=1000,
                        help="user_id range width for the exp 2 covering query")
    parser.add_argument("--sweep", type=int, nargs="+",
                        default=(1, 2, 5, 10, 50, 100, 200, 500, 900),
                        help="bucket-threshold sweep for the exp 3 crossover")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "all":
        run_all(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
