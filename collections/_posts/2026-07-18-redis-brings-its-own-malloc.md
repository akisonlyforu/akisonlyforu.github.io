---
layout:     post
title:      Redis Brings Its Own malloc, and Here's Why
date:       2026-07-18
description:    Redis doesn't use your system's malloc. It ships jemalloc in its own source tree and compiles it in by default. I went looking for why, built the same Redis two ways, and watched the fragmentation ratio pick a side.
categories: redis memory jemalloc allocator operations
---

For years I filed `malloc` under "solved problem the OS handles" and never thought about it again. You call `malloc`, you get memory, you call `free`, you give it back, and whichever `malloc` happens to be linked in does a fine job. Then one day I was reading an `INFO memory` dump trying to explain a fragmentation ratio to someone and my eye caught a line I'd scrolled past a thousand times:

```
mem_allocator:jemalloc-5.3.0
```

Redis wasn't using my system's `malloc`. It had brought its own. And once I noticed that, the obvious question was why a database that's otherwise happy to lean on the OS for everything decided the one thing it couldn't trust the OS with was handing out memory. This is me chasing that down, and then building the same Redis two different ways to watch the difference show up in a number.

If you've ever wondered why `deps/jemalloc` is sitting in the Redis source tree, or why your fragmentation ratio looks nothing like the one a coworker on a different distro is quoting, this is for you.

## What jemalloc even is

jemalloc is a general-purpose memory allocator that Jason Evans originally wrote for FreeBSD's libc. The "je" is his initials, which tells you it started as one person's `malloc` and grew into the thing Facebook, Redis, Rust's early runtime, and a pile of other memory-sensitive systems ended up reaching for.

The reason it exists at all is that the default allocator on most Linux boxes, glibc's `ptmalloc`, is tuned to be a reasonable general-purpose choice for programs that allocate a bit of memory and mostly get on with their lives. Redis is not that program. Redis is a process whose entire job is to hold millions of small allocations of wildly varying sizes, churn them constantly as keys come and go, and stay resident for months. That's close to the worst-case workload for a general allocator, and it's exactly the workload jemalloc was built to survive.

Redis takes this seriously enough that it doesn't link jemalloc from the system. It vendors a pinned copy in `deps/jemalloc` and statically compiles it in, so the allocator is the same version everywhere Redis is built the standard way, regardless of what the host distro ships. On Linux the default build is jemalloc. If you build with `make MALLOC=libc`, you get the system allocator instead, and that flag is the whole reason this post has a benchmark.

## Why not just use the system malloc

The short version is fragmentation, and jemalloc is structured specifically to keep it low. Three pieces of that structure matter for understanding your `INFO memory` output.

**Arenas.** jemalloc splits its memory into multiple independent arenas and hands each thread one to allocate from. The point is lock contention, threads working out of different arenas aren't fighting over the same allocator locks. Redis is mostly single-threaded on the command path, but the I/O threads and the background threads still benefit, and it means a freed allocation goes back to the arena it came from rather than into one global pool everyone contends on.

**Size classes.** This is the one that shows up in your numbers. jemalloc doesn't hand you exactly the number of bytes you asked for. It rounds every request up to the nearest of a fixed set of size classes, 8, 16, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, and up from there. Ask for a 200-byte value and jemalloc gives you a 224-byte slot. Those 24 bytes are internal fragmentation, and you pay them on every allocation of that size.

That sounds wasteful until you see what it buys. Because every allocation of a given size class is the same width, a freed slot is always a perfect fit for the next allocation of that size. There's no slow accumulation of oddly-sized holes that nothing quite fits into, which is the external fragmentation that eats a general allocator alive under Redis's churn. jemalloc trades a small, bounded, predictable amount of internal waste for not drowning in unpredictable external waste. That's the deal, and for Redis it's a good one.

**Extents and dirty pages.** jemalloc grabs memory from the kernel in big chunks called extents and carves size-class slots out of them. When you free everything on a page, jemalloc doesn't immediately hand that page back to the kernel. It keeps freed-but-dirty pages around so it can reuse them fast, and only decays them back to the OS over time, governed by `dirty_decay_ms` and `muzzy_decay_ms`. This is the mechanism behind RSS lingering after a big delete, and it's a deliberate speed choice, not a bug. It also means the allocator you compiled in is directly responsible for the gap between `used_memory` and `used_memory_rss` that pages people at 3am. I wrote a whole [separate post about that gap turning into an OOM kill](/blog/redis-said-it-was-fine/), and jemalloc's decay behavior is the root of it.

## The benchmark

There's no honest way to benchmark jemalloc in the abstract, and a bare-allocator microbenchmark wouldn't tell you anything you could act on, because you don't run bare jemalloc, you run Redis. So the experiment is to build the exact same Redis two ways, once with its default bundled jemalloc and once with `make MALLOC=libc` so it links glibc's `ptmalloc`, then throw the identical workload at both and read the difference straight out of `INFO memory`.

The workload is the one that makes an allocator sweat: load a large set of small keys of a few different value sizes, then churn them, overwrite a chunk, delete a chunk, add more, the constant reshaping of the keyspace that a real Redis lives through, not a clean load-then-measure. After the churn settles I read three numbers off each build:

- `used_memory`, what Redis asked the allocator for.
- `used_memory_rss`, what the OS is actually keeping resident.
- `mem_fragmentation_ratio`, which is just `rss / used`, and is the number that tells you how much the allocator is holding over what Redis is using.

