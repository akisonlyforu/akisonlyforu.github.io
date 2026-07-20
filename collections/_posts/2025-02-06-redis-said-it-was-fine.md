---
layout:     post
title:      The Memory Redis Wouldn't Give Back
date:       2025-02-06
description:    used_memory sat there calm while RSS climbed to the cgroup limit and the kernel killed the process. Everyone said leak. It wasn't. I reproduced it locally and chased down why the memory number Redis shows you isn't the one that gets you killed.
categories: redis memory jemalloc operations
---

If you've ever watched `used_memory` sit there calm and reasonable while the OOM killer took the process anyway, and everyone in the channel immediately said leak, this is for you. The OOM killer, if you haven't met it, is the Linux kernel's out-of-memory killer, the thing that picks a process and terminates it when the machine (or the cgroup) runs out of memory. Most of the time it's fragmentation rather than a leak, and the reason nobody believes that at first is that the number they're all staring at genuinely does look fine.

## The problem

A Redis process gets killed by the kernel for running out of memory while `used_memory` still reads comfortably under its limit, so the whole channel assumes a leak. It usually isn't one. After a big delete `used_memory` drops but the memory the kernel actually counts stays high, `maxmemory` never reacts because it's watching the wrong number, and the process walks into the OOM killer looking healthy the whole way. This is the reproduction and the three memory numbers that tell them apart.

I wanted to reproduce this the same way I reproduce anything I don't fully trust myself to explain, small enough to run on a laptop and argue with the same allocator that's doing it to you in production. The setup is a queue-shaped workload: load a pile of small keys, pretend to batch-process them, then delete millions of them at once, which is the part that matters. That mass delete is where `used_memory` and the actual resident footprint stop agreeing.

Every memory behavior and cgroup OOM rule reproduced perfectly on the first attempt. I left the shapes that didn't move RSS in the repo too, because a version where the memory quietly comes back on its own would make for a neater story than the one that actually happened.

The [Docker harness, deterministic workload, Redis config, and the raw `INFO memory` dumps are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-oom). These are laptop numbers from one container with a hard memory limit, the mechanism transfers, the absolute megabytes do not. Redis 7.4.0, jemalloc 5.3.0.

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
.cb-svg .used { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .rss { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
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

<figure class="cache-bench">
  <h3>The mass delete, as the two numbers see it</h3>
  <!-- Geometry computed from benchmarks/redis-oom/results/memory_timeline.csv (empty_settle through deleted_settle). -->
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="redis-mem-title redis-mem-desc">
    <title id="redis-mem-title">used_memory versus used_memory_rss across a mass delete</title>
    <desc id="redis-mem-desc">After the bulk delete, used_memory falls sharply while used_memory_rss stays near its peak.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="18" y="214">0</text>
    <text x="30" y="124">½ max</text>
    <text x="30" y="34">max</text>
    <polyline class="rss"  points="90.0,189.8 108.3,189.8 126.2,189.8 144.4,189.8 162.7,189.8 181.0,181.3 199.2,129.9 217.5,79.4 235.8,30.2 254.0,30.2 271.9,30.2 290.2,30.2 308.5,30.2 326.7,30.2 345.0,30.2 363.3,30.2 381.5,30.2 399.8,30.2 418.1,30.2 436.0,30.0 454.2,30.0 472.5,30.0 490.8,30.0 508.7,30.0 526.9,30.0 545.2,30.0 563.5,30.0 581.7,30.0 600.0,30.0" />
    <polyline class="used" points="90.0,207.4 108.3,207.4 126.2,207.4 144.4,207.4 162.7,207.4 181.0,198.9 199.2,145.5 217.5,92.0 235.8,37.9 254.0,37.9 271.9,37.9 290.2,37.9 308.5,37.9 326.7,37.9 345.0,37.9 363.3,37.9 381.5,37.9 399.8,37.9 418.1,37.9 436.0,200.9 454.2,200.9 472.5,200.9 490.8,200.9 508.7,200.9 526.9,200.9 545.2,200.9 563.5,200.9 581.7,200.9 600.0,200.9" />
    <text x="403" y="24">delete</text>
    <line class="grid" x1="418" y1="30" x2="418" y2="210" style="stroke-dasharray:3 3" />
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>used_memory (what Redis asked for)</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>used_memory_rss (what the OS backs)</span>
  </div>
  <figcaption>used_memory drops the instant the keys are gone. RSS doesn't move, because jemalloc is still holding the freed pages. The gap between the two lines is the fragmentation everyone mistakes for a leak.</figcaption>
</figure>

## First, there are three numbers

The thing that makes this confusing is that "how much memory is Redis using" isn't one question, it's three, and the three don't get reported to the same place.

- **`used_memory`** is what Redis asked jemalloc for. Keys, values, overhead. This is what your dashboard graphs and what people mean when they say we're at 60% and we're fine.
- **`used_memory_rss`** is the resident set size, RSS, which is the amount of physical memory the OS is actually keeping resident for the process. The OOM killer and your cgroup only ever look at this one, which is what makes it the one that pages you at 3am.
- **`mem_fragmentation_ratio`** is just `rss / used`. A bit over 1 is normal and healthy. Sitting at 19 right after you deleted a few hundred thousand keys is what this whole post is chasing.

So the trouble is that pretty much everything you'd naturally watch is built on the first number, and the one that actually kills the process is the second.

## The workload I used

Nothing exotic. Load 220,000 small keys, each value about 200 bytes, the shape of a queue that's buffered a lot of little jobs. Let it settle. Snapshot the numbers. Then delete essentially all of them in one go, the way a batch job does when it finishes a run and clears its working set.

Here is Redis right before the delete, straight out of `INFO memory`:

```
used_memory_human:71.61M
used_memory_rss_human:74.82M
mem_fragmentation_ratio:1.04
maxmemory_human:95MB
evicted_keys:0
```

Sensible. `used` and `rss` are close, the ratio is near 1, we're comfortably under `maxmemory`. Now the same block right after the mass delete:

```
used_memory_human:3.81M
used_memory_rss_human:74.91M
mem_fragmentation_ratio:19.68
maxmemory_human:95MB
evicted_keys:0
```

`used_memory` collapsed, exactly like you'd expect after deleting the keys. `used_memory_rss` barely moved. The fragmentation ratio jumped to 19.68. That gap is memory the process is still holding from the OS that Redis will happily tell you it isn't using.

## Why the memory didn't come back

When you delete a key, Redis frees it back to jemalloc. That is not the same as jemalloc giving the page back to the kernel. jemalloc manages memory in runs and chunks, and a page only becomes returnable once everything living on it is freed. After a mass delete you get pages that are mostly empty but not entirely, one surviving allocation is enough to pin a whole page as resident.

On top of that, jemalloc doesn't rush to hand pages back even when it can. It keeps freed-but-dirty pages around on purpose so it can reuse them fast instead of going back to the kernel for every allocation, and it only decays them back over time. So immediately after a big delete you're in the worst spot: the keys are gone from `used_memory`, but the pages are still resident in `used_memory_rss`, and they'll only trickle back slowly if nothing forces the issue.

This is why it reads as a leak when it isn't one. Nothing is actually lost, the memory is still accounted for and still reusable, and jemalloc does hand it back eventually, just a lot slower than the OOM killer is willing to wait.

## Why maxmemory watched it happen

This is the part that actually pages someone. `maxmemory` is checked against `used_memory`, never against RSS. Eviction fires when the number Redis asked for crosses the limit, and nothing else moves it.

So through all of this `used_memory` is low, sometimes very low right after a delete. Redis looks at it, compares it to `maxmemory`, and correctly decides there's nothing to evict. `evicted_keys` sat at 0 the whole time. Meanwhile the resident footprint is climbing toward the container's 95MB limit, and the cgroup could not care less what `used_memory` says, it measures RSS. When RSS crossed 95MB the kernel OOM-killed the container, exit code 137, which is its way of not leaving a note.

So `maxmemory`, the thing you set to protect yourself, is checking `used_memory`, which looks fine the whole time, and nobody ever put a limit on RSS, which is the number that actually gets the process killed.

<figure class="cache-bench">
  <h3>maxmemory was measuring the wrong number</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">What maxmemory checked</p>
      <div class="cb-bar-row"><span>used_memory</span><span class="cb-track"><span class="cb-fill" style="--value:4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">3.81M</span></div>
      <div class="cb-bar-row"><span>maxmemory</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-muted)"></span></span><span class="cb-value">95MB</span></div>
    </div>
    <div>
      <p class="cb-panel-title">What the cgroup checked</p>
      <div class="cb-bar-row"><span>RSS</span><span class="cb-track"><span class="cb-fill" style="--value:79%;--bar:var(--cb-orange)"></span></span><span class="cb-value">74.91M</span></div>
      <div class="cb-bar-row"><span>cgroup limit</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-muted)"></span></span><span class="cb-value">95MB</span></div>
    </div>
  </div>
  <figcaption>Same instant, two measurements. Redis saw plenty of headroom under maxmemory and evicted nothing. The cgroup saw RSS against its own limit. Bar widths are plotted using measurements from benchmarks/redis-oom/results/.</figcaption>
