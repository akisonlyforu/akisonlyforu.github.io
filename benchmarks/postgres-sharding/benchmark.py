"""Reproduce the routing economics behind horizontal Postgres sharding (Figma-style).

A query engine ("DBProxy") routes a query to ONE shard when it carries the shard
key, but must FAN OUT to ALL shards (scatter-gather) when it doesn't. Logical
shards are modeled as N separate DATABASES (shard_0 .. shard_{N-1}) inside one
Postgres instance -- faithful to Figma's "many logical shards colocated on one
physical host" model. A tiny router hashes the shard key (stable md5) to pick a
shard. Connections are persistent per shard, so the fan-out cost we measure is N
query round-trips, not N TCP handshakes -- the honest routing story.

Three experiments:
  A. Routing (N=8)   - single-shard point query vs scatter-gather (no shard key).
  B. Scaling         - rebuild at N=1,2,4,8, same total rows; p99 single vs scatter.
  C. Colocation (N=8)- colocated join (same shard key -> 1 shard) vs cross-shard
                       join (comments sharded by a DIFFERENT key -> fan out).

Env: PGHOST(127.0.0.1) PGPORT(55432) PGPASSWORD(shardbench) PGUSER(postgres)
     TOTAL_ROWS(50000) ITERATIONS(200) WARMUP(10) SEED(1234) RESULTS_DIR(results/)
"""
import csv
import hashlib
import os
import random
import statistics
import time

import psycopg

HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "55432"))
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "shardbench")
TOTAL_ROWS = int(os.environ.get("TOTAL_ROWS", "50000"))
ITERATIONS = int(os.environ.get("ITERATIONS", "200"))
WARMUP = int(os.environ.get("WARMUP", "10"))
SEED = int(os.environ.get("SEED", "1234"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

OBJECTS_PER_FILE = 5
COMMENTS_PER_OBJECT = 2
NUM_USERS = 500  # created_by / author domain


def shard_for(key, n):
    """Stable router: md5 of the key, mod N. NOT Python's builtin hash."""
    return int(hashlib.md5(str(key).encode()).hexdigest(), 16) % n


def admin_conn():
    return psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           dbname="postgres", autocommit=True)


def shard_conn(i):
    return psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           dbname=f"shard_{i}", autocommit=True)


SCHEMA = """
CREATE TABLE objects (
    id bigserial PRIMARY KEY,
    file_key text NOT NULL,
    created_by int NOT NULL,
    name text NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE comments (
    id bigserial PRIMARY KEY,
    file_key text NOT NULL,
    object_id bigint NOT NULL,
    author int NOT NULL,
    body text NOT NULL
);
CREATE TABLE comments2 (
    id bigserial PRIMARY KEY,
    file_key text NOT NULL,
    object_id bigint NOT NULL,
    author int NOT NULL,
    body text NOT NULL
);
CREATE INDEX ON objects (file_key);
CREATE INDEX ON objects (created_by);
CREATE INDEX ON comments (file_key);
CREATE INDEX ON comments (object_id);
CREATE INDEX ON comments2 (author);
CREATE INDEX ON comments2 (object_id);
"""


def build_dataset(admin, n, total, seed):
    """Drop+recreate n shard databases, load `total` objects distributed by
    hash(file_key), plus colocated `comments` (same file_key) and cross-shard
    `comments2` (sharded by author). Returns (conns, catalog)."""
    rng = random.Random(seed)
    for i in range(n):
        admin.execute(f"DROP DATABASE IF EXISTS shard_{i} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE shard_{i}")
    conns = {i: shard_conn(i) for i in range(n)}
    for c in conns.values():
        c.execute(SCHEMA)

    num_files = total // OBJECTS_PER_FILE
    # buffers per shard, per table
    obj_buf = {i: [] for i in range(n)}
    com_buf = {i: [] for i in range(n)}
    com2_buf = {i: [] for i in range(n)}

    obj_gid = 0
    file_keys = []
    object_ids = []
    for f in range(num_files):
        file_key = f"file-{f}"
        oshard = shard_for(file_key, n)
        file_keys.append(file_key)
        for _ in range(OBJECTS_PER_FILE):
            obj_gid += 1
            created_by = rng.randint(1, NUM_USERS)
            obj_buf[oshard].append(
                (obj_gid, file_key, created_by, f"obj-{obj_gid}",
                 f"2026-01-01T00:00:{obj_gid % 60:02d}+00"))
            object_ids.append(obj_gid)
            for _ in range(COMMENTS_PER_OBJECT):
                # colocated comment: SAME file_key -> same shard as its object
                com_buf[oshard].append(
                    (file_key, obj_gid, rng.randint(1, NUM_USERS), "colocated body"))
                # cross-shard comment: sharded by author (a DIFFERENT key)
                author = rng.randint(1, NUM_USERS)
                com2_buf[shard_for(author, n)].append(
                    (file_key, obj_gid, author, "cross-shard body"))

    for i in range(n):
        with conns[i].cursor() as cur:
            with cur.copy("COPY objects (id,file_key,created_by,name,updated_at) FROM STDIN") as cp:
                for row in obj_buf[i]:
                    cp.write_row(row)
            with cur.copy("COPY comments (file_key,object_id,author,body) FROM STDIN") as cp:
                for row in com_buf[i]:
                    cp.write_row(row)
            with cur.copy("COPY comments2 (file_key,object_id,author,body) FROM STDIN") as cp:
                for row in com2_buf[i]:
                    cp.write_row(row)
        conns[i].execute("ANALYZE")

    catalog = {
        "file_keys": file_keys,
        "object_ids": object_ids,
        "created_by_values": list(range(1, NUM_USERS + 1)),
    }
    return conns, catalog


