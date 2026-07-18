"""Measure memcached slab-allocator waste and calcification on real running instances.

memcached never allocates exactly what you store. It carves memory into pages, splits
each page into fixed-size chunks belonging to one slab class, and rounds every item UP
to the nearest chunk. So the bytes you asked for (mem_requested) and the bytes it pinned
(used_chunks * chunk_size) are two different numbers, and the gap is pure internal
fragmentation you paid RAM for.

Three experiments, each on a fresh digest-pinned memcached (this script drives docker
directly so it can vary -m / -f / -o per case):

  1. Slab rounding waste - same item count, a size that lands JUST OVER a slab boundary
     (worst case) vs a size that fills a chunk snugly (best case). Per-class waste.
  2. Growth-factor knob - the worst-case size under default -f 1.25 vs a tighter -f 1.08.
  3. Slab calcification - fill the small class, then switch to large items with
     slab_automove OFF vs aggressive (=2), and watch evictions with free RAM sitting there.

Stats come straight off the wire: `stats`, `stats slabs`, `stats items` parsed from the
raw text protocol. Env: MC_HOST (127.0.0.1), MC_PORT (11311), RESULTS_DIR (./results),
MC_IMAGE (the pinned digest below), N_ITEMS (400000 for exp 1/2).

These are laptop numbers that demonstrate the mechanism, not a capacity plan.
"""
import csv
import os
import socket
import subprocess
import sys
import time

from pymemcache.client.base import Client
from pymemcache.exceptions import MemcacheServerError

