---
layout:     post
title:      The JVM That Was Up and Doing Nothing
date:       2026-07-20
description:    Why a JVM stuck in a garbage collection death spiral passes every health check, why the JVM's own OOM guards don't fire, how Netflix's jvmquake catches it with a debt counter, and what Uber's GC tuning numbers actually teach.
categories: java jvm garbage-collection reliability
---

*This one is theory, not my own benchmark. It's my reading of two posts: Netflix's [Introducing jvmquake](https://netflixtechblog.medium.com/introducing-jvmquake-ec944c60ba70) and Uber's [JVM Tuning for Large Scale Services](https://www.uber.com/us/en/blog/jvm-tuning-garbage-collection/). Every number below is theirs, not mine. I'm crediting them up front and drawing my own diagrams so I'm not lifting anyone's images.*

There's a specific kind of outage that ruins your afternoon, and it's not the crash. The crash is easy. The process dies, the supervisor restarts it, the load balancer pulls it out, and you go read the log. The bad one is the node that stays up. It answers the health check. Its port is open. Its CPU is pinned at 100%. And it is doing approximately nothing, and it will keep not doing anything until somebody notices and kills it by hand.

## The problem

A JVM in a garbage collection death spiral is technically alive. The heap is nearly full, every collection reclaims almost nothing, so the collector runs again immediately, and the application threads get a sliver of time between pauses. Netflix's description of what this does to a datastore node is the line I keep coming back to: throughput "has, typically, decreased by four orders of magnitude." Not degraded. Four orders of magnitude. Their real example was a JVM taking repeated 20-second-plus GC pauses with almost no work between them. Meanwhile nothing in the JVM decides this is a failure, because from the JVM's point of view nothing has failed. It hasn't run out of memory. It found memory. It just spent twenty seconds finding it, and it's about to do it again.

## Why the guards you already have don't fire

The instinct is that the JVM must already handle this, and it half does. There's a family of flags for it: `GCHeapFreeLimit`, `GCTimeLimit`, `OnOutOfMemoryError`, `ExitOnOutOfMemoryError`, `CrashOnOutOfMemoryError`. Netflix evaluated the lot and their conclusion is worth quoting flat, because it saved me from a week of tuning them myself: they "either do not work consistently on all JVMs and garbage collectors, are hard to tune or understand, or they simply don't work in various edge cases."

The deeper reason is that all of them are keyed to *running out of memory*, and a death spiral is not running out of memory. It's a grey failure, the space between healthy and dead where the process is technically doing its job and practically useless. Every guard in that list watches the wrong end of the curve.

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
  <h3>The gap those flags leave open</h3>
  <svg viewBox="0 0 720 190" width="100%" role="img" aria-label="A horizontal band showing GC time as a share of wall clock. Zero to fifty percent is labelled healthy, fifty to one hundred percent is a wide orange band labelled grey failure where the process is alive but useless, and only the far right edge at one hundred percent is red and labelled out of memory, the only point where the JVM's own guards fire.">
    <rect x="20" y="60" width="330" height="46" rx="3" fill="#23856d"></rect>
    <text x="185" y="89" text-anchor="middle" font-size="13" fill="#ffffff">healthy: debt trends to zero</text>
    <rect x="352" y="60" width="316" height="46" rx="3" fill="#d65f3c"></rect>
    <text x="510" y="83" text-anchor="middle" font-size="13" fill="#ffffff">grey failure: alive, useless</text>
    <text x="510" y="99" text-anchor="middle" font-size="11" fill="#ffffff">nothing in the JVM fires here</text>
    <rect x="670" y="60" width="30" height="46" rx="3" fill="#7b1d1d"></rect>
    <line x1="20" y1="118" x2="700" y2="118" stroke="#999999"></line>
    <text x="20" y="136" font-size="11" fill="#666666">0%</text>
    <text x="352" y="136" text-anchor="middle" font-size="11" fill="#666666">50%</text>
    <text x="700" y="136" text-anchor="end" font-size="11" fill="#666666">100%</text>
    <text x="360" y="154" text-anchor="middle" font-size="12" fill="#666666">share of wall clock spent in GC →</text>
    <text x="685" y="46" text-anchor="end" font-size="11" fill="#666666">OOM: the only point the flags watch ↓</text>
    <text x="20" y="34" font-size="13" fill="#666666">the whole band is time your process is "up"</text>
  </svg>
  <figcaption>My own drawing. The point is the width of the orange: a process can spend anywhere from half to nearly all of its wall clock collecting garbage without ever triggering an out-of-memory condition, and that entire range looks identical to a TCP health check.</figcaption>
</figure>

## GC time as a debt you can go bankrupt on

jvmquake's idea is the part worth stealing even if you never run their tool. Stop asking "is the heap full." Ask "is this process paying its GC bill," and track that as a running balance.

It works like a leaky bucket. Hook the JVMTI callbacks `GarbageCollectionStart` and `GarbageCollectionFinish` so you can see every pause, then:

- every millisecond spent in GC **adds** a millisecond of debt,
- every millisecond the application actually runs **pays down** debt,
- debt floors at zero, it never goes negative.

That's it. And the reason it works is a piece of arithmetic that clicks the moment you see it. A JVM that spends less than half its time in GC pays down faster than it borrows, so the balance trends to zero and stays there forever, no matter how long the process runs. A JVM that spends more than half its time in GC borrows faster than it pays, so the balance trends to infinity. There's no tuning to get right, no threshold to guess per service. The 50% line falls out of the algorithm on its own, and a healthy process sits on the safe side of it by a mile.

Then you put a cap on the balance. Default is 30 seconds of accumulated debt, checked after each collection finishes. Cross it and the process gets killed, on the grounds that anything that has fallen 30 seconds behind is not coming back.

<figure class="cache-bench">
  <h3>Two JVMs, same counter</h3>
  <svg viewBox="0 0 720 260" width="100%" role="img" aria-label="A line chart of accumulated GC debt over time. The healthy JVM's line stays flat along the zero baseline, ticking up slightly at each collection and immediately paying back down. The spiralling JVM's line is a rising staircase that climbs steadily past the thirty second kill threshold near the right edge.">
    <line x1="62" y1="214" x2="700" y2="214" stroke="#999999"></line>
    <line x1="62" y1="214" x2="62" y2="30" stroke="#999999"></line>
    <line x1="62" y1="169" x2="700" y2="169" stroke="#cccccc" stroke-dasharray="2 4"></line>
    <line x1="62" y1="124" x2="700" y2="124" stroke="#cccccc" stroke-dasharray="2 4"></line>
    <text x="56" y="218" text-anchor="end" font-size="11" fill="#666666">0s</text>
    <text x="56" y="173" text-anchor="end" font-size="11" fill="#666666">10s</text>
    <text x="56" y="128" text-anchor="end" font-size="11" fill="#666666">20s</text>
    <text x="56" y="83" text-anchor="end" font-size="11" fill="#666666">30s</text>
    <line x1="62" y1="79" x2="700" y2="79" stroke="#d65f3c" stroke-width="1.5" stroke-dasharray="6 4"></line>
    <text x="700" y="72" text-anchor="end" font-size="11" fill="#d65f3c">kill threshold (default 30s of debt)</text>
    <polyline fill="none" stroke="#d65f3c" stroke-width="2" points="62,214.0 62,203.2 76,203.2 76,205.7 94,205.7 94,194.9 108,194.9 108,197.3 126,197.3 126,186.6 140,186.6 140,189.0 158,189.0 158,178.2 172,178.2 172,180.7 190,180.7 190,169.9 204,169.9 204,172.4 222,172.4 222,161.6 236,161.6 236,164.1 253,164.1 253,153.2 268,153.2 268,155.7 285,155.7 285,144.9 300,144.9 300,147.4 317,147.4 317,136.6 332,136.6 332,139.1 349,139.1 349,128.3 363,128.3 363,130.8 381,130.8 381,120.0 395,120.0 395,122.4 413,122.4 413,111.6 427,111.6 427,114.1 445,114.1 445,103.3 459,103.3 459,105.8 477,105.8 477,95.0 491,95.0 491,97.5 509,97.5 509,86.7 523,86.7 523,89.1 540,89.1 540,78.3 555,78.3 555,80.8 572,80.8 572,70.0 587,70.0 587,72.5 604,72.5 604,61.7 619,61.7 619,64.2 636,64.2 636,53.4 651,53.4 651,55.8 668,55.8 668,45.0 682,45.0 682,47.5 700,47.5"></polyline>
    <polyline fill="none" stroke="#23856d" stroke-width="2" points="62,214.0 62,209.9 76,209.9 76,214.0 94,214.0 94,209.9 108,209.9 108,214.0 126,214.0 126,209.9 140,209.9 140,214.0 158,214.0 158,209.9 172,209.9 172,214.0 190,214.0 190,209.9 204,209.9 204,214.0 222,214.0 222,209.9 236,209.9 236,214.0 253,214.0 253,209.9 268,209.9 268,214.0 285,214.0 285,209.9 300,209.9 300,214.0 317,214.0 317,209.9 332,209.9 332,214.0 349,214.0 349,209.9 363,209.9 363,214.0 381,214.0 381,209.9 395,209.9 395,214.0 413,214.0 413,209.9 427,209.9 427,214.0 445,214.0 445,209.9 459,209.9 459,214.0 477,214.0 477,209.9 491,209.9 491,214.0 509,214.0 509,209.9 523,209.9 523,214.0 540,214.0 540,209.9 555,209.9 555,214.0 572,214.0 572,209.9 587,209.9 587,214.0 604,214.0 604,209.9 619,209.9 619,214.0 636,214.0 636,209.9 651,209.9 651,214.0 668,214.0 668,209.9 682,209.9 682,214.0 700,214.0"></polyline>
    <text x="640" y="40" text-anchor="end" font-size="12" fill="#d65f3c">GC &gt; runtime: debt → ∞</text>
    <text x="360" y="240" text-anchor="middle" font-size="12" fill="#23856d">GC &lt; runtime: debt → 0, forever</text>
    <text x="360" y="256" text-anchor="middle" font-size="11" fill="#666666">collections over time →</text>
  </svg>
  <figcaption>My own drawing of the algorithm, not a captured trace. The green line is what every healthy JVM you own looks like on this counter: each collection nudges the balance up, the next stretch of real work wipes it out, and it sits on the floor indefinitely. The red line never gets a stretch of real work long enough to pay back what the last pause cost.</figcaption>
</figure>

There's a knob for the ratio, `runtime_weight`. Set it to 2 and application runtime pays down debt twice as fast, which moves the break-even line from 50% throughput to 33%. That's the whole tuning surface: one number saying how bad you'll let it get before you call it dead.

## Killing it is the easy half

Once you've decided the process is gone, you have a much more interesting problem: this is the only moment you will ever have a live, in-memory picture of whatever caused the spiral, and if you just `SIGKILL` it you have thrown that away and you will see the bug again next week.

jvmquake handles that in a way I found genuinely clever. Two options, depending on what you want out of the corpse.

The first is a heap dump, and rather than plumbing a new dump path it goes through the front door the JVM already has. It deliberately allocates giant arrays until the JVM throws a real `OutOfMemoryError`, which trips the `-XX:+HeapDumpOnOutOfMemoryError` flag you already set, and the JVM writes the heap dump itself. Induce the failure the existing machinery already knows how to document. No new code path to keep working.

The second is a full core dump, which is strictly better for diagnosis and strictly worse for your disk. Send `SIGABRT`, the kernel writes the core. Netflix's problem was that these cores are enormous and the box doesn't have room, so they don't land them on disk at all: a script compresses and pipes the core straight to S3, and systemd restarts the process while the upload is still going. They report they "reliably upload 16GB core dumps in less than two minutes." The node is back in rotation before the evidence has finished uploading.

That's the part I'd argue matters more than the detector. If the kill leaves you nothing to read, you've bought back a few minutes of availability and learned nothing, and the same bug is on the calendar for next week. Netflix says jvmquake "mitigated dozens of incidents, each time in mere minutes," and separately that the core dumps let them chase down real bugs in Cassandra and Elasticsearch offline. Those are two different wins and only the second one compounds.

## The other direction: not dying in the first place

Uber's post is the mirror image. Same enemy, opposite end. Instead of "how do I detect a JVM that's already lost," it's "what actually moves GC pauses on a heap the size of a small car."

Their trigger point is a number worth writing down: GC tuning becomes worth your time when pauses consistently exceed **100ms**. Above that you're paying for it in throughput and reliability. Below it, go do something else.

The result I like best is the counter-intuitive one. On the HDFS NameNode they raised the heap from 120GB to 160GB, which should help, and ParNew pause times went **up about 35%**. More memory, worse pauses. The reason is that they'd grown the total heap without growing the young generation, so the old generation got bigger while the young gen stayed at 7.4GB, and the young collection has to scan old-gen references to find what's still pointing into the young space. Bigger old gen, more scanning, longer "young" pauses. The fix was to raise young gen to 16GB alongside the heap and set `-XX:ParGCCardsPerStrideChunk=32k` to chunk that scan better.

<figure class="cache-bench">
  <h3>HDFS NameNode: -Xmx160g -Xmn7.4g vs -Xmx160g -Xmn16g -XX:ParGCCardsPerStrideChunk=32k</h3>
  <div class="cb-panel-title">max GC pause</div>
  <div class="cb-bar-row"><span>baseline</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">22 s</span></div>
  <div class="cb-bar-row"><span>tuned</span><span class="cb-track"><span class="cb-fill" style="--value:6.8%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.5 s</span></div>
  <div class="cb-panel-title" style="margin-top:0.9rem;">RPC queue average time</div>
  <div class="cb-bar-row"><span>baseline</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">&gt;500 ms</span></div>
  <div class="cb-bar-row"><span>tuned</span><span class="cb-track"><span class="cb-fill" style="--value:80%;--bar:var(--cb-green)"></span></span><span class="cb-value">~400 ms</span></div>
  <div class="cb-panel-title" style="margin-top:0.9rem;">RPC operations served</div>
  <div class="cb-bar-row"><span>baseline</span><span class="cb-track"><span class="cb-fill" style="--value:66.7%;--bar:var(--cb-orange)"></span></span><span class="cb-value">8,000</span></div>
  <div class="cb-bar-row"><span>tuned</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">12,000</span></div>
  <figcaption>Bars are scaled to the larger value in each pair. Numbers reported in Uber's post linked at the top, not measured by me. The max-pause bar is the one to look at: 22 s to 1.5 s, from sizing the young generation to match the heap it lives in.</figcaption>
</figure>

Uber's rule of thumb from that: **young gen is typically 20 to 50% of total heap**, more if your service allocates heavily, and when you grow the heap you grow the young gen with it. And size the total heap about 20% above the maximum live footprint you see in verbose GC logs, so a full GC that reclaims almost nothing doesn't immediately trigger the next one. That cascade, full GCs freeing too little and instantly re-firing, is exactly the death spiral from the top of this post, seen from the tuning side. On their Presto coordinator the fix for it was unglamorous: add 10% more heap and the cascade stopped.

## The GC problem that wasn't a GC problem

The Hive Metastore story is the one I'd tell a junior engineer, because it's the trap. API latencies went from under 100ms to 2 to 4 seconds. GC logs looked awful: 2,258 collections averaging ~177ms, heap sawtoothing violently. Every instinct says tune the collector.

The actual cause was a metrics collector daemon with its backoff set to 1 millisecond instead of 1 second. A thousand mbeans calls a second, generating garbage at roughly 400 Mbps. Nobody's collector survives that. They fixed the one wrong constant and collections dropped from 2,258 to 143.

<figure class="cache-bench">
  <h3>Hive Metastore: GC events, before and after fixing a 1ms backoff</h3>
  <div class="cb-bar-row"><span>1 ms backoff</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">2,258</span></div>
  <div class="cb-bar-row"><span>1 s backoff</span><span class="cb-track"><span class="cb-fill" style="--value:6.3%;--bar:var(--cb-green)"></span></span><span class="cb-value">143</span></div>
  <figcaption>Same code, same collector, same heap. Numbers reported in Uber's post linked at the top. The allocation rate was the bug, GC was just the thing reporting it.</figcaption>
</figure>

I've done the version of this where you spend two days on GC flags for a problem that was one line of application code, and the tell is always the same: the allocation rate is absurd for what the service is supposed to be doing. Check that the traffic into the heap makes sense before you tune what's cleaning up after it.

Their other findings in the same spirit. On the Presto coordinator, string deduplication was costing 6.59% of runtime in GC pauses; turning it off dropped that to 3% and weekly errors fell from 2.5% to 0.73%. And of all the parameters they swept, only one or two per service actually moved anything. `TLABSize`, `ConcGCThreads` and friends did essentially nothing. Sweeping twenty flags is how you convince yourself you're working.

They also ran Azul's C4 collector against CMS for the large-heap case, and it's a real result with a real price: ~17ms RPC queue latency versus ~24ms, and pauses that stay flat even at 650GB heaps rather than growing with the heap. The price is about 150GB of extra off-heap memory for a 200GB heap. That's the honest shape of the modern pauseless collectors, ZGC and Shenandoah included. You're buying flat pauses with memory and CPU.

## The takeaway

Two halves of the same problem, and you want both.

Detection first, because it's cheap and you probably have nothing. Your health checks almost certainly cannot tell a working JVM from one that's spending 95% of its wall clock in GC, and neither can `ExitOnOutOfMemoryError`, because that failure never reaches an out-of-memory condition. The debt counter is the fix and the idea travels: GC time borrows, application time repays, floor at zero, kill above a cap. Under 50% GC time it self-corrects, over 50% it runs away, and you didn't have to pick a threshold for that.

Then tuning, in this order. Check the allocation rate before the collector, because the Metastore bug was a wrong constant and the flags would never have found it. Size the young generation as a fraction of the heap, not as a fixed number you forget to revisit when the heap grows, or you'll get Uber's 35% pause regression from an upgrade that was supposed to help. Leave the heap ~20% of headroom above live footprint so a full GC that frees nothing doesn't immediately call the next one. And expect one or two flags to matter, not twenty.

The last thing, the one worth building before you need it: whatever kills the sick process must leave a body. A restart with no heap dump and no core is the same incident again next week, and you'll have learned nothing except how to restart it faster.