def teardown(admin, conns):
    for c in conns.values():
        c.close()
    for i in list(conns.keys()):
        admin.execute(f"DROP DATABASE IF EXISTS shard_{i} WITH (FORCE)")


def pct(vals, p):
    s = sorted(vals)
    if not s:
        return 0.0
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


# ---- query primitives ----------------------------------------------------

def q_single_shard(conns, n, file_key):
    """Router picks 1 shard, query runs on 1 DB. Returns (rows, shards_touched, shard_queries)."""
    i = shard_for(file_key, n)
    with conns[i].cursor() as cur:
        cur.execute(
            "SELECT id,file_key,created_by,name,updated_at FROM objects WHERE file_key=%s",
            (file_key,))
        rows = cur.fetchall()
    return rows, 1, 1


def q_scatter(conns, n, created_by):
    """No shard key -> fan out to ALL shards, merge. shards_touched = n."""
    merged = []
    for i in range(n):
        with conns[i].cursor() as cur:
            cur.execute(
                "SELECT id,file_key,created_by,name,updated_at FROM objects WHERE created_by=%s",
                (created_by,))
            merged.extend(cur.fetchall())
    return merged, n, n


def q_colocated_join(conns, n, file_key):
    """objects JOIN comments on the SAME file_key -> both live on one shard."""
    i = shard_for(file_key, n)
    with conns[i].cursor() as cur:
        cur.execute(
            "SELECT o.id, c.body FROM objects o JOIN comments c "
            "ON c.object_id=o.id AND c.file_key=o.file_key WHERE o.file_key=%s",
            (file_key,))
        rows = cur.fetchall()
    return rows, 1


def q_cross_shard_join(conns, n, file_key, object_ids_of_file):
    """comments2 is sharded by author, so an object's comments are scattered.
    To reconstruct object+comments you read the object's own shard, then fan out
    to ALL shards to gather comments2 by object_id."""
    i = shard_for(file_key, n)
    with conns[i].cursor() as cur:
        cur.execute("SELECT id,name FROM objects WHERE file_key=%s", (file_key,))
        objs = cur.fetchall()
    merged = []
    for s in range(n):
        with conns[s].cursor() as cur:
            cur.execute(
                "SELECT object_id, author, body FROM comments2 WHERE object_id = ANY(%s)",
                (object_ids_of_file,))
            merged.extend(cur.fetchall())
    return objs, merged, n


# ---- experiments ---------------------------------------------------------

