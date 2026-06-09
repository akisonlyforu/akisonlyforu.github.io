"""Robotic ad-click invalidation on a click firehose: flag high-frequency bot
sources and drop exact-replay click-ids in FIXED memory.

The naive approach keeps an exact per-source counter (dict / Redis HASH) and an
exact set of seen click-ids. Both grow UNBOUNDED with unique sources / unique ids.
The probabilistic approach does the same job in fixed memory with RedisBloom:
Count-Min Sketch (per-source frequency), Top-K (the heavy-hitter list), and a
Bloom filter (id dedup).

One deterministic synthetic stream (fixed seed) drives four experiments:
  1. Exact counting grows unbounded  - dict + Redis HASH + id-set bytes vs unique sources.
  2. Count-Min Sketch                - fixed memory, estimate error for bots vs humans.
  3. Top-K                           - recall of planted bots in bounded memory.
  4. Bloom filter dedup vs exact set - memory, false-positive rate, dedup recall.

Env: REDIS_HOST (127.0.0.1), REDIS_PORT (6399), RESULTS_DIR (results/),
     IMAGE_DIGEST, plus SCALE knobs (see below) if you want a smaller run.
"""
import csv
import os
import random
import sys
import time

import redis

HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("REDIS_PORT", "6399"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
IMAGE_DIGEST = os.environ.get(
    "IMAGE_DIGEST",
    "sha256:798ab84d9f266936b034ab11c4d04a2b8e4b441884c5aa7d17ac951eefdf742a",
)

SEED = int(os.environ.get("SEED", "1337"))

# --- stream shape (all overridable, defaults are the numbers in the post) ---
N_HUMANS = int(os.environ.get("N_HUMANS", "1000000"))   # unique human sources
N_BOTS = int(os.environ.get("N_BOTS", "20"))            # planted bot sources
BOT_CLICKS = int(os.environ.get("BOT_CLICKS", "50000")) # clicks per bot
REPLAY_P = float(os.environ.get("REPLAY_P", "0.12"))    # fraction of events that replay an id
REPLAY_POOL = 200_000                                    # recent-id pool for replays
HELD_OUT = int(os.environ.get("HELD_OUT", "200000"))    # never-seen ids for Bloom FP test
HUMAN_SAMPLE = 2000                                      # humans sampled for CMS error

# human click count distribution (Zipf-ish: most click once)
HUMAN_WEIGHTS = [(1, 0.70), (2, 0.20), (3, 0.07), (4, 0.03)]

# --- probabilistic structure dimensions (all FIXED, independent of stream size) ---
CMS_WIDTH = int(os.environ.get("CMS_WIDTH", "20000"))
CMS_DEPTH = int(os.environ.get("CMS_DEPTH", "5"))
TOPK_K = int(os.environ.get("TOPK_K", "50"))
TOPK_WIDTH = int(os.environ.get("TOPK_WIDTH", "1000"))
TOPK_DEPTH = int(os.environ.get("TOPK_DEPTH", "8"))
TOPK_DECAY = float(os.environ.get("TOPK_DECAY", "0.9"))
BLOOM_CAP = int(os.environ.get("BLOOM_CAP", "2500000"))
BLOOM_ERR = float(os.environ.get("BLOOM_ERR", "0.001"))

CHECKPOINTS = [100_000, 250_000, 500_000, 1_000_000]
CHUNK = 10_000


def deep_size_dict(d):
    return sys.getsizeof(d) + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in d.items())


def deep_size_set(s):
    return sys.getsizeof(s) + sum(sys.getsizeof(x) for x in s)


def human_click_counts(rng, n):
    """Deterministic per-human click counts from the weighted distribution."""
    vals = [v for v, _ in HUMAN_WEIGHTS]
    weights = [w for _, w in HUMAN_WEIGHTS]
    return rng.choices(vals, weights=weights, k=n)


