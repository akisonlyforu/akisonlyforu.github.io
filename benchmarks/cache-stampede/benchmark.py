"""Thundering-herd fixes, measured. One hot computed key, many concurrent readers,
a ~300ms recompute. Compare:

  herd         - plain TTL, everyone recomputes on the synchronized miss
  lock         - SET NX recompute lock: one holder computes, the rest wait
  lock_crash   - same lock, but the holder is killed mid-recompute 1 in 5 times
  probabilistic- XFetch early recompute: one reader refreshes AHEAD of expiry,
                 in the background, so no caller ever blocks or stampedes

Plus a jitter mini-experiment: many keys with a synchronized vs jittered TTL.

Env: REDIS_HOST (127.0.0.1), REDIS_PORT (6395).
"""
import csv
import json
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import redis

HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("REDIS_PORT", "6395"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

WORKERS = 64
DURATION = 12.0
RECOMPUTE_MS = 300
TTL = 2.0
LOCK_TTL = 3.0
BETA = 1.0
THINK_MS = 15
KEY = "price:aggregate"

pool = redis.ConnectionPool(host=HOST, port=PORT, decode_responses=True, max_connections=WORKERS + 8)
bg = ThreadPoolExecutor(max_workers=8)


def r():
    return redis.Redis(connection_pool=pool)


def recompute():
    """The expensive aggregate. Returns (value, seconds_it_took)."""
    t0 = time.time()
    time.sleep(RECOMPUTE_MS / 1000.0)
    return f"price@{t0:.3f}", time.time() - t0


# ---- strategies: each returns latency_seconds for one request ----

def do_herd(c):
    t0 = time.time()
    v = c.get(KEY)
    if v is None:
        val, _ = recompute()
        c.set(KEY, val, px=int(TTL*1000))
    return time.time() - t0


def do_lock(c, crash=False):
    t0 = time.time()
    v = c.get(KEY)
    if v is not None:
        return time.time() - t0
    if c.set(KEY + ":lock", "1", nx=True, ex=int(LOCK_TTL)):
        if crash and random.random() < 0.2:
            time.sleep(RECOMPUTE_MS / 1000.0)      # holder dies here: no write, no unlock
            return time.time() - t0
        val, _ = recompute()
        c.set(KEY, val, px=int(TTL*1000))
        c.delete(KEY + ":lock")
        return time.time() - t0
    # wait for the holder's value (convoy); give up after the lock could expire
    deadline = time.time() + LOCK_TTL + 0.5
    while time.time() < deadline:
        v = c.get(KEY)
        if v is not None:
            break
        time.sleep(0.01)
    return time.time() - t0


_refreshing = set()
_refresh_lock = threading.Lock()


def _bg_refresh(c):
    if not c.set(KEY + ":refresh", "1", nx=True, ex=int(LOCK_TTL)):
        return                                     # someone else is already refreshing
    try:
        val, delta = recompute()
        exp = time.time() + TTL
        c.set(KEY, json.dumps({"v": val, "delta": delta, "exp": exp}), ex=int(TTL) + 2)
    finally:
        c.delete(KEY + ":refresh")


def do_probabilistic(c):
    t0 = time.time()
    raw = c.get(KEY)
    if raw is None:
        val, delta = recompute()
        exp = time.time() + TTL
        c.set(KEY, json.dumps({"v": val, "delta": delta, "exp": exp}), ex=int(TTL) + 2)
        return time.time() - t0
    d = json.loads(raw)
    # XFetch: the closer to expiry and the longer the recompute, the likelier to volunteer
    if time.time() - d["delta"] * BETA * math.log(random.random()) >= d["exp"]:
        bg.submit(_bg_refresh, r())               # refresh in the background, don't block
    return time.time() - t0                        # caller returns the still-valid value now


STRATS = {
    "herd": lambda c: do_herd(c),
    "lock": lambda c: do_lock(c, crash=False),
    "lock_crash": lambda c: do_lock(c, crash=True),
    "probabilistic": lambda c: do_probabilistic(c),
}


def run_strategy(name):
    c0 = r()
    c0.delete(KEY, KEY + ":lock", KEY + ":refresh")
    fn = STRATS[name]
    samples = []            # (t_offset, latency_ms)
    lock = threading.Lock()
    start = threading.Barrier(WORKERS + 1)
    t_start = [0.0]

    def worker():
        c = r()
        start.wait()
        local = []
        while time.time() - t_start[0] < DURATION:
            lat = fn(c)
            local.append((time.time() - t_start[0], lat * 1000.0))
            time.sleep(THINK_MS / 1000.0 * random.uniform(0.5, 1.5))
        with lock:
            samples.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    start.wait()
    t_start[0] = time.time()
    for t in threads:
        t.join()
    return samples


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100.0))]


