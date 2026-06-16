"""Reproduce the resharding cost of hash-modulo sharding vs. a fixed pool of
LOGICAL shards mapped to physical machines through a lookup table (the "Notion
model").

The problem: place a workspace with `physical = hash(workspace_id) % P`. Adding a
machine (P -> P+1) rehashes almost every key, so almost every row must physically
move.

The fix: pin a large, fixed count of LOGICAL shards, `logical = hash(workspace_id)
% L` (L=480, never changes), and route with a lookup table `physical =
lookup[logical]`. Rescaling only re-points whole logical shards at new machines, so
only a known subset of rows moves and no key ever changes its logical shard.

A physical shard is modeled as a Postgres SCHEMA (shard_p0 .. shard_p{P-1}) inside
ONE digest-pinned Postgres 16 instance. Moving a logical shard = moving its rows
between schemas with `INSERT INTO ... SELECT` + `DELETE` in one transaction, and we
count the rows actually moved with SQL. That keeps it an honest, real Postgres data
move without needing N containers.

Hash: blake2b(str(workspace_id), digest_size=8) -> int. Used identically for
`% P` (physical modulo) and `% L` (logical). NOT Python's builtin hash().

Experiments:
  A. Modulo resharding tax  - for P_old->P_new in {4->5,4->6,4->8,8->12}, count the
     rows whose (hash%P_old) != (hash%P_new): they must physically move.
  B. Logical shards + lookup - actually LOAD rows into P physical schemas by the
     lookup table, then rebalance (4->6 and 8->12) by re-pointing the minimum set of
     logical shards, moving their rows between schemas and counting them in SQL.
     Key->logical churn is 0% by construction; we verify row count + id checksum are
     identical before/after.
  C. Why 480 is highly composite - distribute L logical shards across many P and
     measure per-physical-machine row-load imbalance. Compare L=480 (highly
     composite) vs 479 (prime) vs 500. A subset is measured by real per-schema row
     counts; the rest is shard-count arithmetic over the same real per-shard rows.

Env: PGHOST(127.0.0.1) PGPORT(55442) PGPASSWORD(shardbench) PGUSER(postgres)
     N_WORKSPACES(200000) MAX_ROWS_PER_WS(20) L(480) SEED(1234) RESULTS_DIR(results/)
"""
import csv
import hashlib
import os
import random

import psycopg

HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "55442"))
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "shardbench")
N_WORKSPACES = int(os.environ.get("N_WORKSPACES", "200000"))
MAX_ROWS_PER_WS = int(os.environ.get("MAX_ROWS_PER_WS", "20"))
L = int(os.environ.get("L", "480"))
SEED = int(os.environ.get("SEED", "1234"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

A_TRANSITIONS = [(4, 5), (4, 6), (4, 8), (8, 12)]
B_TRANSITIONS = [(4, 6), (8, 12)]
C_PHYSICALS = [3, 4, 5, 6, 8, 10, 12, 15, 16, 24, 32]
C_LOGICAL_COUNTS = [480, 479, 500]
C_MEASURED = [(480, 6), (479, 6), (480, 16), (479, 16)]  # actually load rows + count


def h(workspace_id):
    """Stable hash of a workspace id -> int (blake2b, 8 bytes). Not builtin hash()."""
    return int(hashlib.blake2b(str(workspace_id).encode(), digest_size=8).hexdigest(), 16)


def admin_conn():
    return psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           dbname="postgres", autocommit=True)


# ---- dataset -------------------------------------------------------------

def build_catalog():
    """~N_WORKSPACES workspaces, each with a skewed 1..MAX_ROWS_PER_WS row count.
    Returns (catalog, rows).
      catalog[i] = (workspace_id, hash, n_rows, logical)
      rows       = list of (row_id, workspace_id, logical) -- the real rows to load.
    Skew: cubic-ish toward 1 row so counts are lumpy like real workspaces."""
    rng = random.Random(SEED)
    catalog = []
    rows = []
    row_id = 0
    for wid in range(1, N_WORKSPACES + 1):
        hv = h(wid)
        # skewed toward small counts (r**2.5), deterministic
        n_rows = 1 + int((MAX_ROWS_PER_WS - 1) * (rng.random() ** 2.5))
        logical = hv % L
        catalog.append((wid, hv, n_rows, logical))
        for _ in range(n_rows):
            row_id += 1
            rows.append((row_id, wid, logical))
    return catalog, rows


ITEMS_DDL = (
    "CREATE TABLE {schema}.items ("
    " row_id bigint PRIMARY KEY,"
    " workspace_id bigint NOT NULL,"
    " logical int NOT NULL)"
)


def recreate_schemas(admin, p_total):
    for m in range(p_total):
        admin.execute(f"DROP SCHEMA IF EXISTS shard_p{m} CASCADE")
        admin.execute(f"CREATE SCHEMA shard_p{m}")
        admin.execute(ITEMS_DDL.format(schema=f"shard_p{m}"))


def drop_schemas(admin, p_total):
    for m in range(p_total):
        admin.execute(f"DROP SCHEMA IF EXISTS shard_p{m} CASCADE")


def load_rows(admin, rows, place, p_total, index_logical=False):
    """COPY every row into schema shard_p{place(logical)}.items. `place` maps a
    logical shard id -> physical machine. Returns per-machine row counts (measured
    by SQL afterwards)."""
    buffers = {m: [] for m in range(p_total)}
    for (rid, wid, lg) in rows:
        buffers[place(lg)].append((rid, wid, lg))
    for m in range(p_total):
        with admin.cursor() as cur:
            with cur.copy(
                f"COPY shard_p{m}.items (row_id,workspace_id,logical) FROM STDIN"
            ) as cp:
                for r in buffers[m]:
                    cp.write_row(r)
        if index_logical:
            admin.execute(f"CREATE INDEX ON shard_p{m}.items (logical)")
        admin.execute(f"ANALYZE shard_p{m}.items")
    return per_schema_counts(admin, p_total)


def per_schema_counts(admin, p_total):
    counts = {}
    for m in range(p_total):
        counts[m] = admin.execute(
            f"SELECT count(*) FROM shard_p{m}.items").fetchone()[0]
    return counts


def checksum(admin, p_total):
    """(total_rows, sum_of_row_ids) across every physical schema -- an id checksum
    that catches lost/duplicated rows after a move."""
    total, s = 0, 0
    for m in range(p_total):
        c, ss = admin.execute(
            f"SELECT count(*), coalesce(sum(row_id),0) FROM shard_p{m}.items"
        ).fetchone()
        total += c
        s += int(ss)
    return total, s


# ---- lookup table / rebalance -------------------------------------------

def initial_lookup(p_total):
    """Contiguous: machine m owns L/p_total logical shards. Requires L % p_total==0
    (true for 4 and 8 with L=480)."""
    chunk = L // p_total
    return {lg: lg // chunk for lg in range(L)}


def rebalance(lookup, p_old, p_new):
    """Re-point the MINIMUM set of logical shards so every machine (old + new) ends
    with L/p_new shards. Old machines keep `target` and shed the surplus; new
    machines are filled from that surplus. Returns (new_lookup, moves) where moves
    is a list of (logical, src_machine, dst_machine)."""
    target = L // p_new
    owned = {m: [] for m in range(p_old)}
    for lg in range(L):
        owned[lookup[lg]].append(lg)
    surplus = []
    for m in range(p_old):
        surplus.extend(owned[m][target:])  # keep first `target`, shed the rest
    new_lookup = dict(lookup)
    moves = []
    idx = 0
    for nm in range(p_old, p_new):
        for _ in range(target):
            lg = surplus[idx]
            moves.append((lg, lookup[lg], nm))
            new_lookup[lg] = nm
            idx += 1
    assert idx == len(surplus), (idx, len(surplus))
    return new_lookup, moves


# ---- Experiment A --------------------------------------------------------

def experiment_a(catalog):
    """Count rows (and workspaces) that must physically move when P changes under
    plain hash-modulo placement. Counted over the dataset's REAL per-workspace row
    counts."""
    total_rows = sum(c[2] for c in catalog)
    total_ws = len(catalog)
    out = []
    print("=" * 68)
    print("EXPERIMENT A  modulo resharding tax (hash % P)")
    print("=" * 68)
    for p_old, p_new in A_TRANSITIONS:
        rows_moved = 0
        ws_moved = 0
        for (wid, hv, n_rows, lg) in catalog:
            if (hv % p_old) != (hv % p_new):
                rows_moved += n_rows
                ws_moved += 1
        pr = 100.0 * rows_moved / total_rows
        pw = 100.0 * ws_moved / total_ws
        out.append((f"{p_old}->{p_new}", p_old, p_new, rows_moved, total_rows,
                    round(pr, 3), ws_moved, total_ws, round(pw, 3)))
        print(f"  {p_old}->{p_new}: rows_moved={rows_moved:>9} / {total_rows} "
              f"({pr:5.1f}%)  workspaces_moved={ws_moved:>7} / {total_ws} ({pw:5.1f}%)")
    with open(os.path.join(RESULTS, "exp_a_modulo.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["transition", "p_old", "p_new", "rows_moved", "total_rows",
                    "pct_rows_moved", "workspaces_moved", "total_workspaces",
                    "pct_workspaces_moved"])
        w.writerows(out)
    return out, total_rows


# ---- Experiment B --------------------------------------------------------

def experiment_b(admin, rows):
    """For each transition, LOAD rows into p_old schemas via the lookup table, then
    rebalance to p_new by moving whole logical shards' rows between schemas in one
    txn, counting moved rows in SQL. Verify id checksum is unchanged."""
    total_rows = len(rows)
    csv_rows = []
    summary = []
    print("\n" + "=" * 68)
    print("EXPERIMENT B  logical shards + lookup table (real data movement)")
    print("=" * 68)
    for p_old, p_new in B_TRANSITIONS:
        recreate_schemas(admin, p_new)  # p_new schemas; new ones start empty
        place = lambda lg: initial_lookup(p_old)[lg]
        before_counts = load_rows(admin, rows, place, p_new, index_logical=True)
        # new machines (p_old..p_new-1) are empty for now
        cs_before = checksum(admin, p_new)

        lookup, moves = rebalance(initial_lookup(p_old), p_old, p_new)
        shards_moved = len(moves)
        # group moved logical shards by (src, dst) machine pair
        by_pair = {}
        for (lg, src, dst) in moves:
            by_pair.setdefault((src, dst), []).append(lg)

        rows_moved = 0
        with psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                             dbname="postgres", autocommit=False) as tx:
            with tx.cursor() as cur:
                for (src, dst), lgs in by_pair.items():
                    cur.execute(
                        f"INSERT INTO shard_p{dst}.items (row_id,workspace_id,logical) "
                        f"SELECT row_id,workspace_id,logical FROM shard_p{src}.items "
                        f"WHERE logical = ANY(%s)", (lgs,))
                    moved = cur.rowcount
                    cur.execute(
                        f"DELETE FROM shard_p{src}.items WHERE logical = ANY(%s)", (lgs,))
                    assert cur.rowcount == moved, (cur.rowcount, moved)
                    rows_moved += moved
            tx.commit()

        after_counts = per_schema_counts(admin, p_new)
        cs_after = checksum(admin, p_new)
        match = (cs_before == cs_after)
        pct = 100.0 * rows_moved / total_rows

        def bal(counts, p):
            vals = [counts.get(m, 0) for m in range(p)]
            nz = [v for v in vals if v > 0] or [0]
            return min(nz), max(nz), (max(nz) / min(nz) if min(nz) else 0.0)

        b_lo, b_hi, b_ratio = bal(before_counts, p_new)
        a_lo, a_hi, a_ratio = bal(after_counts, p_new)

        for m in range(p_new):
            csv_rows.append((f"{p_old}->{p_new}", m, before_counts.get(m, 0),
                             after_counts.get(m, 0)))
        csv_rows.append((f"{p_old}->{p_new}", "__summary__",
                         f"rows_moved={rows_moved}", f"pct={pct:.3f}"))

        summary.append({
            "transition": f"{p_old}->{p_new}", "shards_moved": shards_moved,
            "rows_moved": rows_moved, "pct": pct, "total": total_rows,
            "churn": 0.0, "match": match, "cs_before": cs_before, "cs_after": cs_after,
            "before_ratio": b_ratio, "after_ratio": a_ratio,
            "before_bal": (b_lo, b_hi), "after_bal": (a_lo, a_hi),
        })
        print(f"  {p_old}->{p_new}: logical_shards_moved={shards_moved}  "
              f"rows_moved={rows_moved} / {total_rows} ({pct:.1f}%)  "
              f"key->logical churn=0.0%")
        print(f"         balance before P={p_old} (rows/machine {b_lo}..{b_hi}, "
              f"max/min={b_ratio:.3f})  after P={p_new} ({a_lo}..{a_hi}, "
              f"max/min={a_ratio:.3f})")
        print(f"         checksum before={cs_before} after={cs_after}  "
              f"identical={match}")
        drop_schemas(admin, p_new)

    with open(os.path.join(RESULTS, "exp_b_logical.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["transition", "physical_machine", "rows_before", "rows_after"])
        w.writerows(csv_rows)
    return summary


# ---- Experiment C --------------------------------------------------------

def place_contig(lg, logical_count, p):
    """Distribute `logical_count` shards into p contiguous groups (sizes differ by
    at most 1)."""
    return min(p - 1, lg * p // logical_count)


def experiment_c(admin, catalog):
    """Per-physical-machine row-load imbalance when L logical shards spread across P
    machines. A subset (C_MEASURED) is loaded into real schemas and counted in SQL;
    the rest is derived arithmetic over the same real per-logical-shard row counts."""
    # real rows per logical shard, for each candidate L (recompute logical = hash % Lc)
    logical_rows = {}  # (Lc) -> dict lg->rows
    for Lc in C_LOGICAL_COUNTS:
        d = {}
        for (wid, hv, n_rows, _lg) in catalog:
            lg = hv % Lc
            d[lg] = d.get(lg, 0) + n_rows
        logical_rows[Lc] = d

    # measured loads: build a synthetic rows stream per Lc from catalog, load, count
    measured = {}  # (Lc,P) -> per-machine measured counts
    for (Lc, P) in C_MEASURED:
        # rows for this Lc: reuse catalog row counts, assign row_ids deterministically
        rows = []
        rid = 0
        for (wid, hv, n_rows, _lg) in catalog:
            lg = hv % Lc
            for _ in range(n_rows):
                rid += 1
                rows.append((rid, wid, lg))
        recreate_schemas(admin, P)
        counts = load_rows(admin, rows, lambda lg: place_contig(lg, Lc, P), P)
        measured[(Lc, P)] = counts
        drop_schemas(admin, P)

    out = []
    print("\n" + "=" * 68)
    print("EXPERIMENT C  why the logical count should be highly composite")
    print("=" * 68)
    print(f"  {'L':>4} {'P':>3} {'even?':>6} {'shards[min..max]':>18} "
          f"{'spread':>7} {'rows[min..max]':>20} {'row_ratio':>10} {'source':>10}")
    for Lc in C_LOGICAL_COUNTS:
        lr = logical_rows[Lc]
        for P in C_PHYSICALS:
            # shards per machine
            spm = [0] * P
            mrows = [0] * P
            for lg in range(Lc):
                m = place_contig(lg, Lc, P)
                spm[m] += 1
                mrows[m] += lr.get(lg, 0)
            s_min, s_max = min(spm), max(spm)
            r_min, r_max = min(mrows), max(mrows)
            evenly = (Lc % P == 0)
            source = "derived"
            if (Lc, P) in measured:
                mc = measured[(Lc, P)]
                meas_vals = [mc[m] for m in range(P)]
                assert meas_vals == mrows, (Lc, P, meas_vals, mrows)
                r_min, r_max = min(meas_vals), max(meas_vals)
                source = "measured"
            ratio = (r_max / r_min) if r_min else 0.0
            out.append((Lc, P, int(evenly), s_min, s_max, s_max - s_min,
                        r_min, r_max, round(ratio, 4), source))
            print(f"  {Lc:>4} {P:>3} {str(evenly):>6} "
                  f"{str(s_min)+'..'+str(s_max):>18} {s_max - s_min:>7} "
                  f"{str(r_min)+'..'+str(r_max):>20} {ratio:>10.4f} {source:>10}")
    with open(os.path.join(RESULTS, "exp_c_composite.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["logical_count", "physical_count", "divides_evenly",
                    "shards_per_machine_min", "shards_per_machine_max",
                    "shards_spread", "rows_min", "rows_max", "row_ratio", "source"])
        w.writerows(out)
    return out


# ---- main ----------------------------------------------------------------

def main():
    os.makedirs(RESULTS, exist_ok=True)
    admin = admin_conn()
    pg_version = admin.execute("SELECT version()").fetchone()[0].split(",")[0]

    print(f"building catalog: {N_WORKSPACES} workspaces (seed={SEED}) ...")
    catalog, rows = build_catalog()
    total_rows = len(rows)
    print(f"  generated {total_rows} rows across {N_WORKSPACES} workspaces "
          f"(avg {total_rows / N_WORKSPACES:.2f} rows/ws, L={L})")

    a_out, _ = experiment_a(catalog)
    b_out = experiment_b(admin, rows)
    c_out = experiment_c(admin, catalog)
    admin.close()

    digest = os.environ.get("IMAGE_DIGEST",
                            "sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20")

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(f"{pg_version}\n")
        f.write(f"params: n_workspaces={N_WORKSPACES}  total_rows={total_rows}  "
                f"max_rows_per_ws={MAX_ROWS_PER_WS}  L={L}  seed={SEED}\n")
        f.write(f"hash: blake2b(str(workspace_id), digest_size=8) -> int; "
                f"physical=hash%P, logical=hash%L\n\n")

        f.write("EXPERIMENT A  modulo resharding tax (hash % P)\n")
        f.write("  transition  rows_moved / total        pct     workspaces_moved   pct\n")
        for (t, po, pn, rm, tr, pr, wm, tw, pw) in a_out:
            f.write(f"  {t:<10}  {rm:>9} / {tr:<9}  {pr:>6.1f}%  {wm:>9} / {tw:<7}  {pw:>5.1f}%\n")

        f.write("\nEXPERIMENT B  logical shards + lookup table (real data movement)\n")
        for s in b_out:
            f.write(f"  {s['transition']}: logical_shards_moved={s['shards_moved']}  "
                    f"rows_moved={s['rows_moved']} / {s['total']} ({s['pct']:.1f}%)  "
                    f"key->logical churn={s['churn']:.1f}%\n")
            f.write(f"         balance after: rows/machine {s['after_bal'][0]}.."
                    f"{s['after_bal'][1]} (max/min={s['after_ratio']:.3f})\n")
            f.write(f"         checksum(count,sum_id) before={s['cs_before']} "
                    f"after={s['cs_after']} identical={s['match']}\n")

        f.write("\nEXPERIMENT C  why L should be highly composite "
                "(480=2^5*3*5 vs 479 prime vs 500)\n")
        f.write("  L    P   even?  shards_spread  row_ratio  source\n")
        for (Lc, P, ev, smn, smx, spread, rmn, rmx, ratio, src) in c_out:
            f.write(f"  {Lc:<4} {P:<3} {str(bool(ev)):<6} {spread:<13} "
                    f"{ratio:<9.4f} {src}\n")

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["postgres_version", "image_digest", "hash_function",
                    "n_workspaces", "total_rows", "max_rows_per_ws", "L", "seed",
                    "A_transitions", "B_transitions", "C_physicals", "C_logical_counts"])
        w.writerow([pg_version, digest,
                    "blake2b(str(workspace_id),digest_size=8)->int",
                    N_WORKSPACES, total_rows, MAX_ROWS_PER_WS, L, SEED,
                    ";".join(f"{a}->{b}" for a, b in A_TRANSITIONS),
                    ";".join(f"{a}->{b}" for a, b in B_TRANSITIONS),
                    ";".join(str(p) for p in C_PHYSICALS),
                    ";".join(str(x) for x in C_LOGICAL_COUNTS)])

    print(f"\n  {pg_version} | artifacts in {RESULTS}")


if __name__ == "__main__":
    main()
