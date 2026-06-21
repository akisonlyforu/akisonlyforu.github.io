---
layout:     post
title:      The Objects That Never Died
date:       2026-07-18
description:    A merged service ran hotter for the same traffic. p50 and p99 were fine, but the GC threads were pinned. G1 was spending its afternoons re-marking a heap that was half long-lived cache, proving over and over that objects which were never going to die were still alive. Moving them off-heap cut total GC time 97.8% and lifted throughput 30%, and the latency cost I braced for never showed up.
categories: java gc g1gc jvm performance offheap
---

We merged two services into one process to save a network hop. On paper it was free throughput. In practice the box ran hotter for the exact same traffic — p50 flat, p99 flat, and the GC threads pinned at the top of `top` like they had somewhere to be. Nothing was slower. Everything was more expensive.

What I'd actually done was double the long-lived cache. Each service carried a big map of models it kept for the life of the process, and now one heap held both. G1 noticed. It spent the afternoon walking objects that were never going to die, marking them alive, reclaiming almost nothing, and starting over.

## The problem

A large population of long-lived objects is not a quiet tenant. G1 kicks off a concurrent mark cycle whenever old-gen occupancy crosses the IHOP threshold, and marking has to scan the live set to decide what's collectable. If most of the old generation is a cache that never dies, every cycle reads the same objects, frees almost nothing, and occupancy is right back over the threshold within a few young collections, so it marks again. You're burning CPU to keep re-proving the same objects are still alive. A bigger heap delays it, a smaller heap makes it worse, and neither one removes the work. The only real fix is to get those objects out of GC's line of sight.

## What concurrent marking is actually counting

G1 splits the heap into regions and collects young ones on every pause. Old regions are different — before it can reclaim any, it has to know which objects in them are still reachable, and that's what a concurrent mark cycle does. It runs mostly off the application threads, which is why your pause times can look healthy while a whole core disappears into `gc`.

The trigger is occupancy. By default G1 adapts the threshold, but the shape is the same: once the heap is X% full it starts marking. The cost of a mark cycle scales with the live set, not the garbage, and that's what bit me. A heap that's 60% permanent cache gives you the most expensive possible marking: huge live set to walk, almost nothing to collect at the end of it. And because the cache doesn't shrink, you cross the threshold again almost immediately. Marking becomes a treadmill.

## The setup

I wanted to reproduce the treadmill and then step off it, on one laptop, with numbers I could point at. The harness compiles one small Java program and runs it two ways under an identical workload.

- **On-heap.** Build a long-lived `byte[][]` of 3,500,000 live `byte[]` objects, each a 192-byte payload — about 672 MB of live arrays that I hold a strong reference to for the whole run, so nothing ever gets collected. With object headers the live floor settles near 737 MB, about half the 1500 MB heap, before the workload even starts.
- **Off-heap.** The same 3,500,000 payloads, same bytes, live in a single `ByteBuffer.allocateDirect` slab addressed by computed offset. The heap holds the index arithmetic and nothing else.

Both then run the same 40,000,000-iteration loop: allocate a chunk of short-lived garbage to push on the collector, then look up a random key — an `arr[key]` dereference on-heap, a bounds-checked slab read on off-heap — with a deterministic key sequence and a checksum folded from the payload bytes at the end so the JIT can't quietly delete the work. Heap pinned at `-Xmx1500m -Xms1500m`, G1 explicit, IHOP fixed at 40% so the trigger point is the same every run. Same seed, same iteration count, same everything except where the long-lived bytes live.

You can watch the difference in a single line of the GC log. On-heap, a young pause looks like this:

```
GC(9) Pause Young (Concurrent Start) (G1 Evacuation Pause) 693M->694M(1500M) 9.900ms
```

693 MB before, 694 MB after. The collection freed nothing and occupancy went *up*, because the long-lived cache is most of the heap and it isn't going anywhere — this is the pause that crosses 40% and kicks off a mark cycle. Off-heap, the same workload's young pauses read like this:

