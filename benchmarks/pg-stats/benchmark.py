#!/usr/bin/env python3
"""Reproduce a LIMIT-driven pg_stats cardinality mistake on PostgreSQL 16."""

import argparse
import csv
import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"
PG_DSN = os.environ.get(
    "PG_STATS_BENCH_DSN",
    "dbname=pg_stats_bench user=stats_bench password=stats_bench "
    "host=127.0.0.1 port=55433",
)
UUID_SQL = """
(
    substr(md5(session_number::text), 1, 8) || '-' ||
    substr(md5(session_number::text), 9, 4) || '-' ||
    substr(md5(session_number::text), 13, 4) || '-' ||
    substr(md5(session_number::text), 17, 4) || '-' ||
    substr(md5(session_number::text), 21, 12)
)::uuid
"""
POSTGRES_IMAGE = (
    "postgres:16.14@sha256:"
    "33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20"
)


def db_connect():
    connection = psycopg2.connect(PG_DSN)
    connection.autocommit = True
    return connection


def wait_for_postgres(timeout=90.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            connection = db_connect()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            connection.close()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("PostgreSQL did not become ready: %s" % last_error)


def verify_benchmark_database(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_name = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT marker
            FROM pg_stats_benchmark_identity
            WHERE marker = 'pg_stats_bench_v1'
            """
        )
        marker = cursor.fetchone()
    if database_name != "pg_stats_bench" or marker != ("pg_stats_bench_v1",):
        raise RuntimeError(
            "refusing destructive seed: DSN is not the dedicated pg_stats_bench database"
        )


def command_version(command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return "unavailable"


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def deterministic_uuid(number):
    return str(uuid.UUID(hashlib.md5(str(number).encode("ascii")).hexdigest()))


def seed(connection, row_count, events_per_session, block_size):
    if row_count % block_size:
        raise ValueError("seed rows must be divisible by block size")
    if not 0 < events_per_session < block_size:
        raise ValueError("events per session must be between 1 and block size - 1")

    session_count = row_count // block_size
    print(
        "seeding %s rows (%s sessions x %s contiguous events)"
        % (f"{row_count:,}", f"{session_count:,}", events_per_session),
        flush=True,
    )
    started = time.monotonic()
    with connection.cursor() as cursor:
        cursor.execute("SET synchronous_commit = off")
        cursor.execute("SET maintenance_work_mem = '1GB'")
        cursor.execute("TRUNCATE audit_events, admin_sessions RESTART IDENTITY")
        cursor.execute("DROP INDEX IF EXISTS idx_audit_events_session_id")
        cursor.execute(
            """
            INSERT INTO admin_sessions (id, admin_email, ip_address)
            SELECT
                {uuid_expr},
                'admin-' || session_number || '@example.test',
                ('10.0.' || ((session_number / 256) %% 256) || '.' ||
                    (session_number %% 256))::inet
            FROM generate_series(1, %s) AS session_number
            """.format(uuid_expr=UUID_SQL),
            (session_count,),
        )
        cursor.execute(
            """
            INSERT INTO audit_events (
                entity_type, entity_id, session_id, event_type, payload, created_at
            )
            SELECT
                'account',
                row_number,
                CASE
                    WHEN ((row_number - 1) %% %s) < %s THEN {uuid_expr}
                    ELSE NULL
                END,
                'updated',
                '{{}}'::jsonb,
                '2026-01-01 00:00:00+00'::timestamptz
                    + row_number * interval '1 microsecond'
            FROM generate_series(1, %s) AS row_number
            CROSS JOIN LATERAL (
                SELECT ((row_number - 1) / %s + 1)::bigint AS session_number
            ) AS session
            ORDER BY row_number
            """.format(uuid_expr=UUID_SQL),
            (block_size, events_per_session, row_count, block_size),
        )
        cursor.execute(
            "CREATE INDEX idx_audit_events_session_id ON audit_events (session_id)"
        )
        cursor.execute("RESET maintenance_work_mem")
        cursor.execute("RESET synchronous_commit")
    elapsed = time.monotonic() - started
    print("seed finished in %.1fs" % elapsed, flush=True)
    return session_count, elapsed


def validate_seed(connection, row_count, events_per_session, block_size):
    expected_sessions = row_count // block_size
    expected_non_null = expected_sessions * events_per_session
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                count(*),
                count(*) FILTER (WHERE session_id IS NULL),
                count(*) FILTER (WHERE session_id IS NOT NULL),
                count(DISTINCT session_id)
            FROM audit_events
            """
        )
        total, nulls, non_nulls, distinct_sessions = cursor.fetchone()
        cursor.execute(
            """
            SELECT min(event_count), max(event_count), count(*)
            FROM (
                SELECT session_id, count(*) AS event_count
                FROM audit_events
                WHERE session_id IS NOT NULL
                GROUP BY session_id
            ) AS sessions
            """
        )
        minimum, maximum, grouped_sessions = cursor.fetchone()
        cursor.execute(
            """
            SELECT count(*)
            FROM (
                SELECT session_id, count(*) AS event_count,
                       min(id) AS first_id, max(id) AS last_id
                FROM audit_events
                WHERE session_id IS NOT NULL
                GROUP BY session_id
            ) AS sessions
            WHERE event_count <> %s
               OR last_id - first_id + 1 <> %s
            """,
            (events_per_session, events_per_session),
        )
        non_contiguous_sessions = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT indisvalid, indisready
            FROM pg_index
            WHERE indexrelid = 'idx_audit_events_session_id'::regclass
            """
        )
        index_valid, index_ready = cursor.fetchone()

    expected = (row_count, row_count - expected_non_null, expected_non_null, expected_sessions)
    actual = (total, nulls, non_nulls, distinct_sessions)
    if actual != expected:
        raise RuntimeError("seed totals are wrong: expected %r, got %r" % (expected, actual))
    if (minimum, maximum, grouped_sessions) != (
        events_per_session,
        events_per_session,
        expected_sessions,
    ):
        raise RuntimeError("session burst validation failed")
    if non_contiguous_sessions:
        raise RuntimeError(
            "%s sessions are not physically contiguous" % non_contiguous_sessions
        )
    if not index_valid or not index_ready:
        raise RuntimeError("session_id index is not valid and ready")
    return distinct_sessions


def resolved_n_distinct(raw_value, reltuples):
    return -raw_value * reltuples if raw_value < 0 else raw_value


def read_stats(connection, target, analyze_run, analyze_runs, real_distinct):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.null_frac, s.n_distinct, c.reltuples, a.attstattarget
            FROM pg_stats AS s
            JOIN pg_class AS c ON c.relname = s.tablename
            JOIN pg_attribute AS a
              ON a.attrelid = c.oid AND a.attname = s.attname
            WHERE s.schemaname = 'public'
              AND s.tablename = 'audit_events'
              AND s.attname = 'session_id'
            """
        )
        null_frac, raw_n_distinct, reltuples, active_target = cursor.fetchone()
    estimate = resolved_n_distinct(float(raw_n_distinct), float(reltuples))
    return {
        "statistics_target": target,
        "analyze_run": analyze_run,
        "analyze_runs": analyze_runs,
        "null_frac": round(float(null_frac), 6),
        "n_distinct_raw": round(float(raw_n_distinct), 6),
        "n_distinct_estimate": round(estimate, 3),
        "real_n_distinct": real_distinct,
        "miss_ratio": round(real_distinct / estimate, 3),
        "reltuples": round(float(reltuples)),
        "active_attstattarget": active_target,
    }