</figure>

## The fix

A few things actually help here, and restarting the process isn't one of them.

The direct answer to resident-but-empty pages is `activedefrag`. Turn it on and Redis walks the fragmented allocations, copies live data into denser pages, and lets the emptied ones go back to the kernel. In my run, RSS after the same delete came down to 19.80M and the ratio settled to 5.2 instead of staying pinned up near the peak. It burns some CPU while it runs. I'll take that over a 137.

The second thing is to set `maxmemory` with real headroom under the cgroup limit, not right up against it. Since eviction watches `used_memory` and the kill watches RSS, you're deliberately leaving room for the gap between them. If the hard limit is X, `maxmemory` has to sit far enough below X that even an ugly fragmentation ratio can't shove RSS past the limit before eviction or defrag catches up.

And if you can, don't delete everything in one shot. The spike is the bulk free, so when I deleted the same keys in batches with `UNLINK` instead, RSS only peaked at 20.29M and the ratio held around 5.33, because jemalloc got to reuse and decay pages as it went rather than being handed the whole cliff at once. `UNLINK` also frees on a background thread, so you're not stalling command processing while it cleans up.

## Stuff worth remembering

- When Redis looks like it's leaking, check `mem_fragmentation_ratio` before you reach for a heap profiler. Calm `used_memory` sitting next to high `rss` is fragmentation, and it wants a completely different fix than a real leak.
- `maxmemory` only ever looks at `used_memory`, so it will happily let RSS climb after a mass delete without evicting a thing. Under a hard memory limit, minding that gap is on you.
- Deleting a lot of keys at once can cost you more resident memory for a while, not less. Batch it with `UNLINK` when you can.
- These are laptop-scale numbers, here to show the mechanism, not to size anything. What matters is which `INFO` field is telling the truth and which limit is actually load-bearing.

## The takeaway

The reason this one is worth keeping in your head is that it's a ten-minute fix once you know to look at RSS, and I've watched it turn into a two-day "we have a leak" hunt when nobody did. `used_memory` is the number Redis asked for and `used_memory_rss` is what the OS is actually holding, so when the two disagree by a lot, trust RSS, because that's the number the kernel is about to act on.
