---
layout: post
title: "One Byte Over the Slab"
date: 2026-07-18
description: "memcached rounds every item up to the next slab class. Land one byte over a boundary and you hand a fifth of your RAM to the allocator for nothing. I measured it."
categories: [caching, memcached, performance]
---

The first memcached tier I ran reported plenty of free memory and evicted my hottest keys anyway. `stats` said `limit_maxbytes` was nowhere near full, the hit rate was sliding, and the keys falling out were the ones I most wanted to keep. I spent an afternoon chasing the client, the TTLs, the traffic pattern. The problem was none of those. The problem was that memcached had already decided how to cut up its memory before I stored a single byte, and my values didn't fit the cuts.

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

## The problem

memcached doesn't hand out memory a byte at a time. It carves each 1 MB page into fixed-size chunks, and every item you store gets rounded up to the nearest chunk. The chunk sizes step up by a growth factor — 1.25 by default — so the ladder near a kilobyte looks like 944, 1184, 1480, 1856. Store an item whose real size is 945 bytes and it goes into a 1184-byte chunk. The other 239 bytes are gone. Not to another item, not to overhead you can point at — gone, held by the allocator, counted as used, invisible in `stats` unless you go digging in `stats slabs`.

That rounding is fine when your values happen to sit near a chunk size. It is a tax when they sit just past one. And "just past one" is not a rare accident — it is whatever your value size happens to be, which nobody picks with the slab ladder in mind. So I picked the worst case on purpose and measured what it costs.

## What the slab ladder actually does

Probe it yourself. Store one item, dump `stats slabs`, and read the `chunk_size` for each class. On memcached 1.6.45 with the default factor, near a kilobyte:

```
class 11: 944    class 15: 2320
class 12: 1184   class 16: 2904
class 13: 1480   class 17: 3632
class 14: 1856   class 18: 4544
```

The total item size memcached stores is your value plus the key plus a header — for a 16-byte key that overhead measured 75 bytes flat. So a 870-byte value becomes 945 bytes on the wire, which is one byte over the 944 chunk, which means it rounds all the way up to 1184. A 1109-byte value becomes 1184 exactly and fits class 12 with nothing left over.

Same class, same 400,000 items, same amount of real data. The only difference is which side of 944 the item lands on.

<figure class="cache-bench">
<h3>Same 400,000 items. One value size wastes a fifth of the RAM.</h3>
<div class="cb-panels">
  <div>
    <p class="cb-panel-title">value 870 B — one byte over 944</p>
    <div class="cb-bar-row"><span>Live data</span><span class="cb-track"><span class="cb-fill" style="--value:79.8%;--bar:var(--cb-blue)"></span></span><span class="cb-value">378 MB</span></div>
    <div class="cb-bar-row"><span>Wasted to rounding</span><span class="cb-track"><span class="cb-fill" style="--value:20.2%;--bar:var(--cb-orange)"></span></span><span class="cb-value">95.6 MB</span></div>
  </div>
  <div>
    <p class="cb-panel-title">value 1109 B — fills the chunk</p>
    <div class="cb-bar-row"><span>Live data</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">473.6 MB</span></div>
    <div class="cb-bar-row"><span>Wasted to rounding</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0 MB</span></div>
  </div>
</div>
<figcaption>Both cases store 400,000 items in chunk class 12 (chunk_size 1184). Worst case: value 870 B, item 945 B, 239 bytes wasted per item — 95.6 MB, 20.2% of allocated RAM, holding nothing. Snug case: value 1109 B, item 1184 B, zero waste. Measured on memcached 1.6.45, -m 2048 -f 1.25, results in benchmarks/memcached-slabs/results/exp1_worst_case.csv and exp1_best_case.csv.</figcaption>
</figure>

Twenty percent. The cache is holding 378 MB of data and paying for 473.6 MB of RAM, and every byte of that 95.6 MB gap is doing nothing but sitting between the end of one item and the edge of its chunk. `stats` will tell you the bytes are used. It won't tell you they're empty.

## The knob you can actually turn

The growth factor is a startup flag, `-f`. Drop it and the chunks step up in smaller increments, so any given item rounds up less. I took the same worst-case 870-byte value and started memcached with `-f 1.08` instead of the default.

<figure class="cache-bench">
<h3>Tighter growth factor, same data, less rounding.</h3>
<div>
  <div class="cb-bar-row"><span>-f 1.25 (default)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">20.2% · 39 classes</span></div>
  <div class="cb-bar-row"><span>-f 1.08</span><span class="cb-track"><span class="cb-fill" style="--value:11.8%;--bar:var(--cb-green)"></span></span><span class="cb-value">2.4% · 63 classes</span></div>