Both builds are the same Redis version, same config, same digest-pinned container, same deterministic workload seeded the same way. The only variable is which `malloc` got compiled in. The [Docker harness, the two build targets, the workload script, and the raw `INFO memory` dumps live in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-jemalloc). Laptop numbers from one container, the mechanism transfers, the absolute megabytes do not. Both builds are Redis 7.4.0; the default build reports jemalloc-5.3.0.

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
  <h3>Same Redis, same churn, two allocators</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">jemalloc build (default)</p>
      <div class="cb-bar-row"><span>used_memory</span><span class="cb-track"><span class="cb-fill" style="--value:59%;--bar:var(--cb-blue)"></span></span><span class="cb-value">25.26M</span></div>
      <div class="cb-bar-row"><span>RSS</span><span class="cb-track"><span class="cb-fill" style="--value:96%;--bar:var(--cb-orange)"></span></span><span class="cb-value">41.14M</span></div>
      <div class="cb-bar-row"><span>frag ratio</span><span class="cb-track"><span class="cb-fill" style="--value:96%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.63</span></div>
    </div>
    <div>
      <p class="cb-panel-title">libc build (MALLOC=libc)</p>
      <div class="cb-bar-row"><span>used_memory</span><span class="cb-track"><span class="cb-fill" style="--value:59%;--bar:var(--cb-blue)"></span></span><span class="cb-value">25.20M</span></div>
      <div class="cb-bar-row"><span>RSS</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">42.72M</span></div>
      <div class="cb-bar-row"><span>frag ratio</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.70</span></div>
    </div>
  </div>
  <figcaption>Both builds ask for about the same used_memory, because that's just the data. The difference is RSS and the ratio, which is the allocator's fingerprint. Bar widths are normalized against the larger RSS across the two builds; the frag-ratio bars against the higher ratio. Measured values from benchmarks/redis-jemalloc/results/.</figcaption>
</figure>

## Where the difference landed

Before running it I wrote down what I expected, because a benchmark you've already peeked at has a way of confirming whatever you walked in believing. My bet was that `used_memory` would come out about the same on both builds, since that number is just the data Redis is holding and the data doesn't care which allocator stored it, and that the whole split would show up in RSS and therefore in the ratio. glibc's `ptmalloc`, handed Redis's churn, tends to accumulate scattered free space it can't hand back, so I expected its RSS and its ratio to sit higher after the same workload, and jemalloc's size-class discipline to keep its ratio closer to the ground.

That's roughly how it landed, and I'll be honest that it was closer than the folklore made me expect. On a churny run of 50,000 keys across five size classes, the libc build settled at 42.72M resident with a fragmentation ratio of 1.70; the jemalloc build held 41.14M at 1.63. Both asked the allocator for basically the same `used_memory`, so that whole gap is the allocator's doing, but on this workload it's a few percent, not the night-and-day margin you might picture. Which is worth saying plainly: if raw fragmentation on a steady churn were the only axis, you could almost shrug at the difference. The reason jemalloc actually isn't a coin-flip is the next section.

## The part where jemalloc isn't optional

Even before the numbers, there's one thing the libc build straight up cannot do, and it's the reason the choice isn't really a toss-up. Redis active defragmentation only works with jemalloc.

`activedefrag yes` works by walking Redis's allocations, spotting the ones sitting on sparsely-populated pages, and copying them into denser pages so the empty ones can go back to the kernel. Redis can only do that because jemalloc exposes enough about where an allocation physically lives for Redis to ask "is this thing worth relocating," through a hook jemalloc provides for exactly this purpose. glibc's `malloc` gives you no such window. It hands you a pointer and keeps its bookkeeping to itself.

So the libc build fragments a bit more, and worse, it has no way to clean up after itself when it does. On the jemalloc build, a fragmentation ratio that climbs after a mass delete is something you can turn `activedefrag` loose on. On the libc build, your only defrag is restarting the process. That gap alone is enough to explain why Redis ships jemalloc as the default and leaves libc as the escape hatch.

## A note for the Rust and module crowd

If you write a Redis module, this comes back around in a way worth knowing. A module that allocates with the system `malloc` puts memory outside Redis's accounting, so `used_memory` undercounts what your module is really holding and `maxmemory` can't see it. Redis exposes its own allocation functions, `RedisModule_Alloc` and friends, that route through the same jemalloc Redis itself uses, so anything you allocate that way lands in the same pool and shows up in the same numbers. In Rust the redismodule bindings wire the global allocator to those functions for the same reason, so a `Vec` you grow inside a module is memory Redis knows about. It's the same lesson as the rest of this post from a different angle, which allocator your bytes come from is not an implementation detail Redis lets you ignore.

## Stuff worth remembering

- Redis vendors and compiles in its own jemalloc rather than trusting the system `malloc`, and `INFO memory` tells you which one you actually got on the `mem_allocator` line. Check it before comparing fragmentation numbers with anyone, because a jemalloc box and a libc box aren't measuring the same thing.
- jemalloc rounds every allocation up to a size class, so a 200-byte value costs you a 224-byte slot. That internal waste is the price of not accumulating the unpredictable external fragmentation that sinks a general allocator under churn.
- The lingering gap between `used_memory` and `used_memory_rss` is jemalloc holding dirty pages on purpose for reuse speed, decayed back to the kernel slowly. It's a feature until a hard memory limit turns it into an OOM.
- `activedefrag` needs jemalloc. On a libc build your only way to reclaim fragmented RSS is a restart, which is reason enough to leave the default alone.

I went into this thinking the allocator was a box I'd never have to open. It turned out to be sitting right there in the source tree, statically linked, quietly deciding the shape of every memory number I look at. If your fragmentation ratio ever looks wrong, the first thing to confirm is which `malloc` is even reporting it.