HOST = os.environ.get("MC_HOST", "127.0.0.1")
PORT = int(os.environ.get("MC_PORT", "11311"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
IMAGE = os.environ.get(
    "MC_IMAGE",
    "memcached:1.6@sha256:dc561d52bb8ad3c038867123ed2dc8357c4c128f047cdbd526cd65cf39408cbd",
)
N_ITEMS = int(os.environ.get("N_ITEMS", "400000"))
CONTAINER = "mc-slabs-bench"

# constant-length keys so per-item item overhead (header + key) stays constant
def key(i):
    return f"k{i:015d}"  # 16 bytes

KEY_LEN = len(key(0))


# ----------------------------------------------------------------------------- docker
def mc_start(flags):
    subprocess.run(["docker", "rm", "-f", CONTAINER],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = ["docker", "run", "-d", "--rm", "--name", CONTAINER,
           "-p", f"{HOST}:{PORT}:11211", IMAGE, "memcached"] + [str(f) for f in flags]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    # wait for the port to answer `version`
    for _ in range(200):
        try:
            s = socket.create_connection((HOST, PORT), timeout=1)
            s.sendall(b"version\r\n")
            if s.recv(64).startswith(b"VERSION"):
                s.close()
                return
            s.close()
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("memcached did not come up")


def mc_stop():
    subprocess.run(["docker", "rm", "-f", CONTAINER],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------------------------------------------------------- raw stats
def _intify(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return float(v)
        except (ValueError, TypeError):
            return v


def raw_stats(arg=""):
    s = socket.create_connection((HOST, PORT), timeout=5)
    s.sendall((f"stats {arg}\r\n" if arg else "stats\r\n").encode())
    buf = b""
    while b"END\r\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    out = {}
    for line in buf.decode(errors="replace").split("\r\n"):
        if line.startswith("STAT "):
            parts = line.split(" ", 2)
            if len(parts) == 3:
                out[parts[1]] = _intify(parts[2])
    return out


def slab_classes():
    """Return ({class_id: {field: val}}, {global_field: val}) from `stats slabs`."""
    raw = raw_stats("slabs")
    classes, glob = {}, {}
    for k, v in raw.items():
        if ":" in k:
            cid, field = k.split(":", 1)
            classes.setdefault(int(cid), {})[field] = v
        else:
            glob[k] = v
    return classes, glob


def item_stats():
    """Return {class_id: {field: val}} from `stats items` (items:CID:field)."""
    raw = raw_stats("items")
    out = {}
    for k, v in raw.items():
        if k.startswith("items:"):
            _, cid, field = k.split(":", 2)
            out.setdefault(int(cid), {})[field] = v
    return out


def merged_classes():
    """Per-class view combining `stats slabs` (chunk_size, used_chunks, pages, free)
    with `stats items` (mem_requested, evicted, outofmemory). In memcached 1.6
    mem_requested is only reported by `stats items`, not `stats slabs`."""
    classes, glob = slab_classes()
    items = item_stats()
    for cid, fields in items.items():
        classes.setdefault(cid, {})
        for f in ("mem_requested", "evicted", "outofmemory", "number"):
            if f in fields:
                classes[cid][f] = fields[f]
    return classes, glob


def client():
    # default_noreply=False: sets are acknowledged, so stats read afterwards reflect a
    # fully-applied state (no fire-and-forget race) and store failures surface as errors.
    return Client((HOST, PORT), connect_timeout=5, timeout=30, default_noreply=False)


def fill(cli, value, count, start=0):
    """set `count` items of `value` with distinct keys via batched set_many.
    Returns (stored_ok, server_errors)."""
    ok, err = 0, 0
    batch = {}
    B = 5000
    for i in range(start, start + count):
        batch[key(i)] = value
        if len(batch) >= B:
            try:
                failed = cli.set_many(batch)   # returns list of keys NOT stored
                ok += len(batch) - len(failed)
                err += len(failed)
            except MemcacheServerError:
                err += len(batch)
            batch = {}
    if batch:
        try:
            failed = cli.set_many(batch)
            ok += len(batch) - len(failed)
            err += len(failed)
        except MemcacheServerError:
            err += len(batch)
    return ok, err


# ----------------------------------------------------------------------------- probing
def probe_one(value_size, flags=("-m", 2048, "-f", 1.25)):
    """Store a single item of value_size on a fresh instance; return (class_id,
    chunk_size, ntotal) where ntotal = the item's real byte cost (mem_requested)."""
    mc_start(list(flags))
    try:
        cli = client()
        cli.set(key(0), b"x" * value_size)
        cli.get(key(0))  # ensure the store is fully applied before we read stats
        active = []
        for _ in range(20):
            classes, _ = merged_classes()
            active = [(cid, c) for cid, c in classes.items() if c.get("used_chunks", 0) >= 1]
            if len(active) == 1 and "mem_requested" in active[0][1]:
                break
            time.sleep(0.05)
        assert len(active) == 1, f"expected 1 active class, got {[a[0] for a in active]}"
        cid, c = active[0]
        return cid, c["chunk_size"], c["mem_requested"]
    finally:
        mc_stop()


def walk_ladder(overhead):
    """Walk one item per class up the ladder, collecting adjacent (class_id, chunk_size)."""
    ladder, seen = [], set()
    v = 700
    for _ in range(30):
        cid, chunk, ntotal = probe_one(v)
        if cid not in seen:
            seen.add(cid)
            ladder.append((cid, chunk))
        if chunk > 4000:
            break
        v = chunk - overhead + 1   # ntotal = chunk+1 -> next class up
    return ladder


# ----------------------------------------------------------------------------- exp 1
def per_class_waste_rows(classes):
    """[(class_id, chunk_size, used_chunks, mem_requested, allocated, waste, pct)] + totals."""
    rows, t_req, t_alloc = [], 0, 0
    for cid in sorted(classes):
        c = classes[cid]
        used = c.get("used_chunks", 0)
        if used == 0:
            continue
        chunk = c["chunk_size"]
        req = c.get("mem_requested", 0)
        alloc = used * chunk
        waste = alloc - req
        pct = 100.0 * waste / alloc if alloc else 0.0
        rows.append([cid, chunk, used, req, alloc, waste, round(pct, 2)])
        t_req += req
        t_alloc += alloc
    t_waste = t_alloc - t_req
    t_pct = 100.0 * t_waste / t_alloc if t_alloc else 0.0
    rows.append(["TOTAL", "", "", t_req, t_alloc, t_waste, round(t_pct, 2)])
    return rows, t_req, t_alloc, t_waste, t_pct


def write_case_csv(name, rows, glob):
    path = os.path.join(RESULTS, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slab_class", "chunk_size", "used_chunks",
                    "mem_requested", "allocated", "waste_bytes", "waste_pct"])
        w.writerows(rows)
        w.writerow([])
        w.writerow(["global_bytes", glob.get("bytes")])
        w.writerow(["global_total_malloced", glob.get("total_malloced")])
    return path


def run_case(value_size, csv_name, factor=1.25):
    mc_start(["-m", 2048, "-f", factor])
    try:
        cli = client()
        ok, err = fill(cli, b"x" * value_size, N_ITEMS)
        classes, sglob = merged_classes()
        g = raw_stats()
        glob = {"bytes": g.get("bytes"), "total_malloced": sglob.get("total_malloced")}
        rows, t_req, t_alloc, t_waste, t_pct = per_class_waste_rows(classes)
        write_case_csv(csv_name, rows, glob)
        # the class this workload landed in
        landed = max((cid for cid, c in classes.items() if c.get("used_chunks", 0) >= 1),
                     key=lambda cid: classes[cid]["used_chunks"])
        return {
            "value_size": value_size, "stored_ok": ok, "server_errors": err,
            "landed_class": landed, "landed_chunk": classes[landed]["chunk_size"],
            "mem_requested": t_req, "allocated": t_alloc,
            "waste_bytes": t_waste, "waste_pct": t_pct,
            "active_slabs": sglob.get("active_slabs"),
        }
    finally:
        mc_stop()


# ----------------------------------------------------------------------------- exp 3
def calcification(automove, small_val=100, large_val=8192,
                  small_cap=700000, large_count=5000, passes=15, settle=1.0):
    """Fill the small class to eviction, then repeatedly write a large-item working set
    that WOULD fit in cache if it could get pages. With automove off the large class is
    frozen at its lone page and thrashes; with automove=2 memcached reassigns pages to it
    and the large working set goes resident. `large_count` is sized to fit after a full
    rebalance (~44 of 64 pages), so the contrast shows up as large-class evictions."""
    flags = ["-m", 64, "-o", f"slab_automove={automove}"]
    mc_start(flags)
    out = {"automove": automove, "phases": {}}
    try:
        cli = client()

        # discover small & large class ids by a single probe of each on this instance
        cli.set("probe_small", b"x" * small_val)
        cli.set("probe_large", b"x" * large_val)
        classes, _ = slab_classes()
        active = {cid for cid, c in classes.items() if c.get("used_chunks", 0) >= 1}
        # small item is the lower chunk_size, large the higher
        by_chunk = sorted(active, key=lambda cid: classes[cid]["chunk_size"])
        small_cid, large_cid = by_chunk[0], by_chunk[-1]
        cli.delete("probe_small")
        cli.delete("probe_large")

        # ---- Phase A: small items until the small class is full and evicting
        val = b"s" * small_val
        written, batch, B = 0, {}, 5000
        g = raw_stats()
        start_ev = g.get("evictions", 0)
        while written < small_cap:
            for _ in range(B):
                batch[key(written)] = val
                written += 1
            cli.set_many(batch)
            batch = {}
            if raw_stats().get("evictions", 0) - start_ev > 20000:
                break
        classes, sglob = slab_classes()
        items = item_stats()
        g = raw_stats()
        out["phases"]["A_small"] = {
            "written": written,
            "evictions": g.get("evictions", 0),
            "small_class": small_cid,
            "small_total_pages": classes.get(small_cid, {}).get("total_pages"),
            "small_used_chunks": classes.get(small_cid, {}).get("used_chunks"),
            "small_free_chunks": classes.get(small_cid, {}).get("free_chunks"),
            "large_class": large_cid,
            "large_total_pages": classes.get(large_cid, {}).get("total_pages", 0),
        }
        ev_after_A = g.get("evictions", 0)

        # ---- Phase B: a large-item working set, rewritten under sustained pressure so an
        # automove thread (if any) has both the eviction signal and the time to rebalance.
        lval = b"L" * large_val
        lkeys = [key(10_000_000 + i) for i in range(large_count)]
        errs = 0
        for _ in range(passes):
            try:
                failed = cli.set_many({k: lval for k in lkeys})
                errs += len(failed)
            except MemcacheServerError:
                errs += large_count
            time.sleep(settle)
        classes, sglob = slab_classes()
        items = item_stats()
        g = raw_stats()
        out["phases"]["B_large"] = {
            "large_working_set": large_count,
            "large_server_errors": errs,
            "evictions_global": g.get("evictions", 0),
            "evictions_since_A": g.get("evictions", 0) - ev_after_A,
            "large_class": large_cid,
            "large_total_pages": classes.get(large_cid, {}).get("total_pages", 0),
            "large_used_chunks": classes.get(large_cid, {}).get("used_chunks", 0),
            "large_evicted": items.get(large_cid, {}).get("evicted", 0),
            "large_outofmemory": items.get(large_cid, {}).get("outofmemory", 0),
            "small_class": small_cid,
            "small_total_pages": classes.get(small_cid, {}).get("total_pages"),
            "small_free_chunks": classes.get(small_cid, {}).get("free_chunks"),
            "curr_items": g.get("curr_items"),
        }
        return out
    finally:
        mc_stop()


# ----------------------------------------------------------------------------- main
def main():
    os.makedirs(RESULTS, exist_ok=True)
    log = []

    def say(*a):
        line = " ".join(str(x) for x in a)
        print(line)
        log.append(line)

    # version + overhead from a first single-item probe
    mc_start(["-m", 2048, "-f", 1.25])
    version = raw_stats().get("version")
    mc_stop()
    ntotal_100 = probe_one(100)[2]
    overhead = ntotal_100 - 100

    say("=" * 66)
    say("memcached", version, "| item overhead (header+key, key len %d): %d bytes" % (KEY_LEN, overhead))
    say("=" * 66)

    # ---------- probe the slab ladder ----------
    ladder = walk_ladder(overhead)
    say("\nslab-class ladder (class_id: chunk_size), factor 1.25:")
    say("  " + "  ".join(f"{cid}:{ch}" for cid, ch in ladder))

    # pick an adjacent pair whose upper chunk sits ~1-2 KB (good MB fill, ~1.25 step)
    pair = None
    for i in range(len(ladder) - 1):
        c_lo, c_hi = ladder[i][1], ladder[i + 1][1]
        if 1000 <= c_hi <= 2200:
            pair = (c_lo, c_hi)
            break
    if pair is None:
        pair = (ladder[0][1], ladder[1][1])
    c_lo, c_hi = pair

    worst_value = c_lo - overhead + 1   # ntotal = c_lo + 1 -> rounds up to c_hi (near-full step wasted)
    snug_value = c_hi - overhead        # ntotal = c_hi exactly -> fills the chunk

    # confirm they land where intended, nudge if a boundary is off by a byte or two
    def lands_in(val):
        return probe_one(val)[1]
    for _ in range(4):
        if lands_in(worst_value) == c_hi:
            break
        worst_value += 1
    for _ in range(4):
        if lands_in(snug_value) == c_hi:
            break
        snug_value -= 1

    say(f"\nchosen boundary: chunk {c_lo} -> {c_hi} (step {c_hi - c_lo} bytes)")
    say(f"  worst-case value = {worst_value} B (ntotal just over {c_lo}, rounds to {c_hi})")
    say(f"  snug     value = {snug_value} B (ntotal fills {c_hi})")

    # ---------- EXP 1 ----------
    say("\n" + "=" * 66)
    say(f"EXPERIMENT 1  slab rounding waste  ({N_ITEMS} items each, -m 2048 -f 1.25)")
    say("=" * 66)
    worst = run_case(worst_value, "exp1_worst_case.csv")
    snug = run_case(snug_value, "exp1_best_case.csv")
    for label, r in (("WORST (just over boundary)", worst), ("BEST  (snug fit)", snug)):
        say(f"  {label}")
        say(f"    value {r['value_size']}B -> class {r['landed_class']} (chunk {r['landed_chunk']}), "
            f"stored {r['stored_ok']}")
        say(f"    mem_requested {r['mem_requested']:,}  allocated {r['allocated']:,}")
        say(f"    WASTE {r['waste_bytes']:,} bytes = {r['waste_pct']:.1f}% of allocated RAM")
    say(f"  => same {N_ITEMS} items, same chunk {worst['landed_chunk']}: "
        f"{worst['waste_pct']:.1f}% of allocated RAM wasted to rounding (worst) "
        f"vs {snug['waste_pct']:.1f}% (snug) — "
        f"{worst['waste_bytes'] / 1e6:.0f} MB thrown away holding identical data volume")

    # ---------- EXP 2 ----------
    say("\n" + "=" * 66)
    say(f"EXPERIMENT 2  growth-factor knob  (worst-case {worst_value}B, {N_ITEMS} items)")
    say("=" * 66)
    # count total slab classes per factor by storing one near-max item (its class id == top class)
    def num_classes(factor):
        mc_start(["-m", 2048, "-f", factor])
        try:
            client().set(key(0), b"x" * 900000)   # ~900 KB, near the 1 MB item ceiling
            classes, _ = slab_classes()
            top = max(cid for cid, c in classes.items() if c.get("used_chunks", 0) >= 1)
            return top
        finally:
            mc_stop()

    exp2 = []
    for factor in (1.25, 1.08):
        r = run_case(worst_value, f"exp2_f{str(factor).replace('.', '_')}.csv", factor=factor)
        ncls = num_classes(factor)
        r["factor"], r["num_slab_classes"] = factor, ncls
        exp2.append(r)
        say(f"  -f {factor}: value lands in class {r['landed_class']} (chunk {r['landed_chunk']}), "
            f"{ncls} slab classes total")
        say(f"    mem_requested {r['mem_requested']:,}  allocated {r['allocated']:,}  "
            f"WASTE {r['waste_pct']:.1f}%")
    say(f"  => tighter -f 1.08 cuts waste {exp2[0]['waste_pct']:.1f}% -> {exp2[1]['waste_pct']:.1f}%, "
        f"cost = {exp2[1]['num_slab_classes']} slab classes vs {exp2[0]['num_slab_classes']}")

    with open(os.path.join(RESULTS, "exp2_growth_factor.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["factor", "num_slab_classes", "landed_class", "chunk_size",
                    "mem_requested", "allocated", "waste_pct"])
        for r in exp2:
            w.writerow([r["factor"], r["num_slab_classes"], r["landed_class"],
                        r["landed_chunk"], r["mem_requested"], r["allocated"],
                        round(r["waste_pct"], 2)])

    # ---------- EXP 3 ----------
    say("\n" + "=" * 66)
    say("EXPERIMENT 3  slab calcification  (-m 64, small then large items)")
    say("=" * 66)
    off = calcification(0)
    aggr = calcification(2)
    passes_note = "rewritten 15x, 1s apart"

    exp3_rows = []
    for mode in (off, aggr):
        am = mode["automove"]
        A, B = mode["phases"]["A_small"], mode["phases"]["B_large"]
        say(f"  automove={am}")
        say(f"    phase A (small): wrote {A['written']}, evictions {A['evictions']}, "
            f"small class {A['small_class']} pages {A['small_total_pages']}, "
            f"large class {A['large_class']} pages {A['large_total_pages']}")
        say(f"    phase B (large working set {B['large_working_set']}, {passes_note}):")
        say(f"      large class pages {B['large_total_pages']}, resident chunks {B['large_used_chunks']}, "
            f"LARGE-CLASS evictions {B['large_evicted']:,}, OOM {B['large_outofmemory']}")
        say(f"      global evictions since A {B['evictions_since_A']:,}, "
            f"small free_chunks {B['small_free_chunks']}, curr_items {B['curr_items']:,}")
        exp3_rows.append([am, "A_small", A["evictions"], A["large_total_pages"],
                          A["small_free_chunks"], 0])
        exp3_rows.append([am, "B_large", B["evictions_global"], B["large_total_pages"],
                          B["small_free_chunks"], B["large_evicted"]])

    off_lp = off["phases"]["B_large"]["large_total_pages"]
    aggr_lp = aggr["phases"]["B_large"]["large_total_pages"]
    off_le = off["phases"]["B_large"]["large_evicted"]
    aggr_le = aggr["phases"]["B_large"]["large_evicted"]
    say(f"  => the large working set is the SAME size in both runs; only the allocator differs.")
    say(f"     automove=0: large class frozen at {off_lp} page, thrashes -> {off_le:,} large-class evictions")
    say(f"     automove=2: rebalanced to {aggr_lp} pages, working set resident -> {aggr_le:,} large-class evictions")
    say(f"     => calcification (RAM locked to the dead small class) vs pages reassigned to where the load moved.")

    with open(os.path.join(RESULTS, "exp3_calcification.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["automove", "phase", "evictions_global", "large_class_pages",
                    "small_class_free_chunks", "large_class_evicted"])
        w.writerows(exp3_rows)

    # ---------- metadata + summary ----------
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["memcached_version", "image_digest", "pymemcache_version",
                    "n_items", "worst_value_bytes", "snug_value_bytes",
                    "item_overhead_bytes", "exp1_worst_waste_pct", "exp1_snug_waste_pct"])
        digest = IMAGE.split("@", 1)[1] if "@" in IMAGE else IMAGE
        import pymemcache
        w.writerow([version, digest, pymemcache.__version__, N_ITEMS,
                    worst_value, snug_value, overhead,
                    round(worst["waste_pct"], 2), round(snug["waste_pct"], 2)])

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(log) + "\n")
    say(f"\n  memcached {version} | artifacts in {RESULTS}")


if __name__ == "__main__":
    try:
        main()
    finally:
        mc_stop()
