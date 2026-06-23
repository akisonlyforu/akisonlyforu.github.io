---
layout:     post
title:      The Slowest Request Was a Garbage Collection
date:       2025-04-23
description:    p50 and p99 flat, and then one request in the trace takes 136 milliseconds. Nobody sent a slow request. The JVM stopped every thread to collect garbage, and the fix was one flag that traded half the throughput for a tail that never freezes.
categories: jvm gc latency java performance
---

If you've ever stared at a latency trace where p50 and p99 look boring and healthy, and then a single request sits there at 136ms with no slow query, no lock, no downstream call to blame, this one is for you. I chased that shape for a while before I accepted the boring answer: the request wasn't slow. The whole JVM stopped, mid-request, to collect garbage, and my request just happened to be holding the thread when it did.

The thing about a stop-the-world pause is it doesn't show up as a slow anything. It shows up as everything in flight freezing at once, and then the tail latency eats the bill. Your service didn't do slow work. It did no work, for 136 milliseconds, because the garbage collector had the floor.

## The problem

A JVM service under steady allocation load will, sooner or later, stop every application thread and reclaim memory. How long it stops depends almost entirely on which collector you picked and how much live data it has to walk. The default throughput collectors are tuned to reclaim the most memory per CPU-second, which is a great thing to optimize right up until one of those collections lands inside a request you cared about and freezes it for a tenth of a second. The pause isn't in your code, it isn't in your profiler's flame graph as *your* time, and it doesn't move when you optimize your handler. It's the runtime, and the only knob that moves it is the collector.

So I built the same service three times, changed exactly one flag each time, and measured what came out the tail.

## The workload

One process, one heap, one loop. It keeps a bounded cache of `byte[]` payloads live in the old generation, and each "request" allocates a few KB of short-lived garbage, does a little work, and every few requests swaps out a cache entry so some medium-lived objects get promoted. Steady allocation, steady promotion: the shape a real request handler makes. I timed every single request with `System.nanoTime` into an allocation-free histogram, and separately parsed the actual stop-the-world pauses out of `-Xlog:gc`. The two agree to the millisecond: the request `max` is the GC pause, sitting inside a request.

Heap fixed at 4g, live set around 2.5GB, two million requests after warmup, median of three runs. The only variable is the collector.

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

<figure class="cache-bench">
  <h3>Same service, same heap, one flag changed</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Longest stop-the-world pause</p>
      <div class="cb-bar-row"><span>ParallelGC</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">136 ms</span></div>
      <div class="cb-bar-row"><span>G1GC</span><span class="cb-track"><span class="cb-fill" style="--value:83.76%;--bar:var(--cb-orange)"></span></span><span class="cb-value">114 ms</span></div>
      <div class="cb-bar-row"><span>ZGC (gen)</span><span class="cb-track"><span class="cb-fill" style="--value:0.41%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.55 ms</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Throughput</p>
      <div class="cb-bar-row"><span>ParallelGC</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">196,768</span></div>
      <div class="cb-bar-row"><span>G1GC</span><span class="cb-track"><span class="cb-fill" style="--value:97.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">191,255</span></div>
      <div class="cb-bar-row"><span>ZGC (gen)</span><span class="cb-track"><span class="cb-fill" style="--value:52.71%;--bar:var(--cb-green)"></span></span><span class="cb-value">103,713</span></div>
    </div>
  </div>
  <figcaption>Max GC pause and throughput in ops/s. ParallelGC froze for 136ms, G1 for 114ms, generational ZGC for 0.55ms, about 250× less. ZGC pays for it: roughly half the throughput. Measured on Temurin 21.0.11, 4g heap, ~2.5GB live, median of 3 runs, results in benchmarks/java-gc-tuning/results/collector_comparison.csv.</figcaption>
</figure>

The left panel is the whole story. ParallelGC's worst pause was 136ms and G1's was 114ms, and in both cases the slowest request in the trace *was* that pause: 136.5ms for Parallel, 117ms for G1, because the request unlucky enough to be running when the collector kicked in ate the entire freeze. Generational ZGC's worst pause was 0.55 milliseconds. Same code, same load, same heap. The tail dropped by two orders of magnitude on a single flag.

And then the honest part, which is the right panel. ZGC did not give you that for free. It pushed about 103,713 ops/s against ParallelGC's 196,768, call it half the throughput. It also has a *worse* typical latency: ZGC's p99.9 request was 364µs, while ParallelGC's p99.9 was 35µs and G1's was 16µs. For the ordinary request, the throughput collectors are faster. ZGC's entire pitch is the one number the others can't promise: the tail never becomes a 100ms freeze. It's slower on average and never catastrophic at the edge, and that trade is either exactly what you want or exactly what you don't, depending on whether you're paid for average throughput or for a p99.9 SLA.

One more caveat I have to put in writing, because the ratio is real but the absolute number depends on scale. Those hundred-millisecond pauses only show up because the live set is large, 2.5GB of stuff the collector has to walk. At a plain 512m heap with a ~188MB live set, the same three collectors gave 22ms, 27ms and 0.15ms. The ratio is still there (~150×), but 25ms is a shrug, not a stall. The bigger your live heap, the longer the throughput collectors have to stop the world to trace it, and the more that one flag is worth. Both regimes are in the repo; I shipped the dramatic one and parked the mild one under `results/attempts/`.

