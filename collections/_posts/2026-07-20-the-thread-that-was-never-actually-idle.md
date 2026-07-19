---
layout:     post
title:      The Thread That Was Never Actually Idle
date:       2026-07-20
description:    A non-blocking queue.poll() in a hot loop pegged a consumer thread at 91.8% CPU for a queue that was empty almost the whole time. I expected the flame graph to show an empty loop spinning on nothing. It showed lock contention instead, 77% of samples in AbstractQueuedSynchronizer.
categories: java performance concurrency flame-graphs
---

If you've ever seen a consumer thread sitting at 90%+ CPU on a queue that's basically empty, you've probably already guessed the bug before anyone opens a profiler: it's polling in a tight loop with no backoff, and it's spinning on nothing. I built exactly that this week, ten consumer threads each polling their own mostly-empty queue, one item arriving roughly every half second per queue. I went in expecting the flame graph to confirm the obvious story. It confirmed a slightly different one.

## The problem

`queue.poll()` is non-blocking. It checks the queue, and if there's nothing there, it returns `null` immediately, no waiting. Put that in a `while(true)` with nothing else in the loop body and you've built a thread that checks an empty queue as fast as the CPU will let it, forever, whether or not there's ever anything to find. Each thread parks itself on exactly one core and holds it there. The usual fix is `queue.poll(timeout, unit)`, the blocking overload, which parks the thread (`LockSupport.park` under the hood) until either an item shows up or the timeout elapses, so an idle consumer actually stays idle.

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

Ten consumer threads, one `ArrayBlockingQueue` each, a producer trickling one item into a queue roughly every 500ms per queue, one core per consumer on a 10-core host so the aggregate CPU number would actually mean something:

```java
Integer item = fixed ? q.poll(50, TimeUnit.MILLISECONDS) : q.poll();
```

Same 35-second run, same producer rate, same queues. The only difference is which overload of `poll()` gets called.

## The number that did move

Unlike the regex bug, this one is exactly as dramatic on CPU% as the reputation suggests:

<figure class="cache-bench">
  <h3>CPU load, bad vs fixed (10 consumer threads, 10-core host)</h3>
  <div class="cb-bar-row"><span>bad</span><span class="cb-track"><span class="cb-fill" style="--value:91.77%;--bar:var(--cb-orange)"></span></span><span class="cb-value">91.8%</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:0.22%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.2%</span></div>
  <figcaption>91.77% avg (peak 97.01%) vs 0.22% avg (peak 1.37%). 417x more CPU burned for the same job. Measured on OpenJDK 25.0.1, results in benchmarks/java-high-cpu-debugging/results/.</figcaption>
</figure>

And the job really is the same job. Both variants delivered the identical 70 items over the run, the producer's rate doesn't change based on how the consumer polls. What changes is how many times each consumer asks: the non-blocking version polled its queue 47.56 billion times combined across ten threads to move those 70 items. The blocking version asked 6,572 times, total, for the identical result. That gap, 47.56 billion vs 6,572, is the whole bug in one comparison.

## What the flame graph actually shows

This is where I was wrong going in. I expected the bad flame graph to be dominated by the loop body itself, an empty `while` spinning on `null`. It isn't.

<figure class="cache-bench">
  <h3>Where the "bad" run's CPU samples land</h3>
  <div class="cb-bar-row"><span>AQS.signalNext</span><span class="cb-track"><span class="cb-fill" style="--value:77%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~77%</span></div>
  <div class="cb-bar-row"><span>SpinBug loop body</span><span class="cb-track"><span class="cb-fill" style="--value:15%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~15%</span></div>
  <div class="cb-bar-row"><span>other AQS / CAS / lock frames</span><span class="cb-track"><span class="cb-fill" style="--value:8%;--bar:var(--cb-grid)"></span></span><span class="cb-value">~8%</span></div>
  <figcaption>AbstractQueuedSynchronizer.signalNext alone accounts for more samples than the actual loop body it's supposedly running underneath. The rest is CAS/state churn on the same lock (getState, setState, compareAndSetState) plus ReentrantLock$Sync.lock/tryRelease.</figcaption>
</figure>

`AbstractQueuedSynchronizer.signalNext` is more than three-quarters of the samples, and `SpinBug`'s own loop body, the code I actually wrote, is only about 15%. What's burning the core isn't idling, it's `ArrayBlockingQueue`'s internal `ReentrantLock` getting acquired and released billions of times a second by a thread that has nothing to do with the result each time. Ten threads spinning at that rate against ten separate queues still means real, measurable contention machinery running underneath something that looks, from the outside, like plain idling.

That's a more useful lesson than "spinning wastes CPU," which everyone already knows. The specific thing it's wasting CPU on is lock acquisition overhead for a lock nobody needed to take.

## Stuff worth remembering

- A non-blocking `poll()` in a hot loop doesn't just waste cycles on "nothing," it drives real lock traffic against the queue's internals every single iteration. That's `signalNext` and friends, not an empty branch.
- The fix is one word: use the blocking overload with a timeout. `queue.poll(50, TimeUnit.MILLISECONDS)` instead of `queue.poll()` took this from 91.8% to 0.2% CPU for the exact same throughput.
- Don't assume you know what a flame graph will show before you've looked. I expected an empty loop body at the top and got synchronizer internals instead.
- These are laptop numbers demonstrating the mechanism, [the lab and its flame graphs are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-high-cpu-debugging). Same switchable project as the regex bug, plus a Hibernate session that pays for changes it never made.

## The takeaway

A busy-spin loop reads as "doing nothing, fast" and the fix reads as obvious once you know it: block with a timeout instead of polling non-blocking. What the flame graph adds is the part nobody tells you in the one-line summary, that the CPU isn't idling in your code, it's fighting over a lock in code you didn't write, billions of times a second, for a queue that had one item to offer every half second. If you only fix the CPU number, you'd walk away thinking spinning burns cycles doing nothing. It's burning cycles doing something, just nothing useful, and that distinction is only visible once you actually look.
