---
layout:     post
title:      The Garbage the Collector Couldn't Collect
date:       2026-07-20
description:    A refactor moved one object from being built once at startup to being built new on every request. Nothing referenced the old copies, so I assumed the garbage collector would clean them up. It couldn't. Each one prestarted two threads, and a live thread is a GC root, so 189 requests left 378 threads pinning 190 MB the collector was helpless to reclaim, and the heap walked straight into an OutOfMemoryError.
categories: java performance memory-leaks jvm
---

If you've ever watched a service OOM-kill itself in production under load that wouldn't have bothered it a month ago, and the heap graph is a slow clean ramp with no single allocation to blame, this is the shape of the bug you're looking for. Mine started as a code review comment that sounded like a cleanup: an object that used to be a startup singleton got moved to being constructed per request, one fresh copy each time a job came in. It read cleaner. It also leaked, and the thing that made it leak is the same thing that made it look harmless, nobody kept a reference to the old copies.

## The problem

The object owned two things: a buffer, standing in for an HTTP client's working state, and a small thread pool it used to run jobs. As a singleton, one of each existed for the life of the process. Refactored to per-request, every incoming request did `new JobExecutor()`, used it once, and dropped it on the floor. No field held it, no list, no cache, nothing. So the intuition is obvious and wrong: unreferenced object, next GC takes it. Except the constructor prestarted the pool's core threads, and a running thread is a garbage collection root. The thread keeps its `Worker` alive, the worker keeps the `ThreadPoolExecutor` alive, the executor keeps its thread factory alive, and the factory is an inner class of the `JobExecutor`, so it keeps the whole object and its buffer alive too. You threw away the handle. The threads never got the message.

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
.cb-bar-row { display: grid; grid-template-columns: minmax(7rem, 1.3fr) minmax(6rem, 4fr) minmax(4.2rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
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
}
</style>

## What I actually built

One switchable Java program, `MODE=leaky` or `MODE=fixed`, running a request loop paced at one request every 300ms. The `JobExecutor` holds a 512 KB buffer and a `ThreadPoolExecutor` with two core threads, prestarted, and `allowCoreThreadTimeOut(false)` so those threads never idle out and die. In leaky mode each request builds a new one and never calls `shutdown()`. In fixed mode a single `JobExecutor` is built once at startup and reused for every request. The whole thing runs under a deliberately small heap so the leak has somewhere to hit a wall:

```
-Xmx192m -Xms192m -Xss256k -XX:+UseG1GC
```

Every five requests I sample heap used right after a full GC, the live count of pool threads, and a static counter of how many `JobExecutor` instances have been constructed. That last number is the honest version of what a heap dump would tell you: not "memory is high" but "there are N of these, and there should be one."

## The ramp

Here is leaky mode walking off the cliff, and fixed mode not moving.

<figure class="cache-bench">
  <h3>Heap used after each full GC, over requests served (−Xmx192m)</h3>
  <svg viewBox="0 0 640 260" role="img" aria-label="Line chart: leaky heap climbs linearly from 6.6 MB at 5 requests to 190.9 MB at 189 requests where it OOMs, while fixed stays flat near 2.7 MB across all 600 requests" style="width:100%;height:auto;font-size:11px;">
    <line x1="48" y1="20" x2="620" y2="20" stroke="var(--cb-orange)" stroke-width="1" stroke-dasharray="4 4" opacity="0.7"/>
    <text x="44" y="23" text-anchor="end" fill="var(--cb-muted)">192 (cap)</text>
    <line x1="48" y1="120" x2="620" y2="120" stroke="var(--cb-grid)" stroke-width="1"/>
    <text x="44" y="123" text-anchor="end" fill="var(--cb-muted)">96</text>
    <line x1="48" y1="220" x2="620" y2="220" stroke="var(--cb-grid)" stroke-width="1"/>
    <text x="44" y="223" text-anchor="end" fill="var(--cb-muted)">0</text>
    <polyline points="52.8,213.2 95.7,166.1 143.3,113.9 191.0,61.9 228.2,21.2" fill="none" stroke="var(--cb-orange)" stroke-width="2.5"/>
    <circle cx="228.2" cy="21.2" r="4" fill="var(--cb-orange)"/>
    <text x="235" y="24" fill="var(--cb-orange)" font-weight="700">OOM @ 189 req</text>
    <polyline points="52.8,217.2 620,217.2" fill="none" stroke="var(--cb-green)" stroke-width="2.5"/>
    <text x="560" y="212" text-anchor="end" fill="var(--cb-green)" font-weight="700">fixed</text>
    <text x="228" y="238" text-anchor="middle" fill="var(--cb-muted)">189</text>
    <text x="620" y="238" text-anchor="end" fill="var(--cb-muted)">600 requests</text>
  </svg>
  <figcaption>Leaky: 6.57 MB after GC at 5 requests, climbing a near-perfect straight line to 190.86 MB at 189 requests, where it threw OutOfMemoryError: Java heap space. Fixed: 2.66 MB, flat, for all 600 requests, still alive when I stopped it. The dashed line is the 192 MB heap cap. Measured on OpenJDK 21.0.11, G1GC, results in benchmarks/java-memory-leak/results/.</figcaption>
</figure>

The leaky line is the giveaway that this is a leak and not just a busy heap. It doesn't sawtooth. A healthy heap under load goes up as you allocate and drops back down after every GC, the classic teeth. This one only ever ratchets up, because the floor, the live set that survives a full GC, is what's growing. The collector is running the whole time. It just has less and less it's allowed to free after each pass. Every 512 KB buffer plus its two pinned thread stacks is a step up in the floor that never comes back down.

## Why the collector was helpless

The thing worth staring at is that GC was working fine. Near the end it was working overtime. In the final five-request interval before death the GC count jumped by 17, full collections back to back, and the log shows the terminal thrash plainly:

```
Pause Full (System.gc()) 191M->191M(192M)
```

191 megabytes in, 191 megabytes out. A full stop-the-world collection that freed nothing, because everything on the heap was reachable from a live thread, and a live thread is a root you cannot collect around. This is the part the "just null out your references" advice skips over. I did null out the reference. There was no reference. The retention chain doesn't run through my code at all, it runs `Thread` to `Worker` to `ThreadPoolExecutor` to thread factory to `JobExecutor`, entirely inside the JDK's own object graph, anchored by two threads I told the pool to start and never told it to stop.

You can read the leak in two counts, and they move in lockstep.

<figure class="cache-bench">
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Live pool threads at death</p>
      <div class="cb-bar-row"><span>leaky</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">378</span></div>
      <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:0.53%;--bar:var(--cb-green)"></span></span><span class="cb-value">2</span></div>
    </div>
    <div>
      <p class="cb-panel-title">JobExecutor instances</p>
      <div class="cb-bar-row"><span>leaky</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">190</span></div>
      <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:0.53%;--bar:var(--cb-green)"></span></span><span class="cb-value">1</span></div>
    </div>
  </div>
  <figcaption>At the moment leaky OOM'd it had built 190 JobExecutors and was holding 378 live pool threads, exactly two per instance minus the last one that ran out of heap mid-construction. Fixed built one and held two, and those numbers never changed across 600 requests. Measured on OpenJDK 21.0.11, results in benchmarks/java-memory-leak/results/.</figcaption>
</figure>

Threads are exactly twice instances, every row, because each `JobExecutor` prestarts two core threads. That's also why the per-instance cost is worse than it looks. The buffer is 512 KB, but the heap climbed 184.29 MB across 185 instances, almost exactly 1.0 MB each. The buffer is only half the leak. The other half is the two thread stacks and the pool machinery, dragged along because you can't free the buffer without freeing the object, and you can't free the object while its threads are alive.

## The fix, and what it buys

The fix is the refactor in reverse: build the `JobExecutor` once, at startup, and reuse it. That's the entire diff. In the benchmark that's the difference between `MODE=leaky` and `MODE=fixed`, and the difference in outcome is the difference between a process that dies and one that doesn't.

<figure class="cache-bench">
  <h3>Requests served before the process died</h3>
  <div class="cb-bar-row"><span>leaky</span><span class="cb-track"><span class="cb-fill" style="--value:31.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">189, died</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">600+, alive</span></div>
  <figcaption>Leaky reached OutOfMemoryError after 189 requests in 57.2 seconds. Fixed served 600 requests in three minutes, 3.2x as many, heap flat at 2.66 MB the whole way, and it hadn't died, I stopped it. On a real service the leaky number isn't "189 requests," it's "however many requests fit between restarts," which is why it looks like a memory problem that only shows up under sustained traffic. Measured on OpenJDK 21.0.11, results in benchmarks/java-memory-leak/results/.</figcaption>
</figure>

The reason this bug hides in review is that the leaky version is correct. It computes the right answer for every request. It just also starts two threads it never stops, and threads are cheap enough that the first few thousand requests look completely fine, which is exactly long enough to pass a load test and ship. The failure is delayed by the size of your heap divided by a megabyte, and then it's an OOM under load nobody changed.

## The takeaway

An unreferenced object is only collectible if nothing reachable is holding it, and a running thread is always reachable. Any object that starts a thread, opens a pool, or holds a native handle is not garbage the moment you drop your reference to it, it's garbage the moment you close it, and if you never close it, it isn't garbage at all. That's the whole bug: `new JobExecutor()` per request instead of once, no `shutdown()`, and 189 requests later the collector is running full GCs that free zero bytes because the live set is the leak.

Three things worth keeping:

- A leak looks like a ramp, not a sawtooth. If heap-after-GC only ever climbs, your live set is growing and you're looking for something that's held, not something that's slow to free.
- The retention chain often runs through the JDK, not your code. Nulling your own reference does nothing when the real root is a thread you started. Prestarted core threads with `allowCoreThreadTimeOut(false)` are permanent roots by design.
- Resource-owning objects belong at startup, scoped to the process, not to a request. If you must build one per request, it owns a `close()` and you call it, in a `finally`, every time.

These are laptop numbers built to show the mechanism on a 192 MB heap, not capacity planning, [the lab is in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-memory-leak) with both modes, the GC logs, and the CSVs the charts are drawn from. Run it yourself and watch the floor climb, it's the most convincing argument I know for not building a thread pool on the hot path.