## When the heap itself is the problem

The collector is one axis. The other is how much room you gave it. A collector can only do its job if there's headroom above your live set: the gap between what you keep alive and `-Xmx` is the runway the GC has to work in. Squeeze that gap and the collector has to run more often to keep up, and "more often" is not free, it's CPU you're spending on bookkeeping instead of requests.

Same workload, same collector this time (G1 throughout) and I only moved `-Xmx`. Live set around 141MB, four million requests.

<figure class="cache-bench">
  <h3>G1, same load, only the heap size changes</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Time spent in GC</p>
      <div class="cb-bar-row"><span>-Xmx256m</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">78.2%</span></div>
      <div class="cb-bar-row"><span>-Xmx320m</span><span class="cb-track"><span class="cb-fill" style="--value:87.10%;--bar:var(--cb-orange)"></span></span><span class="cb-value">68.1%</span></div>
      <div class="cb-bar-row"><span>-Xmx384m</span><span class="cb-track"><span class="cb-fill" style="--value:80.90%;--bar:var(--cb-orange)"></span></span><span class="cb-value">63.3%</span></div>
      <div class="cb-bar-row"><span>-Xmx512m</span><span class="cb-track"><span class="cb-fill" style="--value:70.07%;--bar:var(--cb-orange)"></span></span><span class="cb-value">54.8%</span></div>
      <div class="cb-bar-row"><span>-Xmx1g</span><span class="cb-track"><span class="cb-fill" style="--value:45.47%;--bar:var(--cb-blue)"></span></span><span class="cb-value">35.6%</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Throughput</p>
      <div class="cb-bar-row"><span>-Xmx256m</span><span class="cb-track"><span class="cb-fill" style="--value:33.52%;--bar:var(--cb-orange)"></span></span><span class="cb-value">237,667</span></div>
      <div class="cb-bar-row"><span>-Xmx320m</span><span class="cb-track"><span class="cb-fill" style="--value:46.65%;--bar:var(--cb-blue)"></span></span><span class="cb-value">330,771</span></div>
      <div class="cb-bar-row"><span>-Xmx384m</span><span class="cb-track"><span class="cb-fill" style="--value:62.97%;--bar:var(--cb-blue)"></span></span><span class="cb-value">446,446</span></div>
      <div class="cb-bar-row"><span>-Xmx512m</span><span class="cb-track"><span class="cb-fill" style="--value:66.66%;--bar:var(--cb-blue)"></span></span><span class="cb-value">472,611</span></div>
      <div class="cb-bar-row"><span>-Xmx1g</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">709,008</span></div>
    </div>
  </div>
  <figcaption>Percentage of wall-clock spent in GC, and throughput in ops/s, as the heap shrinks toward the live set. At 256m the JVM spent 78% of its life collecting garbage and pushed a third of the throughput it managed at 1g. G1, ~141MB live, median of 3 runs, results in benchmarks/java-gc-tuning/results/heap_sizing.csv.</figcaption>
</figure>

At 1g the service spent about a third of its time in GC and did 709,008 ops/s. Shrink the heap toward the live set and it gets worse every step: 512m, 384m, 320m, and at 256m the JVM was spending 78.2% of its wall-clock collecting garbage and running 237,667 ops/s, roughly a third of the 1g throughput, for the same work. The tail moved with it: p99.9 went from 11µs at 1g to 353µs at 256m, a 32× jump, entirely from GC frequency. Nothing changed about the requests. The heap was just too tight, so the collector ran constantly, and constant collection is a tax you pay on every request whether it allocates much or not.

This is the failure that hides behind an out-of-memory panic. The service doesn't crash. It survives, technically, at a heap you thought was fine because it never actually OOMs. It just quietly burns most of its CPU on garbage collection and does a third of the work, and if you're only watching for `OutOfMemoryError` you'll never see it. The heap never ran out of memory, so nothing alerted, it just ran hot and slow the whole time.

## The takeaway

Two knobs, and they're the ones you set before you write a line of handler code. The collector decides the *shape* of your tail: ParallelGC and G1 will trace your whole live set in one stop-the-world sweep and freeze a request for a hundred-plus milliseconds doing it, generational ZGC won't stop for more than about a millisecond but hands you roughly half the throughput and a worse average to buy that. If you're paid for a p99.9 SLA, that's a good trade and you should take it; if you're paid for raw throughput and nobody's watching the tail, it's a bad one. Pick on purpose, not by default.

The heap size decides whether the collector you picked even has room to work. Leave real headroom above your live set: the closer `-Xmx` gets to what you keep alive, the more of your CPU goes to collection instead of requests, and a JVM at 78% time-in-GC is a service that's technically up and mostly not working. It never throws the error that would tell you.

And the thing I'd tattoo on the trace if I could: your slowest request is often not a request. Before you profile your own code for a tail spike that has no slow work in it, check whether the runtime stopped the world underneath you. The [harness, the Java workload, the parsed GC logs and every CSV are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-gc-tuning). These are laptop numbers (Temurin 21.0.11, arm64, 10 cores) meant to show the mechanism, not to size your fleet. The mechanism transfers, the absolute milliseconds won't, so go measure your own before you change a flag in production.