def capture_stats(connection, target, analyze_runs, real_distinct):
    snapshots = []
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE audit_events ALTER COLUMN session_id SET STATISTICS %s"
            % int(target)
        )
        for run in range(1, analyze_runs + 1):
            print("statistics target %s: ANALYZE %s/%s" % (target, run, analyze_runs))
            cursor.execute("ANALYZE audit_events")
            snapshots.append(
                read_stats(connection, target, run, analyze_runs, real_distinct)
            )
        cursor.execute("ANALYZE admin_sessions")
    return snapshots


def explain_query(connection, target_session, target, results_dir):
    query = """
SELECT ae.*, s.admin_email, s.ip_address
FROM audit_events ae
JOIN admin_sessions s ON s.id = ae.session_id
WHERE ae.session_id = %s::uuid
ORDER BY ae.id ASC
LIMIT 1
""".strip()
    with connection.cursor() as cursor:
        cursor.execute(query, (target_session,))
        if cursor.fetchone() is None:
            raise RuntimeError("target session has no matching audit event")
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + query, (target_session,))
        plan = "\n".join(row[0] for row in cursor.fetchall()) + "\n"
        cursor.execute("SELECT count(*) FROM audit_events WHERE session_id = %s", (target_session,))
        actual_matching_rows = cursor.fetchone()[0]

    plan_path = results_dir / ("explain_target_%s.txt" % target)
    plan_path.write_text(plan, encoding="utf-8")
    scan_match = re.search(
        r"(?:Index Scan|Bitmap Heap Scan).*?on audit_events ae.*?rows=(\d+)", plan
    )
    if not scan_match:
        raise RuntimeError("could not locate the audit_events scan in target %s plan" % target)
    execution_match = re.search(r"Execution Time: ([0-9.]+) ms", plan)
    removed_match = re.search(r"Rows Removed by Filter: ([0-9]+)", plan)
    buffers_match = re.search(r"Buffers: shared ([^\n]+)", plan)
    buffer_details = buffers_match.group(1) if buffers_match else ""
    hit_match = re.search(r"hit=(\d+)", buffer_details)
    read_match = re.search(r"read=(\d+)", buffer_details)
    if "audit_events_pkey" in plan:
        plan_name = "primary_key_scan"
    elif "idx_audit_events_session_id" in plan:
        plan_name = "session_id_index"
    else:
        plan_name = "other"
    return {
        "statistics_target": target,
        "warmup_runs": 1,
        "target_session": target_session,
        "plan": plan_name,
        "planner_estimated_rows": int(scan_match.group(1)),
        "actual_matching_rows": actual_matching_rows,
        "rows_removed_by_filter": int(removed_match.group(1)) if removed_match else 0,
        "shared_hits": int(hit_match.group(1)) if hit_match else 0,
        "shared_reads": int(read_match.group(1)) if read_match else 0,
        "execution_time_ms": float(execution_match.group(1)),
        "plan_file": plan_path.name,
    }


