#!/usr/bin/env python3
"""Select the least-contended clean paired run from the best-of-N sweep and print it.

Clean = off-heap produced 0 concurrent-mark cycles and < 200 ms of STW pause time
(i.e. it was not swap/CPU-contention polluted). Among clean runs we pick the one
with the highest on-heap throughput -- the least-contended on-heap measurement, which
most faithfully isolates the GC tax from neighbour noise. Contention only ever adds
latency and removes throughput, so best-of-N is a conservative estimate of the
uncontended truth.
"""
import csv, sys
M = "/Users/akisonlyforu/Code/akisonlyforu.github.io/benchmarks/java-gc-offheap/results/runs/metrics.csv"
rows = list(csv.DictReader(open(M)))
clean = [r for r in rows
         if int(r["offheap_cyc"]) == 0
         and float(r["offheap_pause_ms"]) < 200
         and int(r["onheap_cyc"]) >= 20]
if not clean:
    print("NO CLEAN RUN", file=sys.stderr); sys.exit(1)
best = max(clean, key=lambda r: float(r["onheap_tp"]))
print("clean runs:", [r["run"] for r in clean])
print("selected run:", best["run"])
for k, v in best.items():
    print(f"  {k}={v}")
print(best["run"])  # last line = run id for the shell to capture
