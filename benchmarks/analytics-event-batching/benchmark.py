"""Measure the batching + compression layer of a high-scale product-analytics
event pipeline. A single analytics event barely compresses, but the events share
a schema, so once you glue many of them into one batch the redundancy across
events is what the compressor eats -- bytes-per-event collapses and the pipeline
moves billions of events/day cheaply.

Four experiments, all pure Python, no server engine, deterministic under SEED:
  A. Per-event amortization - bytes/event after compression vs batch size.
  B. Compression ratio vs batch size - the headline curve, with stdev.
  C. Codec + level shootout at batch 500 - ratio, compress/decompress ms, MB/s.
  D. At-least-once duplicates & dedup - retry probability -> duplicate rate,
     and dedup by event_id recovering exactly N.

Env:
  RESULTS_DIR  output dir (default ./results)
  SEED         RNG seed (default 42)
  N_EVENTS     size of the event pool (default 40000, runs in <60s)

Timing uses time.perf_counter only; nothing in the DATA path calls time.time()
or unseeded random.
"""
import csv
import gzip
import json
import os
import platform
import random
import statistics
import sys
import time

import zstandard as zstd

SEED = int(os.environ.get("SEED", "42"))
N_EVENTS = int(os.environ.get("N_EVENTS", "40000"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

BATCH_SIZES = [1, 5, 10, 25, 50, 100, 250, 500, 1000]

EVENT_NAMES = [
    "page_viewed", "button_clicked", "design_created", "export_started",
    "share_clicked", "template_opened", "element_added", "text_edited",
    "image_uploaded", "comment_added", "project_saved", "download_completed",
]
PAGES = ["/", "/home", "/editor", "/templates", "/pricing", "/settings",
         "/projects", "/share", "/export", "/account"]
REFERRERS = ["", "https://www.google.com/", "https://t.co/",
             "https://www.facebook.com/", "direct", "https://www.reddit.com/"]
DEVICES = ["desktop", "mobile", "tablet"]
COUNTRIES = ["US", "IN", "GB", "DE", "BR", "CA", "AU", "JP"]
AB_VARIANTS = ["A", "B", "C"]


def gen_events(n, seed):
    """Deterministic pool of realistic product-analytics events. ts is derived
    from the index (monotonically increasing), never from the wall clock."""
    rnd = random.Random(seed)
    base_ts = 1_700_000_000_000  # fixed epoch-ms base, not time.time()
    events = []
    for i in range(n):
        # monotonically increasing ts: base + i*step + small deterministic jitter
        ts = base_ts + i * 40 + rnd.randint(0, 39)
        e = {
            "event_id": "%032x" % rnd.getrandbits(128),  # uuid-like, for dedup
            "event": rnd.choice(EVENT_NAMES),
            "user_id": "u_" + "%016x" % rnd.getrandbits(64),
            "session_id": "s_" + "%012x" % rnd.getrandbits(48),
            "ts": ts,
            "props": {
                "page": rnd.choice(PAGES),
                "referrer": rnd.choice(REFERRERS),
                "device": rnd.choice(DEVICES),
                "country": rnd.choice(COUNTRIES),
                "ab_variant": rnd.choice(AB_VARIANTS),
                "duration_ms": rnd.randint(50, 30000),
                "position": rnd.randint(0, 1200),
            },
        }
        events.append(e)
    return events


def ser(e):
    return json.dumps(e, separators=(",", ":"))


def make_batches(lines, batch_size, max_batches):
    """Slice the serialized-line pool into newline-joined batches (bytes)."""
    batches = []
    n = len(lines)
    for b in range(max_batches):
        start = (b * batch_size) % n
        chunk = []
        for k in range(batch_size):
            chunk.append(lines[(start + k) % n])
        batches.append("\n".join(chunk).encode("utf-8"))
    return batches


def experiment_a(lines, raw_mean):
    print("\n" + "=" * 64)
    print("EXPERIMENT A  Per-event amortization (bytes/event vs batch size)")
    print("=" * 64)
    cctx = zstd.ZstdCompressor(level=3)
    rows = []
    for bs in BATCH_SIZES:
        n_batches = max(50, min(2000, N_EVENTS // bs))
        batches = make_batches(lines, bs, n_batches)
        comp_per_event = []
        raw_per_event = []
        for raw in batches:
            comp = cctx.compress(raw)
            comp_per_event.append(len(comp) / bs)
            raw_per_event.append(len(raw) / bs)
        rbpe = statistics.mean(raw_per_event)
        cbpe = statistics.mean(comp_per_event)
        rows.append({
            "batch_size": bs,
            "raw_bytes_per_event": round(rbpe, 2),
            "compressed_bytes_per_event": round(cbpe, 2),
            "ratio": round(rbpe / cbpe, 3),
        })
        print(f"  batch={bs:>4}  raw/ev={rbpe:7.1f}B  comp/ev={cbpe:7.1f}B  ratio={rbpe/cbpe:5.2f}x")
    _write_csv("a_amortization.csv",
               ["batch_size", "raw_bytes_per_event", "compressed_bytes_per_event", "ratio"], rows)
    return rows


def experiment_b(lines):
    print("\n" + "=" * 64)
    print("EXPERIMENT B  Compression ratio vs batch size")
    print("=" * 64)
    cctx = zstd.ZstdCompressor(level=3)
    rows = []
    for bs in BATCH_SIZES:
        n_batches = max(50, min(2000, N_EVENTS // bs))
        batches = make_batches(lines, bs, n_batches)
        ratios = []
        for raw in batches:
            comp = cctx.compress(raw)
            ratios.append(len(raw) / len(comp))
        rmean = statistics.mean(ratios)
        rstd = statistics.stdev(ratios) if len(ratios) > 1 else 0.0
        rows.append({
            "batch_size": bs,
            "ratio_mean": round(rmean, 3),
            "ratio_stdev": round(rstd, 3),
            "n_batches": len(ratios),
        })
        print(f"  batch={bs:>4}  ratio={rmean:6.2f}x  +/-{rstd:5.2f}  (n={len(ratios)})")
    _write_csv("b_ratio_vs_batchsize.csv",
               ["batch_size", "ratio_mean", "ratio_stdev", "n_batches"], rows)
    return rows


def _zstd_codec(level):
    cctx = zstd.ZstdCompressor(level=level)
    dctx = zstd.ZstdDecompressor()
    return cctx.compress, dctx.decompress


def experiment_c(lines):
    print("\n" + "=" * 64)
    print("EXPERIMENT C  Codec + level shootout at batch size 500")
    print("=" * 64)
    BS = 500
    n_batches = 240  # enough for stable timing
    batches = make_batches(lines, BS, n_batches)

    zc3, zd3 = _zstd_codec(3)
    zc9, zd9 = _zstd_codec(9)
    zc19, zd19 = _zstd_codec(19)

    codecs = [
        ("none",      lambda b: b,                    lambda b: b),
        ("gzip-6",    lambda b: gzip.compress(b, 6),  gzip.decompress),
        ("zstd-3",    zc3,                             zd3),
        ("zstd-9",    zc9,                             zd9),
        ("zstd-19",   zc19,                            zd19),
    ]

    rows = []
    for name, comp_fn, decomp_fn in codecs:
        # warm up
        for raw in batches[:20]:
            decomp_fn(comp_fn(raw))

        raw_total = 0
        comp_total = 0
        comp_times = []
        decomp_times = []
        for raw in batches:
            t0 = time.perf_counter()
            c = comp_fn(raw)
            t1 = time.perf_counter()
            d = decomp_fn(c)
            t2 = time.perf_counter()
            assert d == raw
            raw_total += len(raw)
            comp_total += len(c)
            comp_times.append((t1 - t0) * 1000.0)
            decomp_times.append((t2 - t1) * 1000.0)

        ratio = raw_total / comp_total
        mean_comp_ms = statistics.mean(comp_times)
        mean_decomp_ms = statistics.mean(decomp_times)
        mean_batch_bytes = raw_total / len(batches)
        # throughput = input MB / compress seconds
        throughput = (mean_batch_bytes / 1e6) / (mean_comp_ms / 1000.0) if mean_comp_ms > 0 else 0.0
        rows.append({
            "codec": name,
            "ratio": round(ratio, 3),
            "compress_ms": round(mean_comp_ms, 4),
            "decompress_ms": round(mean_decomp_ms, 4),
            "throughput_mb_s": round(throughput, 1),
            "n_batches": len(batches),
        })
        print(f"  {name:9}  ratio={ratio:6.2f}x  comp={mean_comp_ms:8.3f}ms  "
              f"decomp={mean_decomp_ms:7.3f}ms  {throughput:8.1f} MB/s")
    _write_csv("c_codec_shootout.csv",
               ["codec", "ratio", "compress_ms", "decompress_ms", "throughput_mb_s", "n_batches"], rows)
    return rows


def experiment_d(events, seed):
    print("\n" + "=" * 64)
    print("EXPERIMENT D  At-least-once duplicates & dedup")
    print("=" * 64)
    n = len(events)
    rows = []
    for p in [0.005, 0.01, 0.02, 0.05]:
        rnd = random.Random(seed + int(p * 100000))
        delivered = []  # list of event_ids as seen by the consumer
        for e in events:
            delivered.append(e["event_id"])
            # ambiguous ack -> client retries -> duplicate at consumer.
            # a retry can itself be ambiguous, so model geometric extra copies.
            while rnd.random() < p:
                delivered.append(e["event_id"])
        delivered_count = len(delivered)
        duplicates = delivered_count - n
        unique_after = len(set(delivered))
        dup_rate = duplicates / delivered_count if delivered_count else 0.0
        rows.append({
            "retry_p": p,
            "n_events": n,
            "delivered": delivered_count,
            "duplicates": duplicates,
            "duplicate_rate": round(dup_rate, 5),
            "unique_after_dedup": unique_after,
        })
        ok = "OK" if unique_after == n else "MISMATCH"
        print(f"  p={p:<6}  delivered={delivered_count:>7}  dups={duplicates:>6}  "
              f"dup_rate={dup_rate*100:5.2f}%  unique_after_dedup={unique_after} [{ok}]")
    _write_csv("d_atleastonce_dedup.csv",
               ["retry_p", "n_events", "delivered", "duplicates", "duplicate_rate", "unique_after_dedup"], rows)
    return rows


def _write_csv(name, fields, rows):
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_summary(raw_mean, a, b, c, d):
    bmap = {r["batch_size"]: r for r in b}
    amap = {r["batch_size"]: r for r in a}
    lines = []
    lines.append("Analytics event batching + compression -- benchmark summary")
    lines.append("=" * 60)
    lines.append(f"seed={SEED}  n_events={N_EVENTS}  mean raw event size={raw_mean:.1f} bytes")
    lines.append("")
    lines.append("A. Per-event amortization (zstd-3):")
    lines.append(f"   batch=1     comp/event = {amap[1]['compressed_bytes_per_event']} B")
    lines.append(f"   batch=1000  comp/event = {amap[1000]['compressed_bytes_per_event']} B")
    lines.append(f"   raw bytes/event ~ {amap[1]['raw_bytes_per_event']} B (single-event batch)")
    lines.append("")
    lines.append("B. Compression ratio vs batch size (zstd-3):")
    for bs in [1, 10, 100, 1000]:
        r = bmap[bs]
        lines.append(f"   batch={bs:>4}  ratio={r['ratio_mean']}x  +/-{r['ratio_stdev']}  (n={r['n_batches']})")
    lines.append("")
    lines.append("C. Codec + level shootout at batch 500:")
    lines.append(f"   {'codec':9} {'ratio':>7} {'comp_ms':>10} {'decomp_ms':>11} {'MB/s':>9}")
    for r in c:
        lines.append(f"   {r['codec']:9} {r['ratio']:>6}x {r['compress_ms']:>10} "
                     f"{r['decompress_ms']:>11} {r['throughput_mb_s']:>9}")
    lines.append("")
    lines.append("D. At-least-once duplicates & dedup:")
    for r in d:
        lines.append(f"   p={r['retry_p']:<6} dup_rate={r['duplicate_rate']*100:.2f}%  "
                     f"duplicates={r['duplicates']}  unique_after_dedup={r['unique_after_dedup']} "
                     f"(N={r['n_events']})")
    lines.append("")
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def write_metadata(raw_mean):
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["python_version", "zstandard_version", "platform",
                    "seed", "n_events", "batch_sizes", "mean_raw_event_bytes"])
        w.writerow([sys.version.split()[0], zstd.__version__, platform.platform(),
                    SEED, N_EVENTS, " ".join(str(x) for x in BATCH_SIZES), round(raw_mean, 2)])


def main():
    os.makedirs(RESULTS, exist_ok=True)
    t_start = time.perf_counter()
    print(f"Generating {N_EVENTS} events (seed={SEED}) ...")
    events = gen_events(N_EVENTS, SEED)
    lines = [ser(e) for e in events]
    raw_mean = statistics.mean(len(l.encode("utf-8")) for l in lines)
    print(f"  mean raw event size: {raw_mean:.1f} bytes (compact JSON)")

    a = experiment_a(lines, raw_mean)
    b = experiment_b(lines)
    c = experiment_c(lines)
    d = experiment_d(events, SEED)

    write_summary(raw_mean, a, b, c, d)
    write_metadata(raw_mean)
    print(f"\n  py {sys.version.split()[0]} | zstandard {zstd.__version__} | "
          f"artifacts in results/ | {time.perf_counter() - t_start:.1f}s")


if __name__ == "__main__":
    main()