def experiment_a(conns, n, catalog):
    rng = random.Random(SEED + 1)
    fk_keys = [rng.choice(catalog["file_keys"]) for _ in range(ITERATIONS + WARMUP)]
    cb_keys = [rng.choice(catalog["created_by_values"]) for _ in range(ITERATIONS + WARMUP)]

    single_lat, single_rows_total, per = [], 0, []
    for idx, fk in enumerate(fk_keys):
        t0 = time.perf_counter()
        rows, st, sq = q_single_shard(conns, n, fk)
        dt = (time.perf_counter() - t0) * 1000.0
        if idx >= WARMUP:
            single_lat.append(dt)
            single_rows_total += len(rows)
            per.append(("single", idx - WARMUP, round(dt, 4), st, sq, len(rows)))

    scatter_lat, scatter_rows_total = [], 0
    for idx, cb in enumerate(cb_keys):
        t0 = time.perf_counter()
        rows, st, sq = q_scatter(conns, n, cb)
        dt = (time.perf_counter() - t0) * 1000.0
        if idx >= WARMUP:
            scatter_lat.append(dt)
            scatter_rows_total += len(rows)
            per.append(("scatter", idx - WARMUP, round(dt, 4), st, sq, len(rows)))

    with open(os.path.join(RESULTS, "exp_a_routing.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "iter", "latency_ms", "shards_touched", "shard_queries", "rows"])
        w.writerows(per)

    res = {
        "single_p50": pct(single_lat, 50), "single_p99": pct(single_lat, 99),
        "single_mean": statistics.mean(single_lat), "single_shards": 1,
        "single_queries": len(single_lat) * 1, "single_rows": single_rows_total,
        "scatter_p50": pct(scatter_lat, 50), "scatter_p99": pct(scatter_lat, 99),
        "scatter_mean": statistics.mean(scatter_lat), "scatter_shards": n,
        "scatter_queries": len(scatter_lat) * n, "scatter_rows": scatter_rows_total,
    }
    print("=" * 66)
    print(f"EXPERIMENT A  routing at N={n} shards ({len(single_lat)} timed iters each)")
    print("=" * 66)
    print(f"  single-shard (WHERE file_key): p50={res['single_p50']:.3f}ms "
          f"p99={res['single_p99']:.3f}ms mean={res['single_mean']:.3f}ms  shards_touched=1")
    print(f"  scatter-gather (WHERE created_by): p50={res['scatter_p50']:.3f}ms "
          f"p99={res['scatter_p99']:.3f}ms mean={res['scatter_mean']:.3f}ms  shards_touched={n}")
    print(f"  physical shard-queries: single={res['single_queries']}  scatter={res['scatter_queries']}")
    print(f"  p99 ratio scatter/single = {res['scatter_p99']/res['single_p99']:.1f}x")
    return res


def experiment_b(admin):
    rows = []
    print("\n" + "=" * 66)
    print("EXPERIMENT B  scaling: same total rows redistributed at N=1,2,4,8")
    print("=" * 66)
    for n in [1, 2, 4, 8]:
        conns, catalog = build_dataset(admin, n, TOTAL_ROWS, SEED)
        rng = random.Random(SEED + 2)
        fk_keys = [rng.choice(catalog["file_keys"]) for _ in range(ITERATIONS + WARMUP)]
        cb_keys = [rng.choice(catalog["created_by_values"]) for _ in range(ITERATIONS + WARMUP)]

        single_lat = []
        for idx, fk in enumerate(fk_keys):
            t0 = time.perf_counter()
            q_single_shard(conns, n, fk)
            dt = (time.perf_counter() - t0) * 1000.0
            if idx >= WARMUP:
                single_lat.append(dt)
        scatter_lat = []
        for idx, cb in enumerate(cb_keys):
            t0 = time.perf_counter()
            q_scatter(conns, n, cb)
            dt = (time.perf_counter() - t0) * 1000.0
            if idx >= WARMUP:
                scatter_lat.append(dt)

        s99, sc99 = pct(single_lat, 99), pct(scatter_lat, 99)
        smean, scmean = statistics.mean(single_lat), statistics.mean(scatter_lat)
        rows.append((n, round(s99, 4), round(sc99, 4), round(smean, 4), round(scmean, 4)))
        print(f"  N={n}: single_p99={s99:.3f}ms scatter_p99={sc99:.3f}ms | "
              f"single_mean={smean:.3f}ms scatter_mean={scmean:.3f}ms "
              f"(scatter_mean/single_mean={scmean/smean:.1f}x)")
        teardown(admin, conns)

    with open(os.path.join(RESULTS, "exp_b_scaling.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n_shards", "single_p99_ms", "scatter_p99_ms",
                    "single_mean_ms", "scatter_mean_ms"])
        w.writerows(rows)
    return rows


def experiment_c(conns, n, catalog):
    rng = random.Random(SEED + 3)
    # map file_key -> its object ids (for cross-shard gather)
    keys = [rng.choice(catalog["file_keys"]) for _ in range(ITERATIONS + WARMUP)]
    # object ids per file are contiguous per generation, but derive them safely:
    # each file-f has OBJECTS_PER_FILE objects; gid = f*OPF+1 .. f*OPF+OPF
    def oids(fk):
        f = int(fk.split("-")[1])
        base = f * OBJECTS_PER_FILE
        return list(range(base + 1, base + OBJECTS_PER_FILE + 1))

    colo_lat = []
    for idx, fk in enumerate(keys):
        t0 = time.perf_counter()
        rows, st = q_colocated_join(conns, n, fk)
        dt = (time.perf_counter() - t0) * 1000.0
        if idx >= WARMUP:
            colo_lat.append(dt)
    cross_lat = []
    cross_shards = n
    for idx, fk in enumerate(keys):
        oid_list = oids(fk)
        t0 = time.perf_counter()
        objs, merged, st = q_cross_shard_join(conns, n, fk, oid_list)
        dt = (time.perf_counter() - t0) * 1000.0
        if idx >= WARMUP:
            cross_lat.append(dt)
            cross_shards = st

    with open(os.path.join(RESULTS, "exp_c_colocation.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["join_type", "shards_touched", "p50_ms", "p99_ms", "mean_ms"])
        w.writerow(["colocated", 1, round(pct(colo_lat, 50), 4),
                    round(pct(colo_lat, 99), 4), round(statistics.mean(colo_lat), 4)])
        w.writerow(["cross_shard", cross_shards, round(pct(cross_lat, 50), 4),
                    round(pct(cross_lat, 99), 4), round(statistics.mean(cross_lat), 4)])

    res = {
        "colo_p50": pct(colo_lat, 50), "colo_p99": pct(colo_lat, 99),
        "colo_shards": 1,
        "cross_p50": pct(cross_lat, 50), "cross_p99": pct(cross_lat, 99),
        "cross_shards": cross_shards,
    }
    print("\n" + "=" * 66)
    print(f"EXPERIMENT C  colocation at N={n} shards")
    print("=" * 66)
    print(f"  colocated join (same file_key -> 1 shard): "
          f"p50={res['colo_p50']:.3f}ms p99={res['colo_p99']:.3f}ms  shards_touched=1")
    print(f"  cross-shard join (comments2 by author -> fan out): "
          f"p50={res['cross_p50']:.3f}ms p99={res['cross_p99']:.3f}ms  shards_touched={cross_shards}")
    print(f"  p99 ratio cross/colocated = {res['cross_p99']/res['colo_p99']:.1f}x")
    return res


def main():
    os.makedirs(RESULTS, exist_ok=True)
    admin = admin_conn()
    pg_version = admin.execute("SELECT version()").fetchone()[0].split(",")[0]

    b_rows = experiment_b(admin)

    N = 8
    conns, catalog = build_dataset(admin, N, TOTAL_ROWS, SEED)
    a = experiment_a(conns, N, catalog)
    c = experiment_c(conns, N, catalog)
    teardown(admin, conns)
    admin.close()

    digest = os.environ.get("IMAGE_DIGEST",
                            "sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20")
    # summary.txt
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(f"{pg_version}\n")
        f.write(f"params: n_shards(A,C)=8  scaling N=1,2,4,8  total_rows={TOTAL_ROWS}  "
                f"iterations={ITERATIONS}  warmup={WARMUP}  seed={SEED}\n\n")
        f.write("EXPERIMENT A  routing (N=8)\n")
        f.write(f"  single-shard : p50={a['single_p50']:.3f} p99={a['single_p99']:.3f} "
                f"mean={a['single_mean']:.3f} ms | shards_touched=1 | "
                f"shard_queries={a['single_queries']} | rows={a['single_rows']}\n")
        f.write(f"  scatter      : p50={a['scatter_p50']:.3f} p99={a['scatter_p99']:.3f} "
                f"mean={a['scatter_mean']:.3f} ms | shards_touched=8 | "
                f"shard_queries={a['scatter_queries']} | rows={a['scatter_rows']}\n")
        f.write(f"  p99 scatter/single = {a['scatter_p99']/a['single_p99']:.1f}x\n\n")
        f.write("EXPERIMENT B  scaling (ms)\n")
        f.write("  n_shards  single_p99  scatter_p99  single_mean  scatter_mean  mean_ratio\n")
        for n, s99, sc99, smean, scmean in b_rows:
            f.write(f"  {n:<8}  {s99:<10.3f}  {sc99:<11.3f}  {smean:<11.3f}  "
                    f"{scmean:<12.3f}  {scmean/smean:.1f}x\n")
        f.write("\nEXPERIMENT C  colocation (N=8)\n")
        f.write(f"  colocated join   : p50={c['colo_p50']:.3f} p99={c['colo_p99']:.3f} ms | shards_touched=1\n")
        f.write(f"  cross-shard join : p50={c['cross_p50']:.3f} p99={c['cross_p99']:.3f} ms | "
                f"shards_touched={c['cross_shards']}\n")
        f.write(f"  p99 cross/colocated = {c['cross_p99']/c['colo_p99']:.1f}x\n")

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["postgres_version", "image_digest", "n_shards_AC", "scaling_N",
                    "total_rows", "iterations", "warmup", "seed",
                    "A_single_p99_ms", "A_scatter_p99_ms", "A_p99_ratio",
                    "C_colo_p99_ms", "C_cross_p99_ms", "C_p99_ratio"])
        w.writerow([pg_version, digest, 8, "1;2;4;8", TOTAL_ROWS, ITERATIONS, WARMUP, SEED,
                    round(a["single_p99"], 4), round(a["scatter_p99"], 4),
                    round(a["scatter_p99"] / a["single_p99"], 2),
                    round(c["colo_p99"], 4), round(c["cross_p99"], 4),
                    round(c["cross_p99"] / c["colo_p99"], 2)])

    print(f"\n  {pg_version} | artifacts in {RESULTS}")


if __name__ == "__main__":
    main()