def run():
    os.makedirs(RESULTS, exist_ok=True)
    rng = random.Random(SEED)
    r = redis.Redis(host=HOST, port=PORT)
    r.ping()
    ver = r.info("server")["redis_version"]

    # fresh probabilistic structures, fixed dimensions
    r.flushall()
    r.cms().initbydim("cms:src", CMS_WIDTH, CMS_DEPTH)
    r.topk().reserve("topk:src", TOPK_K, TOPK_WIDTH, TOPK_DEPTH, TOPK_DECAY)
    r.bf().reserve("bloom:id", BLOOM_ERR, BLOOM_CAP)
    r.execute_command("HSET", "exact:src", "__seed__", "0")  # exact per-source counter (Redis HASH)

    # exact structures (Python)
    exact_counts = {}      # source -> count
    seen_ids = set()       # every unique click-id (exact dedup baseline)

    # replay machinery
    replay_pool = []       # recent fresh ids
    seq = 0                # fresh id counter
    replay_events = 0
    replay_sample = []     # sample of replayed ids, for Bloom dedup-recall check

    # exp1 growth checkpoints (only those the population can reach; the final
    # total is always recorded below)
    checkpoints = [c for c in CHECKPOINTS if c <= N_HUMANS]
    growth_rows = []
    next_ckpt = 0

    # per-human plan and bot quotas
    plan = human_click_counts(rng, N_HUMANS)
    bot_remaining = [BOT_CLICKS] * N_BOTS

    total_events = 0
    t0 = time.time()

    # chunk buffers, flushed to Redis every CHUNK events
    buf_src = []           # source strings (for CMS incrby + TopK add)
    buf_fresh_ids = []     # fresh ids to add to Bloom
    hpipe = r.pipeline()   # exact HASH hincrby

    def flush():
        if buf_src:
            r.cms().incrby("cms:src", buf_src, [1] * len(buf_src))
            r.topk().add("topk:src", *buf_src)
            hpipe.execute()
        if buf_fresh_ids:
            r.bf().madd("bloom:id", *buf_fresh_ids)
        buf_src.clear()
        buf_fresh_ids.clear()

    def emit(source):
        """Register one click from `source`, generating (and tracking) its id."""
        nonlocal seq, replay_events, total_events
        # id: replay an earlier id or mint a fresh one
        if replay_pool and rng.random() < REPLAY_P:
            cid = replay_pool[rng.randrange(len(replay_pool))]
            replay_events += 1
            if len(replay_sample) < 5000:
                replay_sample.append(cid)
        else:
            cid = f"c:{seq}"
            seq += 1
            if cid not in seen_ids:
                seen_ids.add(cid)
                buf_fresh_ids.append(cid)
                replay_pool.append(cid)
                if len(replay_pool) > REPLAY_POOL:
                    replay_pool.pop(0)
        # exact per-source count
        exact_counts[source] = exact_counts.get(source, 0) + 1
        buf_src.append(source)
        hpipe.hincrby("exact:src", source, 1)
        total_events += 1
        if len(buf_src) >= CHUNK:
            flush()

    def record_checkpoint(label):
        flush()
        hash_bytes = r.memory_usage("exact:src")
        growth_rows.append({
            "unique_sources": len(exact_counts),
            "total_clicks": total_events,
            "exact_dict_bytes": deep_size_dict(exact_counts),
            "exact_hash_redis_bytes": hash_bytes,
            "unique_click_ids": len(seen_ids),
            "exact_idset_bytes": deep_size_set(seen_ids),
        })
        print(f"  checkpoint {label:>9} sources: "
              f"dict={growth_rows[-1]['exact_dict_bytes']/1e6:6.1f} MB  "
              f"redis_hash={hash_bytes/1e6:6.1f} MB  "
              f"idset={growth_rows[-1]['exact_idset_bytes']/1e6:6.1f} MB "
              f"({len(seen_ids)} ids)")

    def maybe_checkpoint():
        nonlocal next_ckpt
        if next_ckpt < len(checkpoints) and len(exact_counts) >= checkpoints[next_ckpt]:
            record_checkpoint(str(checkpoints[next_ckpt]))
            next_ckpt += 1

    print("=" * 66)
    print("Feeding synthetic click firehose (deterministic, seed=%d)" % SEED)
    print("=" * 66)
    for i in range(N_HUMANS):
        for _ in range(plan[i]):
            emit(f"h:{i}")
        # spread bot clicks across the stream, round-robin
        b = i % N_BOTS
        if bot_remaining[b] > 0:
            emit(f"bot:{b}")
            bot_remaining[b] -= 1
        maybe_checkpoint()
    # drain any bot clicks not yet emitted (if N_HUMANS < total bot quota)
    b = 0
    while any(bot_remaining):
        if bot_remaining[b] > 0:
            emit(f"bot:{b}")
            bot_remaining[b] -= 1
        b = (b + 1) % N_BOTS
    flush()
    # always record the final total (dedup if it coincides with the last checkpoint)
    if not growth_rows or growth_rows[-1]["unique_sources"] != len(exact_counts):
        record_checkpoint("final")

    feed_dt = time.time() - t0
    print(f"  fed {total_events} clicks in {feed_dt:.1f}s "
          f"({total_events/feed_dt:,.0f} clicks/s), {len(seen_ids)} unique ids, "
          f"{replay_events} replay events")

    # ---- write exp1 growth ----
    with open(os.path.join(RESULTS, "exp1_exact_growth.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(growth_rows[0].keys()))
        w.writeheader()
        w.writerows(growth_rows)

    # ---- EXP 2: Count-Min Sketch error ----
    cms_mem = r.memory_usage("cms:src")
    bot_ids = [f"bot:{j}" for j in range(N_BOTS)]
    human_ids = rng.sample([s for s in exact_counts if s.startswith("h:")], HUMAN_SAMPLE)
    cms_rows = []

    def add_cms_rows(sources, kind):
        ests = r.cms().query("cms:src", *sources)
        for s, est in zip(sources, ests):
            true = exact_counts[s]
            cms_rows.append({"source": s, "kind": kind, "true_count": true,
                             "cms_estimate": est, "abs_error": est - true})

    add_cms_rows(bot_ids, "bot")
    add_cms_rows(human_ids, "human")
    with open(os.path.join(RESULTS, "exp2_cms_error.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "kind", "true_count", "cms_estimate", "abs_error"])
        w.writeheader()
        w.writerows(cms_rows)

    def err_stats(kind):
        errs = sorted(row["abs_error"] for row in cms_rows if row["kind"] == kind)
        if not errs:
            return {}
        n = len(errs)
        return {
            "n": n,
            "mean": sum(errs) / n,
            "median": errs[n // 2],
            "p99": errs[min(n - 1, int(n * 0.99))],
            "max": errs[-1],
        }

    bot_err = err_stats("bot")
    human_err = err_stats("human")

    # ---- EXP 3: Top-K recall ----
    topk_mem = r.memory_usage("topk:src")
    topk_list = r.topk().list("topk:src", withcount=True)
    topk_rows = []
    planted = set(bot_ids)
    found = set()
    false_incl = 0
    for rank, (item, count) in enumerate(
        zip(topk_list[0::2], topk_list[1::2]), start=1):
        item = item.decode() if isinstance(item, bytes) else item
        if item in planted:
            kind = "planted_bot"
            found.add(item)
        elif item in exact_counts:
            kind = "human"
            false_incl += 1
        else:
            kind = "unknown"
        topk_rows.append({"rank": rank, "item": item, "topk_count": int(count),
                          "true_count": exact_counts.get(item, ""), "kind": kind})
    with open(os.path.join(RESULTS, "exp3_topk_list.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "item", "topk_count", "true_count", "kind"])
        w.writeheader()
        w.writerows(topk_rows)
    topk_recall = len(found) / N_BOTS
    # the real failure mode: a human ranked above a genuine bot (masquerading as a
    # heavier hitter). With k > N_BOTS the tail necessarily fills with humans, so
    # "false inclusion" is only meaningful for humans ranked among the bots.
    last_bot_rank = max((row["rank"] for row in topk_rows if row["kind"] == "planted_bot"),
                        default=0)
    humans_above_last_bot = sum(1 for row in topk_rows
                                if row["kind"] == "human" and row["rank"] < last_bot_rank)

    # ---- EXP 4: Bloom dedup vs exact set ----
    bloom_mem = r.memory_usage("bloom:id")
    exact_set_bytes = deep_size_set(seen_ids)

    # false positives: query ids that were NEVER in the stream
    held = [f"held:{i}" for i in range(HELD_OUT)]
    fp = 0
    for i in range(0, len(held), CHUNK):
        chunk = held[i:i + CHUNK]
        res = r.bf().mexists("bloom:id", *chunk)
        fp += sum(int(x) for x in res)
    bloom_fp_rate = fp / len(held)
    exact_fp_rate = 0.0  # a set never false-positives

    # dedup recall: a sample of ids that WERE replayed must be present
    if replay_sample:
        rres = r.bf().mexists("bloom:id", *replay_sample[:5000])
        bloom_recall = sum(int(x) for x in rres) / len(replay_sample[:5000])
    else:
        bloom_recall = float("nan")
    exact_recall = 1.0  # exact set catches every replay by construction

    dedup_rows = [
        {"structure": "exact_python_set", "bytes": exact_set_bytes,
         "false_positive_rate": exact_fp_rate, "dedup_recall": exact_recall,
         "note": f"{len(seen_ids)} unique ids"},
        {"structure": "redis_bloom_filter", "bytes": bloom_mem,
         "false_positive_rate": bloom_fp_rate, "dedup_recall": bloom_recall,
         "note": f"cap={BLOOM_CAP} err={BLOOM_ERR}"},
    ]
    with open(os.path.join(RESULTS, "exp4_dedup.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["structure", "bytes", "false_positive_rate",
                                          "dedup_recall", "note"])
        w.writeheader()
        w.writerows(dedup_rows)

    # ---- run metadata ----
    meta = {
        "redis_version": ver,
        "image_digest": IMAGE_DIGEST,
        "redis_py_version": redis.__version__,
        "seed": SEED,
        "n_humans": N_HUMANS,
        "n_bots": N_BOTS,
        "bot_clicks": BOT_CLICKS,
        "total_clicks": total_events,
        "unique_click_ids": len(seen_ids),
        "replay_p": REPLAY_P,
        "replay_events": replay_events,
        "cms_width": CMS_WIDTH,
        "cms_depth": CMS_DEPTH,
        "topk_k": TOPK_K,
        "topk_width": TOPK_WIDTH,
        "topk_depth": TOPK_DEPTH,
        "topk_decay": TOPK_DECAY,
        "bloom_capacity": BLOOM_CAP,
        "bloom_error": BLOOM_ERR,
    }
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(meta.keys()))
        w.writerow(list(meta.values()))

    # fixed probabilistic total
    prob_total = cms_mem + topk_mem + bloom_mem
    final = growth_rows[-1]

    # ---- summary ----
    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("\n" + "=" * 66)
    out("SUMMARY  robotic ad-click invalidation: exact vs probabilistic")
    out("=" * 66)
    out(f"redis {ver} | redis-py {redis.__version__} | image {IMAGE_DIGEST[:19]}...")
    out(f"stream: {N_HUMANS:,} humans + {N_BOTS} bots x {BOT_CLICKS:,} clicks "
        f"= {total_events:,} clicks, {len(seen_ids):,} unique ids, "
        f"{replay_events:,} replays")
    out("")
    out("EXP 1  exact counting grows unbounded (source counter + id set)")
    for row in growth_rows:
        out(f"  {row['unique_sources']:>9,} sources -> "
            f"dict {row['exact_dict_bytes']/1e6:7.1f} MB | "
            f"redis HASH {row['exact_hash_redis_bytes']/1e6:7.1f} MB | "
            f"id-set {row['exact_idset_bytes']/1e6:7.1f} MB "
            f"({row['unique_click_ids']:,} ids)")
    out("")
    out("EXP 2  Count-Min Sketch: FIXED memory, still finds heavy hitters")
    out(f"  CMS width={CMS_WIDTH} depth={CMS_DEPTH} -> {cms_mem/1e6:.2f} MB (fixed) "
        f"vs exact dict {final['exact_dict_bytes']/1e6:.1f} MB")
    out(f"  bot overestimate   : mean {bot_err['mean']:.0f}  median {bot_err['median']}  "
        f"p99 {bot_err['p99']}  max {bot_err['max']}  (true ~{BOT_CLICKS:,})")
    out(f"  human overestimate : mean {human_err['mean']:.1f}  median {human_err['median']}  "
        f"p99 {human_err['p99']}  max {human_err['max']}  (true 1-4)")
    out("")
    out("EXP 3  Top-K: the bot list in bounded memory")
    out(f"  TopK k={TOPK_K} width={TOPK_WIDTH} depth={TOPK_DEPTH} -> {topk_mem/1e6:.3f} MB (fixed)")
    out(f"  recall: {len(found)}/{N_BOTS} planted bots in top-{TOPK_K} "
        f"= {topk_recall*100:.0f}%   bots occupy ranks 1..{last_bot_rank}")
    out(f"  humans ranked above any bot: {humans_above_last_bot}   "
        f"(the k={TOPK_K} list's remaining {false_incl} slots are ordinary humans)")
    shown = topk_rows[:min(len(topk_rows), N_BOTS + 3)]
    for row in shown:
        out(f"    #{row['rank']:<2} {row['item']:<10} topk={row['topk_count']:>7} "
            f"true={row['true_count']:>7} [{row['kind']}]")
    out("")
    out("EXP 4  Bloom dedup vs exact set")
    out(f"  exact python set : {exact_set_bytes/1e6:7.1f} MB  FP=0.000%  recall=100%")
    out(f"  redis bloom      : {bloom_mem/1e6:7.1f} MB  "
        f"FP={bloom_fp_rate*100:.3f}% ({fp}/{HELD_OUT})  recall={bloom_recall*100:.0f}%")
    out("")
    out("HEADLINE")
    out(f"  exact source-counter + id-set climb to "
        f"{(final['exact_dict_bytes']+final['exact_idset_bytes'])/1e6:.0f} MB at "
        f"{final['unique_sources']:,} sources and keep growing.")
    out(f"  CMS+TopK+Bloom = {prob_total/1e6:.1f} MB FIXED, flagged "
        f"{topk_recall*100:.0f}% of planted bots and caught {exact_recall*100:.0f}% of "
        f"replays at {bloom_fp_rate*100:.3f}% false positives.")

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    run()
