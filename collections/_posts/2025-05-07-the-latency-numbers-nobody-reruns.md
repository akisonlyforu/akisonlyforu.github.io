---
layout:     post
title:      The Latency Numbers Nobody Reruns
date:       2025-05-07
description:    Everyone quotes "L1 1ns, main memory 100ns, SSD 16µs" from a table that's fifteen years old. I reran the whole thing on my laptop with a pointer-chase harness. Some rows moved 20x, one hasn't budged since 2012, and one got slower than the number everybody memorized.
categories: latency hardware memory performance benchmarks
---

You've seen the table. *Latency Numbers Every Programmer Should Know*: L1 cache 1ns, branch mispredict a few ns, main memory 100ns, SSD read 16µs, a network round trip 500µs. It's in every interview-prep deck, half the design docs I've ever read, and the back of my own head. I've quoted it in meetings. I have never once checked a single row of it against a machine I actually own.

So I did. The whole table, on the laptop I'm typing this on, an Apple M4.

## The problem

Those numbers trace back to a slide Jeff Dean gave around 2010, popularized and updated a few times since. Call it 2012 vintage. Hardware has not moved uniformly since then. Some rows are off by 20x. One hasn't moved at all. And one is *slower* than the figure everyone memorized. The trouble is you can't tell which is which by looking. The table reads like a set of physical constants, so people quote a 2012 SSD number in a 2026 capacity estimate and the error hides inside a spreadsheet.

The only way to know which rows rotted is to rerun them. That has to happen natively. The whole point is the host's real memory hierarchy and SSD, and a Linux VM on macOS would put a translation layer between me and the DRAM and quietly lie about every number. So no Docker here, just clang and the bare machine.

## The memory ladder

The trick behind the whole table is one benchmark: chase a pointer through an array, and grow the array until the latency jumps. Each jump is a cache boundary. The catch is you have to defeat the prefetcher: walk the array in a *random* cycle, not in order, so the CPU can't guess the next address and pre-load it. And each read has to depend on the last one, or the loads run in parallel and you measure throughput instead of latency:

```c
/* arr is a random permutation cycle: arr[i] points to the next slot */
uint64_t t0 = now_ns();
idx = 0;
for (uint64_t i = 0; i < iters; i++) {
    idx = arr[idx];        /* each load's address is the previous load's value */
}
uint64_t t1 = now_ns();
```

That `idx = arr[idx]` is the entire experiment. The address of the next read is whatever the last read returned, so the CPU can't run ahead, and the random cycle means the prefetcher is useless. Grow the array from 4KB to 256MB and time each size:

