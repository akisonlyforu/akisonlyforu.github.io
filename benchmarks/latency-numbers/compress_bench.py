#!/usr/bin/env python3
"""
compress_bench.py — "Compress 1KB" row of the latency table.

Generates ~1KB of realistic-ish, compressible-but-not-trivial text, compresses
it many times with zlib (stdlib), and reports the median wall-clock ns for one
1KB compression. Writes results/compress.csv.

Usage: python3 compress_bench.py <results_dir>
"""
import sys
import os
import time
import zlib
import random

SAMPLE = (
    "The quick brown fox jumps over the lazy dog. "
    "Latency numbers every programmer should know, remeasured on real hardware. "
    "Memory hierarchy: registers, L1, L2, shared last-level cache, DRAM, SSD, network. "
    "Prefetchers love sequential access and hate random pointer chasing. "
    "Amdahl reminds us that the fast path only matters if it is the common path. "
)


def make_payload(target_bytes=1024, seed=42):
    """Build ~target_bytes of text: repeated sample with light word-level
    scrambling so it compresses meaningfully but not to near-nothing."""
    rng = random.Random(seed)
    words = SAMPLE.split()
    out = []
    size = 0
    while size < target_bytes:
        chunk = words[:]
        # lightly shuffle a subset so it's not a pure repeat
        if rng.random() < 0.5:
            i = rng.randrange(len(chunk))
            j = rng.randrange(len(chunk))
            chunk[i], chunk[j] = chunk[j], chunk[i]
        piece = " ".join(chunk) + " "
        out.append(piece)
        size += len(piece)
    data = "".join(out).encode("utf-8")[:target_bytes]
    return data


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "./results"
    os.makedirs(results_dir, exist_ok=True)

    payload = make_payload(1024)
    n = len(payload)

    # warm up
    for _ in range(1000):
        zlib.compress(payload, 6)

    ratio = n / len(zlib.compress(payload, 6))

    # Time individual compressions; take median of many trials.
    trials = 20000
    samples = []
    for _ in range(trials):
        t0 = time.perf_counter_ns()
        zlib.compress(payload, 6)
        t1 = time.perf_counter_ns()
        samples.append(t1 - t0)
    samples.sort()
    median_ns = samples[len(samples) // 2]

    csv_path = os.path.join(results_dir, "compress.csv")
    with open(csv_path, "w") as f:
        f.write("payload_bytes,median_ns,ratio\n")
        f.write(f"{n},{median_ns},{ratio:.3f}\n")

    print(f"[compress] {n} bytes, median {median_ns} ns, ratio {ratio:.2f}x -> {csv_path}")
    return median_ns


if __name__ == "__main__":
    main()
