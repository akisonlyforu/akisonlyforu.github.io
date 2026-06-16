"""Reproduce the O(N^2) collapse of deep LIMIT/OFFSET pagination in Postgres.

An export service streams a whole table out one page at a time (think: CSV export
of every issue in a project). The naive implementation pages with
`ORDER BY id LIMIT n OFFSET k`. As the offset grows deep, each page re-walks and
throws away all k preceding rows -> O(N^2) total work, per-page latency climbs
linearly with the offset, throughput dies at the tail.

Two fixes:
  * KEYSET (seek) pagination: `WHERE id > :last_id ORDER BY id LIMIT n`. The index
    seeks straight to the boundary, so every page reads exactly n rows. Flat.
  * SERVER-SIDE CURSOR: `DECLARE ... CURSOR` + repeated `FETCH n`. One plan, one
    scan, streamed in batches. Flat and single-pass.

Experiments (one `issues` table, id BIGINT identity PK, ~200 bytes/row):
  1. exp1_offset_pages.csv  - OFFSET export, per-page latency vs offset.
  2. exp2_keyset_pages.csv  - keyset export, per-page latency vs last_id.
  3. exp3_cursor.csv        - server-side named cursor, per-FETCH latency.
  4. exp4_explain.csv       - EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) for OFFSET at a
                              shallow (0) vs deep (990000) offset, and keyset at the
                              equivalent deep boundary. The mechanism evidence:
                              deep OFFSET scans ~offset+n rows and discards offset.

Env: PGHOST(127.0.0.1) PGPORT(55433) PGPASSWORD(exportbench) PGUSER(postgres)
     TOTAL_ROWS(1000000) PAGE_SIZE(1000) REPEATS(3) SEED(1234) RESULTS_DIR(results/)
"""
import csv
import json
import os
import random
import statistics
import string
import time

import psycopg2
import psycopg2.extras

HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "55433"))
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "exportbench")
DBNAME = os.environ.get("PGDATABASE", "postgres")
TOTAL_ROWS = int(os.environ.get("TOTAL_ROWS", "1000000"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1000"))
REPEATS = int(os.environ.get("REPEATS", "3"))
SEED = int(os.environ.get("SEED", "1234"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
DEEP_OFFSET = int(os.environ.get("DEEP_OFFSET", "990000"))

# Columns streamed on every page. Same list + order for OFFSET, keyset, cursor.
COLS = "id, project_id, status, title, body, created_at"


def connect():
    return psycopg2.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                            dbname=DBNAME)


SCHEMA = """
DROP TABLE IF EXISTS issues;
CREATE TABLE issues (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id integer NOT NULL,
    status     text NOT NULL,
    title      text NOT NULL,
    body       text NOT NULL,
    created_at timestamptz NOT NULL
);
"""

STATUSES = ["open", "closed", "in_progress", "blocked", "wontfix"]


def build_dataset(conn, total, seed):
    """Load `total` issue rows via COPY. id is generated identity 1..total, so the
    row at ORDER BY id OFFSET k is exactly id=k+1 (no gaps) -- lets us line up the
    OFFSET and keyset EXPLAINs on the identical physical rows."""
    rng = random.Random(seed)
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()

    # ~200 bytes of text per row: title (~40) + body (~150) + status + ids.
    alphabet = string.ascii_lowercase + " "
    title_pool = ["".join(rng.choice(alphabet) for _ in range(40)) for _ in range(256)]
    body_pool = ["".join(rng.choice(alphabet) for _ in range(150)) for _ in range(256)]

    import io
    BATCH = 50000
    written = 0
    with conn.cursor() as cur:
        while written < total:
            n = min(BATCH, total - written)
            buf = io.StringIO()
            for _ in range(n):
                project_id = rng.randint(1, 5000)
                status = STATUSES[rng.randint(0, len(STATUSES) - 1)]
                title = title_pool[rng.randint(0, 255)]
                body = body_pool[rng.randint(0, 255)]
                sec = written % 60
                ts = f"2026-01-01 00:00:{sec:02d}+00"
                buf.write(f"{project_id}\t{status}\t{title}\t{body}\t{ts}\n")
                written += 1
            buf.seek(0)
            cur.copy_expert(
                "COPY issues (project_id, status, title, body, created_at) "
                "FROM STDIN WITH (FORMAT text)", buf)
        conn.commit()
        cur.execute("ANALYZE issues")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(id), max(id) FROM issues")
        cnt, lo, hi = cur.fetchone()
        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('issues')), "
                    "pg_total_relation_size('issues')")
        size_pretty, size_bytes = cur.fetchone()
    return cnt, lo, hi, size_pretty, size_bytes


# ---- export strategies ---------------------------------------------------

def export_offset(conn, page_size, total, record_pages):
    """Naive OFFSET export. Returns (wall_ms, per_page[list]). per_page rows are
    (page_index, offset, page_latency_ms, rows) -- only populated if record_pages."""
    per_page = []
    t_all = time.perf_counter()
    offset = 0
    page_index = 0
    rows_seen = 0
    with conn.cursor() as cur:
        while True:
            t0 = time.perf_counter()
            cur.execute(
                f"SELECT {COLS} FROM issues ORDER BY id LIMIT %s OFFSET %s",
                (page_size, offset))
            rows = cur.fetchall()
            dt = (time.perf_counter() - t0) * 1000.0
            if not rows:
                break
            rows_seen += len(rows)
            if record_pages:
                per_page.append((page_index, offset, round(dt, 4), len(rows)))
            offset += page_size
            page_index += 1
            if len(rows) < page_size:
                break
    wall = (time.perf_counter() - t_all) * 1000.0
    return wall, per_page, rows_seen


def export_keyset(conn, page_size, record_pages):
    """Keyset (seek) export. Returns (wall_ms, per_page, rows_seen). per_page rows
    are (page_index, last_id, page_latency_ms, rows)."""
    per_page = []
    t_all = time.perf_counter()
    last_id = 0
    page_index = 0
    rows_seen = 0
    with conn.cursor() as cur:
        while True:
            t0 = time.perf_counter()
            cur.execute(
                f"SELECT {COLS} FROM issues WHERE id > %s ORDER BY id LIMIT %s",
                (last_id, page_size))
            rows = cur.fetchall()
            dt = (time.perf_counter() - t0) * 1000.0
            if not rows:
                break
            rows_seen += len(rows)
            if record_pages:
                per_page.append((page_index, last_id, round(dt, 4), len(rows)))
            last_id = rows[-1][0]
            page_index += 1
            if len(rows) < page_size:
                break
    wall = (time.perf_counter() - t_all) * 1000.0
    return wall, per_page, rows_seen


def export_cursor(conn, page_size, record_batches):
    """Server-side named cursor: one implicit DECLARE, repeated FETCH page_size.
    Returns (wall_ms, per_batch, rows_seen). per_batch rows are
    (batch_index, batch_latency_ms, rows)."""
    per_batch = []
    t_all = time.perf_counter()
    rows_seen = 0
    batch_index = 0
    # named cursor -> psycopg2 issues a server-side DECLARE; fetchmany -> FETCH.
    cur = conn.cursor(name="export_cur")
    cur.itersize = page_size
    cur.execute(f"SELECT {COLS} FROM issues ORDER BY id")
    while True:
        t0 = time.perf_counter()
        rows = cur.fetchmany(page_size)
        dt = (time.perf_counter() - t0) * 1000.0
        if not rows:
            break
        rows_seen += len(rows)
        if record_batches:
            per_batch.append((batch_index, round(dt, 4), len(rows)))
        batch_index += 1
    cur.close()
    conn.rollback()  # close the implicit transaction the named cursor opened
    wall = (time.perf_counter() - t_all) * 1000.0
    return wall, per_batch, rows_seen


# ---- EXPLAIN mechanism evidence -----------------------------------------

def _walk_plan(node, acc):
    """Sum shared buffers over the whole plan tree; track the scan node with the
    most Actual Rows (that's the node doing the physical work)."""
    acc["shared_hit"] += node.get("Shared Hit Blocks", 0)
    acc["shared_read"] += node.get("Shared Read Blocks", 0)
    ntype = node.get("Node Type", "")
    arows = node.get("Actual Rows", 0)
    removed = node.get("Rows Removed by Filter", 0)
    if ("Scan" in ntype) and arows >= acc["scan_rows"]:
        acc["scan_rows"] = arows
        acc["scan_node"] = ntype
        acc["rows_removed_by_filter"] = removed
    for child in node.get("Plans", []):
        _walk_plan(child, acc)


def explain_json(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]
    root = plan["Plan"]
    acc = {"shared_hit": 0, "shared_read": 0, "scan_rows": 0,
           "scan_node": "", "rows_removed_by_filter": 0}
    _walk_plan(root, acc)
    # For LIMIT/OFFSET the top Limit node emits the returned page; the scan node
    # underneath reports how many rows were actually walked. discarded = walked - emitted.
    emitted = root.get("Actual Rows", 0)
    discarded = max(0, acc["scan_rows"] - emitted)
    return {
        "actual_total_time_ms": plan["Plan"]["Actual Total Time"],
        "scan_node": acc["scan_node"],
        "rows_scanned": acc["scan_rows"],
        "rows_emitted": emitted,
        "rows_discarded": discarded,
        "rows_removed_by_filter": acc["rows_removed_by_filter"],
        "shared_hit_blocks": acc["shared_hit"],
        "shared_read_blocks": acc["shared_read"],
    }


# ---- driver --------------------------------------------------------------

def pct(vals, p):
    s = sorted(vals)
    if not s:
        return 0.0
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


def median_wall(fn, repeats):
    """Run an export `repeats` times, return (median_wall_ms, all_walls, run_used_idx).
    Per-page detail is captured only on the run whose wall time is the median."""
    walls = []
    details = []
    for _ in range(repeats):
        wall, per, rows = fn(record=True)
        walls.append(wall)
        details.append((per, rows))
    med = statistics.median(walls)
    # index of the run closest to the median wall
    used = min(range(len(walls)), key=lambda i: abs(walls[i] - med))
    return med, walls, used, details[used]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    conn = connect()
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        pg_version = cur.fetchone()[0].split(",")[0]

    print("=" * 70)
    print(f"loading {TOTAL_ROWS} rows into issues ...")
    t0 = time.perf_counter()
    cnt, lo, hi, size_pretty, size_bytes = build_dataset(conn, TOTAL_ROWS, SEED)
    load_s = time.perf_counter() - t0
    print(f"  loaded {cnt} rows (id {lo}..{hi}), table size {size_pretty}, "
          f"{load_s:.1f}s")

    # Warm the cache with one full keyset pass so timings aren't dominated by
    # cold reads from disk. Note this in metadata.
    print("warming cache (one full keyset pass) ...")
    export_keyset(conn, PAGE_SIZE, record_pages=False)

    # ---- exp1 OFFSET ----
    print("\nexp1: OFFSET export ...")
    med_off, walls_off, used_off, (off_pages, off_rows) = median_wall(
        lambda record: export_offset(conn, PAGE_SIZE, TOTAL_ROWS, record), REPEATS)
    print(f"  walls(ms)={[round(w,1) for w in walls_off]}  median={med_off/1000:.2f}s  "
          f"rows={off_rows}  run_used=#{used_off}")
    with open(os.path.join(RESULTS, "exp1_offset_pages.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["page_index", "offset", "page_latency_ms", "rows"])
        w.writerows(off_pages)
    off_lat = [r[2] for r in off_pages]
    off_deep_lat = [r[2] for r in off_pages if r[1] >= DEEP_OFFSET]

    # ---- exp2 KEYSET ----
    print("exp2: keyset export ...")
    med_key, walls_key, used_key, (key_pages, key_rows) = median_wall(
        lambda record: export_keyset(conn, PAGE_SIZE, record), REPEATS)
    print(f"  walls(ms)={[round(w,1) for w in walls_key]}  median={med_key/1000:.2f}s  "
          f"rows={key_rows}  run_used=#{used_key}")
    with open(os.path.join(RESULTS, "exp2_keyset_pages.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["page_index", "last_id", "page_latency_ms", "rows"])
        w.writerows(key_pages)
    key_lat = [r[2] for r in key_pages]
    key_deep_lat = [r[2] for r in key_pages if r[1] >= DEEP_OFFSET]

    # ---- exp3 CURSOR ----
    print("exp3: server-side cursor export ...")
    med_cur, walls_cur, used_cur, (cur_batches, cur_rows) = median_wall(
        lambda record: export_cursor(conn, PAGE_SIZE, record), REPEATS)
    print(f"  walls(ms)={[round(w,1) for w in walls_cur]}  median={med_cur/1000:.2f}s  "
          f"rows={cur_rows}  run_used=#{used_cur}")
    with open(os.path.join(RESULTS, "exp3_cursor.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["batch_index", "batch_latency_ms", "rows"])
        w.writerows(cur_batches)
    cur_lat = [r[1] for r in cur_batches]

    # ---- exp4 EXPLAIN ----
    print("exp4: EXPLAIN (ANALYZE, BUFFERS) ...")
    off_sql = f"SELECT {COLS} FROM issues ORDER BY id LIMIT %s OFFSET %s"
    key_sql = f"SELECT {COLS} FROM issues WHERE id > %s ORDER BY id LIMIT %s"
    ex_rows = []
    e_off0 = explain_json(conn, off_sql, (PAGE_SIZE, 0))
    e_offdeep = explain_json(conn, off_sql, (PAGE_SIZE, DEEP_OFFSET))
    # keyset equivalent deep boundary: id > DEEP_OFFSET returns the same physical
    # rows as OFFSET DEEP_OFFSET (ids are gapless identity 1..N).
    e_keydeep = explain_json(conn, key_sql, (lo - 1 + DEEP_OFFSET, PAGE_SIZE))
    labels = [
        ("offset", 0, e_off0),
        ("offset", DEEP_OFFSET, e_offdeep),
        ("keyset", DEEP_OFFSET, e_keydeep),
    ]
    with open(os.path.join(RESULTS, "exp4_explain.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "position", "scan_node", "rows_scanned", "rows_emitted",
                    "rows_discarded", "rows_removed_by_filter", "actual_total_time_ms",
                    "shared_hit_blocks", "shared_read_blocks"])
        for q, pos, e in labels:
            w.writerow([q, pos, e["scan_node"], e["rows_scanned"], e["rows_emitted"],
                        e["rows_discarded"], e["rows_removed_by_filter"],
                        round(e["actual_total_time_ms"], 4),
                        e["shared_hit_blocks"], e["shared_read_blocks"]])
            ex_rows.append((q, pos, e))

    conn.close()

    # ---- headline numbers ----
    digest = os.environ.get("IMAGE_DIGEST",
                            "sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20")
    off_p99 = pct(off_lat, 99)
    off_max = max(off_lat) if off_lat else 0
    off_deep_max = max(off_deep_lat) if off_deep_lat else 0
    key_p99 = pct(key_lat, 99)
    key_max = max(key_lat) if key_lat else 0
    key_deep_max = max(key_deep_lat) if key_deep_lat else 0

    lines = []
    lines.append(pg_version)
    lines.append(f"params: total_rows={TOTAL_ROWS} page_size={PAGE_SIZE} repeats={REPEATS} "
                 f"seed={SEED} deep_offset={DEEP_OFFSET}")
    lines.append(f"table: {cnt} rows, ids {lo}..{hi}, size {size_pretty}, load {load_s:.1f}s")
    lines.append("cache warmed with one full keyset pass before timing")
    lines.append("")
    lines.append("TOTAL EXPORT WALL-CLOCK (median of repeats)")
    lines.append(f"  OFFSET  : median={med_off/1000:.2f}s  runs(s)={[round(w/1000,2) for w in walls_off]}")
    lines.append(f"  keyset  : median={med_key/1000:.2f}s  runs(s)={[round(w/1000,2) for w in walls_key]}")
    lines.append(f"  cursor  : median={med_cur/1000:.2f}s  runs(s)={[round(w/1000,2) for w in walls_cur]}")
    lines.append(f"  OFFSET/keyset wall ratio = {med_off/med_key:.1f}x")
    lines.append("")
    lines.append("PER-PAGE LATENCY (single recorded run)")
    lines.append(f"  OFFSET  : p99={off_p99:.3f}ms  max={off_max:.3f}ms  "
                 f"deep(offset>={DEEP_OFFSET}) max={off_deep_max:.3f}ms")
    lines.append(f"  keyset  : p99={key_p99:.3f}ms  max={key_max:.3f}ms  "
                 f"deep(last_id>={DEEP_OFFSET}) max={key_deep_max:.3f}ms")
    lines.append(f"  cursor  : p99={pct(cur_lat,99):.3f}ms  max={max(cur_lat):.3f}ms (per FETCH {PAGE_SIZE})")
    lines.append(f"  deep-page OFFSET/keyset max ratio = {off_deep_max/key_deep_max:.0f}x")
    lines.append("")
    lines.append("EXPLAIN (ANALYZE, BUFFERS)  -- the mechanism")
    for q, pos, e in ex_rows:
        lines.append(f"  {q:<7} pos={pos:<7} scan={e['scan_node']:<20} "
                     f"scanned={e['rows_scanned']:<8} discarded={e['rows_discarded']:<8} "
                     f"time={e['actual_total_time_ms']:.3f}ms "
                     f"buffers(hit+read)={e['shared_hit_blocks']+e['shared_read_blocks']}")
    summary = "\n".join(lines) + "\n"
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(summary)
    print("\n" + summary)

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["postgres_version", "image_digest", "total_rows", "page_size",
                    "repeats", "seed", "deep_offset", "table_size_bytes", "load_seconds",
                    "cache_warmed",
                    "offset_wall_median_s", "keyset_wall_median_s", "cursor_wall_median_s",
                    "offset_keyset_wall_ratio",
                    "offset_page_p99_ms", "offset_page_max_ms", "offset_deep_page_max_ms",
                    "keyset_page_p99_ms", "keyset_page_max_ms", "keyset_deep_page_max_ms",
                    "explain_offset0_time_ms", "explain_offset0_scanned",
                    "explain_offsetdeep_time_ms", "explain_offsetdeep_scanned",
                    "explain_offsetdeep_discarded",
                    "explain_keysetdeep_time_ms", "explain_keysetdeep_scanned"])
        w.writerow([pg_version, digest, TOTAL_ROWS, PAGE_SIZE, REPEATS, SEED, DEEP_OFFSET,
                    size_bytes, round(load_s, 2), "keyset_full_pass",
                    round(med_off/1000, 3), round(med_key/1000, 3), round(med_cur/1000, 3),
                    round(med_off/med_key, 2),
                    round(off_p99, 4), round(off_max, 4), round(off_deep_max, 4),
                    round(key_p99, 4), round(key_max, 4), round(key_deep_max, 4),
                    round(e_off0["actual_total_time_ms"], 4), e_off0["rows_scanned"],
                    round(e_offdeep["actual_total_time_ms"], 4), e_offdeep["rows_scanned"],
                    e_offdeep["rows_discarded"],
                    round(e_keydeep["actual_total_time_ms"], 4), e_keydeep["rows_scanned"]])

    print(f"  {pg_version} | artifacts in {RESULTS}")


if __name__ == "__main__":
    main()