<figure class="cache-bench">
<style>
.cache-bench {
  --cb-bg: #f7f9fb;
  --cb-text: #333333;
  --cb-muted: #666666;
  --cb-grid: rgba(0, 0, 0, 0.12);
  --cb-blue: #0076df;
  --cb-orange: #d65f3c;
  --cb-green: #23856d;
  --cb-purple: #7b5bb5;
  margin: 1.8rem 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--cb-grid);
  border-radius: 8px;
  background: var(--cb-bg);
  color: var(--cb-text);
}
.cache-bench h3 { margin: 0 0 1rem; color: var(--cb-text); font-size: 1rem; }
.cache-bench figcaption { margin-top: 0.9rem; color: var(--cb-muted); font-size: 0.82rem; line-height: 1.45; }
.cb-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.cb-panel-title { margin: 0 0 0.55rem; color: var(--cb-muted); font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.cb-bar-row { display: grid; grid-template-columns: minmax(6.5rem, 1.2fr) minmax(7rem, 4fr) minmax(3.8rem, 0.8fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .p999 { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .p50 { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
.cb-legend { display: flex; gap: 1rem; margin-top: 0.5rem; color: var(--cb-muted); font-size: 0.78rem; }
.cb-swatch { width: 0.8rem; height: 0.22rem; margin-right: 0.3rem; display: inline-block; vertical-align: middle; background: var(--swatch); }
@media (prefers-color-scheme: dark) {
  .cache-bench {
    --cb-bg: #252525;
    --cb-text: #e0e0e0;
    --cb-muted: #b0b0b0;
    --cb-grid: rgba(255, 255, 255, 0.14);
    --cb-blue: #4dabf7;
    --cb-orange: #ff8a65;
    --cb-green: #51cf66;
    --cb-purple: #b197fc;
  }
}
:root[data-theme="dark"] .cache-bench {
  --cb-bg: #252525;
  --cb-text: #e0e0e0;
  --cb-muted: #b0b0b0;
  --cb-grid: rgba(255, 255, 255, 0.14);
  --cb-blue: #4dabf7;
  --cb-orange: #ff8a65;
  --cb-green: #51cf66;
  --cb-purple: #b197fc;
}
@media (max-width: 620px) {
  .cb-panels { grid-template-columns: 1fr; }
  .cb-bar-row { grid-template-columns: minmax(6rem, 1.3fr) minmax(5rem, 3fr) minmax(3.6rem, 0.8fr); gap: 0.4rem; }
}
</style>
<h3>Latency per access vs working-set size (log-log)</h3>
<svg class="cb-svg" viewBox="0 0 720 300" role="img" aria-label="Memory access latency in nanoseconds against working-set size from 4 kilobytes to 256 megabytes, log-log axes, showing an L1 plateau, an L2 ramp, and a DRAM plateau">
  <line class="grid" x1="48" y1="254.4" x2="706" y2="254.4"/>
  <line class="grid" x1="48" y1="134.2" x2="706" y2="134.2"/>
  <line class="grid" x1="48" y1="14" x2="706" y2="14"/>
  <text x="42" y="258" text-anchor="end">1ns</text>
  <text x="42" y="138" text-anchor="end">10ns</text>
  <text x="42" y="18" text-anchor="end">100ns</text>
  <text x="48" y="284" text-anchor="middle">4KB</text>
  <text x="212.5" y="284" text-anchor="middle">64KB</text>
  <text x="377" y="284" text-anchor="middle">1MB</text>
  <text x="541.5" y="284" text-anchor="middle">16MB</text>
  <text x="706" y="284" text-anchor="end">256MB</text>
  <polyline class="p50" points="48.0,258.4 89.1,258.4 130.2,258.3 171.4,256.9 212.5,258.2 253.6,257.6 294.8,191.0 335.9,177.0 377.0,170.8 418.1,167.0 459.2,154.1 500.4,147.5 541.5,117.7 565.6,66.4 582.6,44.2 606.7,35.5 623.8,28.7 664.9,20.7 706.0,17.9"/>
  <circle cx="130.2" cy="258.3" r="3.5" fill="var(--cb-blue)"/>
  <circle cx="541.5" cy="117.7" r="3.5" fill="var(--cb-blue)"/>
  <circle cx="706" cy="17.9" r="3.5" fill="var(--cb-blue)"/>
  <text x="56" y="248">L1 · 0.93ns, flat to 128KB</text>
  <text x="330" y="140">L2 · rising to 16MB</text>
  <text x="700" y="34" text-anchor="end">DRAM · 93ns</text>
</svg>
<figcaption>Random dependent pointer chase, median of 5 trials, 60M to 300M accesses per point. The line sits flat at 0.93ns out to 128KB, steps up at 256KB, climbs through the L2 to ~14ns at 16MB, then ramps to a 93ns plateau in DRAM. Measured on Apple M4 (macOS 15.7.3, clang 17), results in benchmarks/latency-numbers/results/mem_latency.csv.</figcaption>
</figure>

The nice part: I never told the harness anything about the chip. The line drew the M4's cache sizes on its own: 0.93ns flat out to exactly 128KB, which is the P-core's L1 data cache, then a long climb that flattens near 16MB, which is the P-core's L2. The hardware spec sheet is sitting right there in the shape of the curve.

And the number that matters most: **main memory came in at 93ns.** The 2012 table says 100ns. Fifteen years, and DRAM access latency got about 7% faster. That's the wall everyone talks about: the cores got wider, the caches got bigger, the SSDs got an order of magnitude quicker, and the trip out to a DRAM row is still ~100ns because that's set by physics and the memory bus, not by transistor count. If you trust one row of the old table for the next decade, trust that one.

## Sequential versus random

The ladder above is the random case, on purpose. Walk the same buffer *in order* and it's a different machine, because now the prefetcher can see you coming:

<figure class="cache-bench">
<h3>256MB buffer, 64-byte cache lines</h3>
<div class="cb-bar-row"><span>sequential</span><span class="cb-track"><span class="cb-fill" style="--value:27.8%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.97 ns/line</span></div>
<div class="cb-bar-row"><span>random</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3.49 ns/line</span></div>
<figcaption>Time per 64-byte cache line touched, median of 3 trials, buffer far larger than the L2. Sequential is 3.6× faster per line, same DRAM, but the prefetcher hides the latency when it can predict the stride. Results in benchmarks/latency-numbers/results/seq_vs_random.csv.</figcaption>
</figure>

3.6x, from nothing but access order. This is why "read 1MB sequentially" is even a row in the table. Sequential and random aren't the same operation slowed down, they're two different costs, and the gap is the prefetcher doing its job.

## The whole table, remeasured

Here's every row I could measure locally, laid next to the canonical 2012 figure. Both bars are on the same log scale, so a longer bar is genuinely slower:

<figure class="cache-bench">
<h3>Canonical latencies: Apple M4 (2026) vs the 2012 table</h3>
<div class="cb-panels">
<div>
<p class="cb-panel-title">Measured: Apple M4, 2026</p>
<div class="cb-bar-row"><span>L1 reference</span><span class="cb-track"><span class="cb-fill" style="--value:4.3%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.93 ns</span></div>
<div class="cb-bar-row"><span>Branch mispredict</span><span class="cb-track"><span class="cb-fill" style="--value:15.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">4.77 ns</span></div>
<div class="cb-bar-row"><span>Mutex lock/unlock</span><span class="cb-track"><span class="cb-fill" style="--value:14.6%;--bar:var(--cb-blue)"></span></span><span class="cb-value">4.18 ns</span></div>
<div class="cb-bar-row"><span>Main memory</span><span class="cb-track"><span class="cb-fill" style="--value:36%;--bar:var(--cb-blue)"></span></span><span class="cb-value">93 ns</span></div>
<div class="cb-bar-row"><span>Compress 1KB</span><span class="cb-track"><span class="cb-fill" style="--value:66.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">7.7 µs</span></div>
<div class="cb-bar-row"><span>SSD random 4KB</span><span class="cb-track"><span class="cb-fill" style="--value:79.9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">54 µs</span></div>
<div class="cb-bar-row"><span>1MB seq from RAM</span><span class="cb-track"><span class="cb-fill" style="--value:79.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">49 µs</span></div>
<div class="cb-bar-row"><span>1MB seq from SSD</span><span class="cb-track"><span class="cb-fill" style="--value:79.9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">54 µs</span></div>
<div class="cb-bar-row"><span>Loopback RTT</span><span class="cb-track"><span class="cb-fill" style="--value:70.1%;--bar:var(--cb-blue)"></span></span><span class="cb-value">13 µs</span></div>
</div>
<div>
<p class="cb-panel-title">Canonical: 2012 table</p>
<div class="cb-bar-row"><span>L1 reference</span><span class="cb-track"><span class="cb-fill" style="--value:4.8%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1 ns</span></div>
<div class="cb-bar-row"><span>Branch mispredict</span><span class="cb-track"><span class="cb-fill" style="--value:12.3%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3 ns</span></div>
<div class="cb-bar-row"><span>Mutex lock/unlock</span><span class="cb-track"><span class="cb-fill" style="--value:24.3%;--bar:var(--cb-orange)"></span></span><span class="cb-value">17 ns</span></div>
<div class="cb-bar-row"><span>Main memory</span><span class="cb-track"><span class="cb-fill" style="--value:36.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">100 ns</span></div>
<div class="cb-bar-row"><span>Compress 1KB</span><span class="cb-track"><span class="cb-fill" style="--value:60%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3 µs</span></div>
<div class="cb-bar-row"><span>SSD random 4KB</span><span class="cb-track"><span class="cb-fill" style="--value:71.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">16 µs</span></div>
<div class="cb-bar-row"><span>1MB seq from RAM</span><span class="cb-track"><span class="cb-fill" style="--value:90.4%;--bar:var(--cb-orange)"></span></span><span class="cb-value">250 µs</span></div>
<div class="cb-bar-row"><span>1MB seq from SSD</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1 ms</span></div>
<div class="cb-bar-row"><span>Loopback RTT</span><span class="cb-track"><span class="cb-fill" style="--value:95.2%;--bar:var(--cb-orange)"></span></span><span class="cb-value">500 µs</span></div>
</div>
</div>
<figcaption>Bars share a log scale from 0.5ns to 1ms. Measured column is the median of many trials on Apple M4; canonical column is the 2012 figure ("main memory 100ns", "read 1MB from SSD 1ms", and so on). Full numbers in benchmarks/latency-numbers/results/canonical_table.csv.</figcaption>
</figure>

Reading down the list, the rows split into three stories.

Some rows moved because the hardware genuinely got better. Mutex lock/unlock went from 17ns to 4.18ns. Uncontended locking is nearly free now. Reading 1MB sequentially from SSD went from 1ms to 54µs, roughly 18x, which is NVMe earning its keep over the SATA SSD the old number assumed.

One row moved for a reason that has nothing to do with what it's named. "Read 1MB sequentially from memory" dropped from 250µs to 49µs, about 5x. That looks like RAM got five times faster, except we just measured that it didn't, DRAM latency barely moved. What actually happened is the L2 grew to 16MB, so 1MB now fits comfortably in cache and the read mostly never touches DRAM at all. The number moved because the caches grew *around* it. Quote it as "memory got faster" and you've learned the wrong lesson.

And a couple got worse, or were never a fair comparison to begin with. SSD random 4KB read came in at 54µs against the canonical 16µs, slower than the number everyone recites. The old 16µs was an optimistic enterprise-SSD figure; a real 4KB random read on this machine, with the OS page cache bypassed so it actually hits the device, is 54µs through the full storage stack. And notice random-4K and sequential-1MB both landed at ~54µs. At these sizes you're paying for the syscall and the device round trip, not for the bytes.

Two rows I'm flagging rather than trusting. Compress 1KB shows 7.7µs, but I ran zlib and the old table measured Google's Zippy (Snappy), which trades ratio for speed, different algorithm, not a fair fight, so I'm not claiming compression got slower. And the 13µs "loopback RTT" is my kernel talking to itself over TCP, not a real datacenter hop; a genuine same-datacenter round trip is still ~500µs because that's wire and switches, not memcpy. I left both in as floors, clearly labelled, not as matches.

One more bit of honesty. The DRAM middle of the ladder jittered on the first run because the scheduler kept bouncing the benchmark between the M4's performance and efficiency cores. That run is saved under `results/attempts/`. And my first branch-mispredict test measured a flat 0ns, because clang looked at my careful branch and quietly compiled it to branchless code. Ask me how I know. The 4.77ns figure is from a version rewritten to force a real, mispredictable branch.

## The takeaway

Memorize the shape, not the digits. The orders of magnitude and the gaps between them are the durable part: L1 is about 100x faster than DRAM, DRAM is a few hundred times faster than an SSD, an SSD is roughly 10x faster than a network hop. Those ratios are the intuition you actually reach for in a design review, and they've held.

The specific microseconds rot, and they rot unevenly. Cache sizes grow and silently move rows that look like they're about memory. SSDs get faster in sequential and can look slower in random once you stop measuring the page cache. And exactly one number, main memory at ~100ns, hasn't moved in fifteen years and probably won't in the next fifteen. So the next time you're about to anchor a real capacity number on a row from that table, spend the four minutes to run it on the machine you'll actually deploy on. Mine disagreed with the famous version on more rows than it agreed with.

The harness (the pointer chase, the SSD reads with the cache bypassed, and the full table) is here: [benchmarks/latency-numbers](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/latency-numbers). These are laptop numbers meant to build intuition, not capacity-planning figures for your fleet, but the machine on your desk is the one you can actually measure, so measure it.