def per_second_p99(samples):
    buckets = {}
    for t, lat in samples:
        buckets.setdefault(int(t), []).append(lat)
    return {sec: pct(v, 99) for sec, v in buckets.items()}


def jitter_experiment():
    """Many keys, all recomputed once, given either a synchronized TTL or a jittered
    one. Then look at how many expire inside the same 1s window = herd size."""
    c = r()
    out = {}
    n = 300
    for mode in ("synchronized", "jittered"):
        for i in range(n):
            c.delete(f"j:{i}")
        base = time.time()
        for i in range(n):
            ttl = TTL if mode == "synchronized" else TTL * random.uniform(0.5, 1.5)
            c.set(f"j:{i}", "v", px=int(ttl * 1000))
        # bucket each key's expiry moment into 250ms windows; the fullest window is the herd
        exps = []
        for i in range(n):
            p = c.pttl(f"j:{i}")
            if p and p > 0:
                exps.append(round((time.time() + p / 1000.0 - base) / 0.25))
        w = {}
        for e in exps:
            w[e] = w.get(e, 0) + 1
        out[mode] = max(w.values()) if w else 0
    return out


def main():
    os.makedirs(RESULTS, exist_ok=True)
    r().ping()
    print(f"  redis {r().info('server')['redis_version']} | {WORKERS} workers, "
          f"{DURATION:.0f}s, recompute {RECOMPUTE_MS}ms, TTL {TTL}s")

    rows, timelines = [], {}
    for name in ("herd", "lock", "lock_crash", "probabilistic"):
        samples = run_strategy(name)
        lats = [l for _, l in samples]
        row = {"strategy": name, "requests": len(lats),
               "p50": round(pct(lats, 50), 1), "p95": round(pct(lats, 95), 1),
               "p99": round(pct(lats, 99), 1), "max": round(max(lats) if lats else 0, 1)}
        rows.append(row)
        timelines[name] = per_second_p99(samples)
        print(f"  {name:14} reqs={row['requests']:6}  p50={row['p50']:7.1f}  "
              f"p95={row['p95']:8.1f}  p99={row['p99']:8.1f}  max={row['max']:8.1f}  (ms)")

    jit = jitter_experiment()
    print(f"  jitter: synchronized peak herd={jit['synchronized']}  jittered peak={jit['jittered']}")

    with open(os.path.join(RESULTS, "latency_percentiles.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "requests", "p50", "p95", "p99", "max"])
        w.writeheader(); w.writerows(rows)
    secs = sorted({s for tl in timelines.values() for s in tl})
    with open(os.path.join(RESULTS, "p99_timeline.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_sec", "herd", "lock", "lock_crash", "probabilistic"])
        for s in secs:
            w.writerow([s] + [round(timelines[n].get(s, 0), 1)
                              for n in ("herd", "lock", "lock_crash", "probabilistic")])
    with open(os.path.join(RESULTS, "jitter.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["mode", "peak_concurrent_expiries"])
        w.writerow(["synchronized", jit["synchronized"]]); w.writerow(["jittered", jit["jittered"]])
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["redis_version", "workers", "duration_s", "recompute_ms", "ttl_s", "lock_ttl_s"])
        w.writerow([r().info('server')['redis_version'], WORKERS, DURATION, RECOMPUTE_MS, TTL, LOCK_TTL])
    print("  artifacts in results/")


if __name__ == "__main__":
    main()