def write_metadata(results_dir, args, connection, real_distinct, seed_seconds):
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        server_version = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT name, setting
            FROM pg_settings
            WHERE name IN (
                'random_page_cost', 'seq_page_cost', 'cpu_tuple_cost',
                'effective_cache_size', 'shared_buffers', 'jit',
                'max_parallel_workers_per_gather'
            )
            ORDER BY name
            """
        )
        settings = cursor.fetchall()
    rows = [
        {"key": "run_at_utc", "value": datetime.now(timezone.utc).isoformat()},
        {"key": "platform", "value": platform.platform()},
        {"key": "python", "value": platform.python_version()},
        {"key": "docker", "value": command_version(["docker", "--version"])},
        {"key": "docker_compose", "value": command_version(["docker", "compose", "version"])},
        {"key": "postgres_image", "value": POSTGRES_IMAGE},
        {"key": "postgres_server", "value": server_version},
        {"key": "seed_rows", "value": str(args.rows)},
        {"key": "block_size", "value": str(args.block_size)},
        {"key": "events_per_session", "value": str(args.events_per_session)},
        {"key": "real_n_distinct", "value": str(real_distinct)},
        {"key": "target_session_number", "value": str(args.target_session)},
        {"key": "statistics_targets", "value": ";".join(map(str, args.statistics_targets))},
        {"key": "query_warmup_runs", "value": "1"},
        {"key": "seed_seconds", "value": "%.3f" % seed_seconds},
    ]
    rows.extend({"key": "pg_" + name, "value": value} for name, value in settings)
    write_csv(results_dir / "run_metadata.csv", ["key", "value"], rows)


def validate_args(args):
    allowed_results = DEFAULT_RESULTS.resolve()
    requested_results = args.results.resolve()
    if requested_results != allowed_results and allowed_results not in requested_results.parents:
        raise RuntimeError("--results must stay under benchmarks/pg-stats/results")
    if not args.reset:
        raise RuntimeError("all truncates benchmark tables; rerun with --reset")
    if args.rows <= 0 or args.block_size <= 1:
        raise ValueError("--rows must be positive and --block-size must be greater than 1")
    if args.rows % args.block_size:
        raise ValueError("--rows must be divisible by --block-size")
    if not 0 < args.events_per_session < args.block_size:
        raise ValueError("--events-per-session must be between 1 and block-size - 1")
    session_count = args.rows // args.block_size
    if not 1 <= args.target_session <= session_count:
        raise ValueError("--target-session must name one of the generated sessions")
    if not args.statistics_targets or len(set(args.statistics_targets)) != len(
        args.statistics_targets
    ):
        raise ValueError("--statistics-targets must be a non-empty unique list")
    if any(target < 1 or target > 10000 for target in args.statistics_targets):
        raise ValueError("statistics targets must be between 1 and 10000")


def run_all(args):
    validate_args(args)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    wait_for_postgres()
    connection = db_connect()
    try:
        verify_benchmark_database(connection)
        _, seed_seconds = seed(
            connection, args.rows, args.events_per_session, args.block_size
        )
        real_distinct = validate_seed(
            connection, args.rows, args.events_per_session, args.block_size
        )
        target_session = deterministic_uuid(args.target_session)
        stats_rows = []
        query_rows = []
        with tempfile.TemporaryDirectory(
            prefix=".pg-stats-run-", dir=str(args.results.parent)
        ) as staging_name:
            staging_results = Path(staging_name)
            for target in args.statistics_targets:
                analyze_runs = 2 if target == 5000 else 1
                stats_rows.extend(
                    capture_stats(connection, target, analyze_runs, real_distinct)
                )
                query_rows.append(
                    explain_query(connection, target_session, target, staging_results)
                )
            write_csv(
                staging_results / "statistics.csv",
                list(stats_rows[0].keys()),
                stats_rows,
            )
            write_csv(
                staging_results / "query_results.csv",
                list(query_rows[0].keys()),
                query_rows,
            )
            write_metadata(
                staging_results, args, connection, real_distinct, seed_seconds
            )
            args.results.mkdir(parents=True, exist_ok=True)
            for source in staging_results.iterdir():
                os.replace(str(source), str(args.results / source.name))

        query_by_target = {row["statistics_target"]: row for row in query_rows}
        first = query_by_target.get(100)
        last = query_by_target.get(5000)
        if first and (
            first["plan"] != "primary_key_scan"
            or first["rows_removed_by_filter"] < 100000
        ):
            raise RuntimeError(
                "the target-100 LIMIT trap did not reproduce; captured evidence was kept"
            )
        if last and last["plan"] != "session_id_index":
            raise RuntimeError(
                "the final statistics target did not flip to the session_id index; "
                "captured evidence was kept"
            )
        print("benchmark complete; results written to %s" % args.results)
    finally:
        connection.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("all",))
    parser.add_argument(
        "--reset",
        action="store_true",
        help="confirm that the dedicated benchmark tables may be truncated",
    )
    parser.add_argument("--rows", type=int, default=20_000_000)
    parser.add_argument("--block-size", type=int, default=1000)
    parser.add_argument("--events-per-session", type=int, default=180)
    parser.add_argument("--target-session", type=int, default=18_000)
    parser.add_argument(
        "--statistics-targets", type=int, nargs="+", default=(100, 2000, 5000)
    )
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "all":
        run_all(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
