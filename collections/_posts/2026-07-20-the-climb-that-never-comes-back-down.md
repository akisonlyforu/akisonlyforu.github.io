---
layout:     post
title:      The Climb That Never Comes Back Down
date:       2026-07-20
description:    A heap leak and a healthy busy service can look identical on a memory graph. The only number that separates them is what garbage collection gives back. So I ran four JVMs into the ground under capped limits and watched the post-GC live set climb to the ceiling, the GC-overhead tripwire fire, and two more walls, metaspace and off-heap direct buffers, that no amount of heap tuning would have fixed.
categories: java jvm memory gc
---

> Sources & further reading, since this walks over well-trodden ground: [shayne007's OOM troubleshooting guide](https://shayne007.github.io/2025/06/14/Java-OOM-Troubleshooting-Guide-Production-Best-Practices/) and [HeapHero on Java memory leaks](https://blog.heaphero.io/from-symptoms-to-solutions-troubleshooting-java-memory-leaks-outofmemoryerror/). The numbers below are my own, reproduced locally.

If you've ever watched a service stay green on every health check, answer every request, and then fall over at 4am with `java.lang.OutOfMemoryError` in the last line of the log, you already know the frustrating part: the memory graph looked fine right up until it didn't. Used heap climbs, drops, climbs, drops, the sawtooth every JVM draws. A leak draws that same sawtooth. So does a perfectly healthy service under load. The graph you're staring at cannot tell you which one you have, and that's the problem.

I wanted to see the difference with my own eyes, so I built five small programs, four that run a JVM out of memory a different way and one that deliberately survives, capped everything small enough that they die in under a minute, and read the logs on the way down. Two of them aren't heap problems at all. `OutOfMemoryError` is one error class wearing at least four different failures, and the first job when your pager goes off is figuring out which one you're actually holding.

## The problem

A leak doesn't crash. It crawls. Garbage collection runs, reclaims a little less than last time, and hands the program back a slightly higher floor to build on. Do that for long enough and the floor reaches the ceiling. By the time you get the `OutOfMemoryError` the JVM has been dying for minutes, sometimes hours, spending more and more of its life in GC for less and less room. And "used memory climbing" is not the signal, because a busy healthy service does exactly that too. The signal is what the collector manages to take *back*.

That number has a name: the live set, the bytes still reachable after a collection finishes. Peak usage barely moves between a healthy service and a leaking one. The post-GC live set is the number that gives it away.

## The tell is what the collector gives back

Here's the leak, in the one line that matters:

```java
static final List<byte[]> ROOTS = new ArrayList<>();

static void leak() {
    int block = 32 * 1024;
    while (true) {
        byte[] b = new byte[block];
        b[0] = 1; b[block - 1] = 2;   // touch it so JIT can't elide the allocation
        ROOTS.add(b);                 // <-- the leak: nothing ever removes it
    }
}
```

Then a second program, `healthy`, that allocates 32 KB blocks at the exact same rate under the exact same `-Xmx256m`, and simply doesn't keep them. Same allocation pressure, same heap, one difference: retention.

I plotted the post-GC heap (the live set after each collection) against wall-clock time for both. This is the chart the memory graph won't draw for you.

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

<figure class="cache-bench">
  <h3>Post-GC live set: leak vs. healthy, same allocation rate, same 256 MB heap</h3>
  <svg viewBox="0 0 640 250" width="100%" role="img" aria-label="Line chart of post-GC heap over time. The leak climbs from 11 MB to the 256 MB ceiling in 33 seconds; the healthy run stays flat at 10 MB.">
    <!-- ceiling -->
    <line x1="44" y1="15.4" x2="628" y2="15.4" stroke="var(--cb-orange)" stroke-width="1" stroke-dasharray="4 4" opacity="0.7"/>
    <text x="628" y="12" text-anchor="end" font-size="10" fill="var(--cb-muted)">256 MB heap ceiling</text>
    <!-- axes -->
    <line x1="44" y1="12" x2="44" y2="232" stroke="var(--cb-grid)" stroke-width="1"/>
    <line x1="44" y1="232" x2="628" y2="232" stroke="var(--cb-grid)" stroke-width="1"/>
    <!-- y labels -->
    <text x="40" y="235" text-anchor="end" font-size="10" fill="var(--cb-muted)">0</text>
    <text x="40" y="181" text-anchor="end" font-size="10" fill="var(--cb-muted)">64</text>
    <text x="40" y="127" text-anchor="end" font-size="10" fill="var(--cb-muted)">128</text>
    <text x="40" y="73" text-anchor="end" font-size="10" fill="var(--cb-muted)">192</text>
    <!-- x labels -->
    <text x="44" y="246" text-anchor="middle" font-size="10" fill="var(--cb-muted)">0s</text>
    <text x="210.9" y="246" text-anchor="middle" font-size="10" fill="var(--cb-muted)">10s</text>
    <text x="377.7" y="246" text-anchor="middle" font-size="10" fill="var(--cb-muted)">20s</text>
    <text x="544.6" y="246" text-anchor="middle" font-size="10" fill="var(--cb-muted)">30s</text>
    <!-- healthy -->
    <polyline fill="none" stroke="var(--cb-green)" stroke-width="2" points="91.7,223.5 225.1,223.5 388.0,223.5 551.8,223.5"/>
    <text x="300" y="219" font-size="10" fill="var(--cb-green)">healthy: flat at 10 MB, ran to completion</text>
    <!-- leak -->
    <polyline fill="none" stroke="var(--cb-orange)" stroke-width="2" points="47.9,222.7 67.8,215.1 87.4,207.5 107.3,199.8 127.0,192.2 146.7,184.6 166.4,177.8 186.1,170.2 205.8,162.6 224.7,155.0 244.3,147.4 264.0,139.8 283.8,132.2 303.5,124.5 323.2,117.8 342.6,110.2 362.4,102.5 382.3,94.9 401.9,88.2 421.5,80.5 441.3,72.9 461.0,65.3 480.6,58.5 499.3,50.9 519.1,43.3 538.8,35.7 558.0,28.9 577.7,21.3 587.3,17.9 589.7,17.1"/>
    <circle cx="589.7" cy="17.1" r="3" fill="var(--cb-orange)"/>
    <text x="584" y="30" text-anchor="end" font-size="10" fill="var(--cb-orange)">OOM</text>
  </svg>
  <figcaption>
    Same 32 KB blocks, same rate, same <code>-Xmx256m</code>. The healthy run's live set never leaves 10 MB across the whole run, four young collections, no growth. The leak's live set climbs one Full GC at a time, 11 MB &rarr; 254 MB, until it pins against the ceiling at 33 seconds and throws. Retention is the only difference between the two lines. Measured on Temurin 21.0.11, results in benchmarks/java-oom-anatomy/results/.
  </figcaption>
</figure>

Look at what the leak's collector is doing. Early on it runs a Full GC, `13M->11M`, and reclaims 2 MB. A third of the way in it runs `109M->109M` and reclaims nothing at all, because nothing is garbage, it's all reachable from `ROOTS`. Near the end: `254M->247M`, 7 MB back out of a full heap, and it spent 12.7 ms of stop-the-world time to get it. The collector is working the whole time and the live set never comes down, and *that*, not the height of the sawtooth, is a leak. A healthy service's post-GC floor is flat. A leaking one's floor climbs like a staircase, one collection at a time.

When it finally gives up, the stack is honest about what it was doing:

```
java.lang.OutOfMemoryError: Java heap space
	at Main.leak(Main.java:147)
	at Main.main(Main.java:105)
```

Line 147 is `ROOTS.add(b)`. A heap dump would say the same thing louder. I sampled a live class histogram with `jcmd <pid> GC.class_histogram` a moment before death:

<figure class="cache-bench">
  <h3>What was on the heap when it died (top classes by retained bytes)</h3>
  <div class="cb-panel-title">leak &mdash; live histogram just before OutOfMemoryError</div>
  <div class="cb-bar-row"><span><code>byte[]</code> (<code>[B</code>)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">240.7 MB</span></div>
  <div class="cb-bar-row"><span>G1 fillers</span><span class="cb-track"><span class="cb-fill" style="--value:2.78%;--bar:var(--cb-blue)"></span></span><span class="cb-value">6.68 MB</span></div>
  <div class="cb-bar-row"><span><code>String</code></span><span class="cb-track"><span class="cb-fill" style="--value:0.091%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.22 MB</span></div>
  <div class="cb-bar-row"><span><code>Class</code></span><span class="cb-track"><span class="cb-fill" style="--value:0.079%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.19 MB</span></div>
  <div class="cb-bar-row"><span><code>Object[]</code></span><span class="cb-track"><span class="cb-fill" style="--value:0.074%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.18 MB</span></div>
  <figcaption>
    <code>[B</code> is the JVM's shorthand for <code>byte[]</code>, and it's 252,419,016 bytes across 17,114 instances, 97% of the top-15 bytes on the heap. That's the retained blocks, the leak named in one command. In a real leak this row is your custom class, or a <code>char[]</code>, or an <code>Object[]</code> backing some cache that only ever grows. You read that top row first, everything under it is noise. Measured on Temurin 21.0.11, results in benchmarks/java-oom-anatomy/results/.
  </figcaption>
</figure>

## The tripwire before the wall: GC overhead limit exceeded

Before a leaking JVM throws `Java heap space`, it often throws something that sounds scarier and means almost the same thing. Run the near-full-heap scenario under the Parallel collector and you get this instead:

```
java.lang.OutOfMemoryError: GC overhead limit exceeded
	at Main.gcOverhead(Main.java:172)
	at Main.main(Main.java:60)
```

This is the JVM's own tripwire firing. HotSpot watches a rolling window, and if it spends more than about 98% of recent wall-clock time in GC while recovering less than 2% of the heap, it stops pretending it's making progress and throws. It's a mercy killing, the JVM refusing to spend the next hour thrashing at 1% throughput before dying anyway. I logged the collector's behavior in the final second, split into windows:

<figure class="cache-bench">
  <h3>GC overhead limit: nearly all the time in GC, almost nothing back</h3>
  <div class="cb-panels">
    <div>
      <div class="cb-panel-title">Wall time spent in GC</div>
      <div class="cb-bar-row"><span>window 1</span><span class="cb-track"><span class="cb-fill" style="--value:62.24%;--bar:var(--cb-orange)"></span></span><span class="cb-value">62.2%</span></div>
      <div class="cb-bar-row"><span>window 2</span><span class="cb-track"><span class="cb-fill" style="--value:98.02%;--bar:var(--cb-orange)"></span></span><span class="cb-value">98.0%</span></div>
      <div class="cb-bar-row"><span>window 3</span><span class="cb-track"><span class="cb-fill" style="--value:91.35%;--bar:var(--cb-orange)"></span></span><span class="cb-value">91.4%</span></div>
      <div class="cb-bar-row"><span>window 4</span><span class="cb-track"><span class="cb-fill" style="--value:96.77%;--bar:var(--cb-orange)"></span></span><span class="cb-value">96.8%</span></div>
      <div class="cb-bar-row"><span>window 5</span><span class="cb-track"><span class="cb-fill" style="--value:88.75%;--bar:var(--cb-orange)"></span></span><span class="cb-value">88.8%</span></div>
      <div class="cb-bar-row"><span>window 6</span><span class="cb-track"><span class="cb-fill" style="--value:93.99%;--bar:var(--cb-orange)"></span></span><span class="cb-value">94.0%</span></div>
    </div>
    <div>
      <div class="cb-panel-title">Heap reclaimed that window</div>
      <div class="cb-bar-row"><span>window 1</span><span class="cb-track"><span class="cb-fill" style="--value:0.779%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.78%</span></div>
      <div class="cb-bar-row"><span>window 2</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.00%</span></div>
      <div class="cb-bar-row"><span>window 3</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.00%</span></div>
      <div class="cb-bar-row"><span>window 4</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.00%</span></div>
      <div class="cb-bar-row"><span>window 5</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.00%</span></div>
      <div class="cb-bar-row"><span>window 6</span><span class="cb-track"><span class="cb-fill" style="--value:1.484%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.48%</span></div>
    </div>
  </div>
  <figcaption>
    Each window is roughly 38 ms of the final second. The collector is running essentially non-stop, 94.0% of the last window's wall time, and giving back 1.5% of the heap for it. The right-hand panel is on the same 0-to-100% scale as the left on purpose: those aren't small bars, they're basically empty. That ratio is the definition of the error. Measured on Temurin 21.0.11 under <code>-XX:+UseParallelGC</code>, results in benchmarks/java-oom-anatomy/results/.
  </figcaption>
</figure>

Worth knowing: which of the two you get is a matter of timing, not a matter of two different bugs. Fill the heap a little faster and it runs out cleanly and says `Java heap space`; let it thrash right at the edge and the overhead heuristic trips first and it says `GC overhead limit exceeded`. Reproducing the overhead-limit message reliably took filling to 80% rather than 90%, and even then the harness retries a couple of times, that shape is genuinely lumpy. Don't treat the two messages as different diagnoses. They're the same leak or the same undersized heap, caught at slightly different moments.

## The OutOfMemoryError that isn't about the heap

Now the one that catches people, because every reflex you built above is wrong for it. You get `OutOfMemoryError`, you pull the heap dump, and the heap is nearly empty. Bumping `-Xmx` does nothing. Because it was never the heap:

```
java.lang.OutOfMemoryError: Metaspace
	at java.base/java.lang.ClassLoader.defineClass0(Native Method)
	...
	at Main.main(Main.java:126)
```

Metaspace is where the JVM keeps class metadata, the runtime shape of every class it has loaded. It lives in native memory, outside the heap, and it has its own ceiling (`-XX:MaxMetaspaceSize`). You fill it not by allocating objects but by loading *classes*, and the classic way to leak it is to keep making new ones: a fresh classloader per request, dynamic proxies, a scripting engine, a redeploy that never lets the old classloader die. I reproduced it by spinning up thousands of throwaway classloaders, each defining one more class, and watched the heap sit still while the metadata wall came up to meet it:

<figure class="cache-bench">
  <h3>Metaspace death: the heap had 91% free and the JVM still died</h3>
  <div class="cb-bar-row"><span>Heap used</span><span class="cb-track"><span class="cb-fill" style="--value:9.4%;--bar:var(--cb-green)"></span></span><span class="cb-value">24 / 256 MB</span></div>
  <div class="cb-bar-row"><span>Metaspace</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">at the wall</span></div>
  <figcaption>
    Across the whole run the heap never left the 12-to-24 MB band, under 10% of its 256 MB budget, while ~10,600 loaded classes drove metadata from 1 MB to the 64 MB cap. If you were staring at heap usage you saw a perfectly calm graph the entire time it was dying. The fix here isn't a bigger heap, it's finding the classloader that never gets collected. Measured on Temurin 21.0.11 with <code>-XX:MaxMetaspaceSize=64m</code>, results in benchmarks/java-oom-anatomy/results/.
  </figcaption>
</figure>

## The one a heap dump can't see

Metaspace at least shows up in the JVM's own memory pools, so a monitoring dashboard has a chance of catching it. The next one doesn't show up anywhere you'd normally look. Direct byte buffers, the ones NIO uses under every socket and file channel and most serious network libraries, keep a small wrapper object on the heap and the actual bytes in native memory the OS handed out. Those bytes only come back when the wrapper becomes unreachable and its `Cleaner` runs. Hold on to the wrapper and the native memory is pinned for good:

```java
static final List<ByteBuffer> BUFFERS = new ArrayList<>();
...
ByteBuffer b = ByteBuffer.allocateDirect(512 * 1024);
BUFFERS.add(b);   // <-- retain the wrapper: the Cleaner never runs
```

128 buffers of 512 KB each under `-XX:MaxDirectMemorySize=64m`, dead in 3.1 seconds. The message is worth reading closely, because it is not the short `Direct buffer memory` string that most older write-ups quote. Modern JDKs hand you the arithmetic instead:

```
java.lang.OutOfMemoryError: Cannot reserve 524288 bytes of direct buffer memory (allocated: 66584626, limit: 67108864)
	at java.base/java.nio.Bits.reserveMemory(Bits.java:178)
	at java.base/java.nio.DirectByteBuffer.<init>(DirectByteBuffer.java:111)
	at java.base/java.nio.ByteBuffer.allocateDirect(ByteBuffer.java:360)
	at Main.directBuffer(Main.java:77)
```

<figure class="cache-bench">
  <h3>Direct buffer memory: the heap was 95% empty when it died</h3>
  <div class="cb-bar-row"><span>Heap used</span><span class="cb-track"><span class="cb-fill" style="--value:4.7%;--bar:var(--cb-green)"></span></span><span class="cb-value">12 / 256 MB</span></div>
  <div class="cb-bar-row"><span>Direct memory</span><span class="cb-track"><span class="cb-fill" style="--value:98.4%;--bar:var(--cb-orange)"></span></span><span class="cb-value">63 / 64 MB</span></div>
  <figcaption>
    Across all 127 samples the heap never left the 11-to-12 MB band while direct memory climbed 0 &rarr; 63 MB into its cap. Now the nasty part: take a heap dump of this process and you'll find roughly 128 small <code>DirectByteBuffer</code> objects adding up to a few kilobytes. The 63 MB that actually killed it is not in the dump, because it was never on the heap. Measured on Temurin 21.0.11, results in benchmarks/java-oom-anatomy/results/.
  </figcaption>
</figure>

Two more you'll eventually meet: `unable to create new native thread`, when the OS won't hand you another thread, and `Requested array size exceeds VM limit`, when something computed a nonsense array length. None of these are fixed by tuning the heap, because none of them are the heap. Read the first word after the colon before you reach for a heap flag.

## Reading the evidence

Put them together and the first question on the pager is never "how do I get more memory," it's "which OutOfMemoryError is this":

| Message | Where it ran out | What actually fixes it |
|---|---|---|
| `Java heap space` | The heap | Find the growing live set, usually a collection that only ever adds. A heap dump names it. |
| `GC overhead limit exceeded` | The heap (caught earlier) | Same as above. It's a leak or an undersized heap, seen mid-thrash, not a separate bug. |
| `Metaspace` | Class metadata, off-heap | Find the classloader that won't die. A bigger `-Xmx` is wasted. |
| `Cannot reserve N bytes of direct buffer memory` | Native memory, off-heap | Find what's retaining the `ByteBuffer` wrappers, or raise `-XX:MaxDirectMemorySize`. A heap dump won't show you the bytes. |
| `unable to create new native thread` / `Requested array size exceeds VM limit` | OS limits, or a bad computed size | A thread leak, or a length someone calculated wrong. Neither is a heap problem. |

And the tell that separates a real leak from a service that's just busy is the same in every heap case: watch the post-GC live set, the floor after each collection, not the peak. A flat floor under load is a healthy JVM doing its job. A floor that climbs collection after collection while the collector reclaims less each time is a leak, and it will reach the ceiling on its own schedule whether or not you're watching.

## The takeaway

Turn on the GC log before you need it. `-Xlog:gc*:file=gc.log:time,level,tags` costs almost nothing and it's the difference between reading the death spiral and guessing at it after the fact. Add `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=...` so the JVM hands you the evidence on its way out, then open the dump in something that shows a dominator tree and read the top row. And when the graph looks fine but the service keeps dying, stop looking at peak memory and look at what garbage collection gives back, because a leak and a healthy load look identical until you do.

The harness that produced all of this, four JVMs run into the ground and one held up next to them as a control, with the GC logs and histograms and stack traces captured, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-oom-anatomy). These are laptop numbers with tiny heaps chosen to fail fast, not capacity or tuning advice, the shape is what carries over, not the seconds on the clock.