</div>
<figcaption>Same 870 B value, 400,000 items. Under -f 1.25 it lands in the 1184 chunk (20.2% wasted, 95.6 MB). Under -f 1.08 the ladder is finer, so 945 B lands in a 968-byte chunk — 2.4% wasted, 9.2 MB. The cost is 63 slab classes instead of 39: more classes, finer granularity, a little more per-class bookkeeping. Measured on memcached 1.6.45, results in benchmarks/memcached-slabs/results/exp2_growth_factor.csv.</figcaption>
</figure>

The finer ladder cut the waste from 20.2% to 2.4% — from 95.6 MB down to 9.2 MB, without touching a single value. You can't make the rounding disappear, only make each step smaller. And smaller steps mean more slab classes, which means memcached spreads its pages across more buckets. That's fine until it isn't, which brings up the second way slabs bite you.

## When the pages get stuck

Here's the part that actually cost me the afternoon. memcached assigns whole pages to a slab class, and by default it does not take them back. Once a page belongs to the class for 100-byte items, it stays there — even if you stop storing 100-byte items entirely and start storing 8 KB items that are starving for space. The RAM is right there, free, and the new items can't have it. That's slab calcification, and it's why a cache with "free memory" evicts your hot keys.

I reproduced it in a 64 MB cache. First I filled it with small items until every page belonged to the small class. Then I switched the workload entirely — a working set of large 8 KB items, rewritten over and over — and watched what the large class could get. Then I ran the exact same sequence again with one flag flipped: `-o slab_automove=2`, which tells memcached to reassign pages from a class that's stopped needing them to one that's thrashing.

<figure class="cache-bench">
<h3>Same large working set. One allocator hoards, one rebalances.</h3>
<div>
  <div class="cb-bar-row"><span>automove=0</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">74,882 · 1 page</span></div>
  <div class="cb-bar-row"><span>automove=2</span><span class="cb-track"><span class="cb-fill" style="--value:2.6%;--bar:var(--cb-green)"></span></span><span class="cb-value">1,976 · 44 pages</span></div>
</div>
<figcaption>Identical large working set (5,000 × 8 KB) rewritten under both allocators. With movement off, the large class stayed frozen at 1 page and evicted its own items 74,882 times while 63 pages stayed locked to the dead small workload. With automove=2, memcached reassigned pages until the large class held 44 of them, the working set went resident, and large-class evictions fell to 1,976. Measured on memcached 1.6.45, -m 64, results in benchmarks/memcached-slabs/results/exp3_calcification.csv.</figcaption>
</figure>

One flag, and the same workload went from evicting itself 74,882 times to 1,976. The free pages were there the whole time — with movement off they stayed pinned to a workload that had already stopped, and only reassigned once I let memcached move them.

One honest caveat, because the raw number can lie. If you look at *global* evictions instead of large-class evictions, automove=2 looks worse — 236,799 versus 74,882. That's because reassigning a page first evicts the stale small items still living on it. Those are one-time reclamations of data I'd already abandoned. The number that matters is the live workload's own thrashing, and that's the one that dropped 38x. It took some tuning to get a clean read — my first attempt sized the large set bigger than the cache could ever hold, so both modes evicted on capacity and the allocator's effect vanished into the noise. That failed run is written up under `results/attempts/` because the wrong-looking number is the whole point.

## The takeaway

memcached decides how to cut up memory before you store anything, and it rounds every item up to fit. Two things follow. First, your value size interacts with a slab ladder you didn't choose — land one byte over a boundary and you can hand a fifth of your RAM to the allocator, invisible in every stat except `stats slabs`. Check where your real item sizes fall, and if they're sitting just past a chunk, either reshape the value or tighten `-f` and eat the extra slab classes. Second, pages don't move on their own unless you tell them to — if your workload's item sizes shift over time, run with `slab_automove` on, or you'll evict hot keys while free RAM sits locked to data nobody's asking for.

Neither of these shows up as an error. The cache just quietly holds less than you paid for, and the only way to know is to measure the chunk your items actually land in.

The harness — digest-pinned memcached, the slab probe, and every number above — is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/memcached-slabs). These are laptop numbers meant to show the mechanism, not your cluster's capacity — the percentages are the point, not the megabytes.