```
GC(0) Pause Young (Normal) (G1 Evacuation Pause) 92M->17M(1500M) 2.089ms
```

92 MB down to 17. All young garbage, all reclaimed, old generation basically empty. Nothing to mark, so nothing marks.

## What the collector did

Across the run, on-heap G1 ran 34 concurrent mark cycles and 15 mixed collections to chase the old regions those cycles found. Off-heap ran zero of each — just plain young pauses. Here's where the time went:

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
.cb-bar-row { display: grid; grid-template-columns: minmax(7.5rem, 1.3fr) minmax(6rem, 4fr) minmax(4.6rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.4rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
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
</style>

<figure class="cache-bench">
  <h3>Where the GC time went (total 5,216 ms on-heap vs 114 ms off-heap)</h3>
  <div class="cb-bar-row"><span>on-heap: concurrent mark</span><span class="cb-track"><span class="cb-fill" style="--value:95.2%;--bar:var(--cb-orange)"></span></span><span class="cb-value">4,968 ms</span></div>
  <div class="cb-bar-row"><span>on-heap: STW pauses</span><span class="cb-track"><span class="cb-fill" style="--value:4.8%;--bar:var(--cb-blue)"></span></span><span class="cb-value">248 ms</span></div>
  <div class="cb-bar-row"><span>off-heap: concurrent mark</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0 ms</span></div>
  <div class="cb-bar-row"><span>off-heap: STW pauses</span><span class="cb-track"><span class="cb-fill" style="--value:2.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">114 ms</span></div>
  <figcaption>Bars are to scale against on-heap total GC time, 5,216.129 ms. On-heap, 95% of the collector's time was concurrent marking — work the pause charts never show. Off-heap did zero marking and its stop-the-world pauses alone (114.328 ms) were less than half the on-heap pause time (247.840 ms). Measured on OpenJDK 21.0.11, G1GC, results in benchmarks/java-gc-offheap/results/.</figcaption>
</figure>

The pause time barely moved the story — 248 ms versus 114 ms, both small, both the kind of number you'd sign off on in a dashboard. The 4,968 ms of concurrent marking is the part that doesn't show up as a pause and doesn't show up in p99, it just quietly eats a core. That's why the merged service looked fine on every latency graph and still ran hot.

<figure class="cache-bench">
  <h3>Concurrent mark cycles over the run</h3>
  <div class="cb-bar-row"><span>on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">34</span></div>
  <div class="cb-bar-row"><span>off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
  <figcaption>34 cycles versus none, same workload, same heap, same seed. Each on-heap cycle scanned a live set that was mostly the 672 MB cache, freed almost nothing, and let occupancy climb back over 40% within a few young collections — one mixed pause shows it plainly, 1365M-&gt;737M, dropping the young garbage and settling right back onto the long-lived floor. Measured on OpenJDK 21.0.11, results in benchmarks/java-gc-offheap/results/.</figcaption>
</figure>

## The fix, and what it bought

Moving the cache off-heap doesn't make it cheaper to store or faster to reach in principle — it just makes it invisible to the collector. The old generation goes empty, occupancy never crosses the threshold, and the whole marking treadmill stops. Total GC time fell from 5,216.129 ms to 114.328 ms, a 97.8% cut, and all of the concurrent marking went with it. The workload finished in 10.5 seconds instead of 13.7, which showed up as throughput:

<figure class="cache-bench">
  <h3>Workload throughput (40,000,000 iterations)</h3>
  <div class="cb-bar-row"><span>on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:76.9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2,929,818/s</span></div>
  <div class="cb-bar-row"><span>off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">3,808,946/s</span></div>
  <figcaption>Same 40,000,000-iteration loop both ways. Off-heap ran 30.0% more operations per second, and the difference is almost exactly the CPU that on-heap was handing to the concurrent marker. Measured on OpenJDK 21.0.11, results in benchmarks/java-gc-offheap/results/.</figcaption>
</figure>

## The cost that didn't show up

I went in braced for the trade every off-heap writeup warns you about: you move the data out of the heap and every read now pays to fetch it back — a copy, a deserialize, a bounds check. The story is always "you traded GC pause for lookup latency." So I measured the lookup path directly, two million samples each way, and waited for the off-heap numbers to be worse.

They weren't.

<figure class="cache-bench">
  <h3>Per-lookup latency, on-heap vs off-heap (nanoseconds)</h3>
  <div class="cb-bar-row"><span>p50 on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:33.4%;--bar:var(--cb-orange)"></span></span><span class="cb-value">292 ns</span></div>
  <div class="cb-bar-row"><span>p50 off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:28.6%;--bar:var(--cb-blue)"></span></span><span class="cb-value">250 ns</span></div>
  <div class="cb-bar-row"><span>p90 on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:47.7%;--bar:var(--cb-orange)"></span></span><span class="cb-value">417 ns</span></div>
  <div class="cb-bar-row"><span>p90 off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:38.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">334 ns</span></div>
  <div class="cb-bar-row"><span>p99 on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:71.4%;--bar:var(--cb-orange)"></span></span><span class="cb-value">625 ns</span></div>
  <div class="cb-bar-row"><span>p99 off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:71.4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">625 ns</span></div>
  <div class="cb-bar-row"><span>p99.9 on-heap</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">875 ns</span></div>
  <div class="cb-bar-row"><span>p99.9 off-heap</span><span class="cb-track"><span class="cb-fill" style="--value:90.4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">791 ns</span></div>
  <figcaption>Off-heap was 14.4% faster at p50 (250 ns vs 292 ns), tied at p99, and slightly ahead at the tail. Two million samples per mode. Measured on OpenJDK 21.0.11, results in benchmarks/java-gc-offheap/results/.</figcaption>
</figure>

Off-heap was faster. Not by a lot, but the wrong direction entirely from the warning I'd internalized, and the reason isn't the instruction count — the off-heap read does *more* work per lookup, copying the payload byte by byte out of the buffer, where on-heap is a single `arr[key]` dereference. It's memory layout. On-heap, those 3,500,000 payload arrays are 3,500,000 separate objects scattered wherever the allocator put them, so the deref is cheap but the payload read is a likely cache miss to somewhere cold. The direct buffer is one contiguous 672 MB run, so consecutive lookups keep landing in lines that are already warm. The extra copy loop loses to the cache miss it avoids.

The honest read is that the latency cost is real but it lives somewhere I didn't put it. If your off-heap value is a structured object you have to serialize on the way in and reconstruct on the way out, *that* deserialize is where the milliseconds go, and a real service moving rich models off-heap will feel it. My harness stores flat bytes, so the read stayed a copy and the contiguous layout even paid it back, and I'm not going to pretend that generalizes. What reproduced cleanly was the GC collapse; the "you'll pay for it on reads" part depends entirely on what your values are, and for a flat blob it simply didn't appear.

## The takeaway

If a JVM service runs hot on GC CPU while its pause times and p99 look fine, don't reach for a bigger heap first — check how much of the old generation never dies. A large permanent cache turns G1's concurrent marker into a treadmill: it re-scans a live set that doesn't shrink, frees nothing, and re-triggers, and none of that shows up as a pause. Moving those objects off-heap took total GC time from 5,216 ms to 114 ms and lifted throughput 30% here, because it removes the work instead of rescheduling it. The read-side cost you've been warned about is real, but it's the deserialize on structured values, not the fact of being off-heap — a flat payload pays nothing, and mine actually got faster. Measure your own lookup path before you assume which way that trade goes.

The [Docker harness, the Java program, the two run modes, and the raw GC logs are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-gc-offheap). These are laptop numbers from one container on a fixed 1500 MB heap, the mechanism transfers, the absolute milliseconds do not. I kept two non-reproducing sizings under results/attempts/ where the marking treadmill wouldn't spin up, because the tuning it took to make it spin is part of the point.
