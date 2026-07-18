---
layout:     post
title:      More Connections, Slower Reads
date:       2025-07-17
description:    A read-heavy database falls behind, so you add app instances and raise max_connections to let more callers in. Throughput barely moves and the p99 goes to 1.7 seconds. The same 512 callers over a pool of 8 backend connections answered at 42ms. Here's the reproduction.
categories: mysql connection-pooling databases performance
---

The read database is falling behind, so you scale the thing that reads from it. More app instances, more workers per instance, and because each of those wants to talk to the database you bump `max_connections` up so nobody gets turned away. It feels right, more capacity on both ends, and for a while the graphs even nod along. Then the p99 starts climbing, and it keeps climbing, and the throughput you added all those connections to buy never really shows up. You have more connections open to MySQL than you have ever had, and reads are slower than when you had a tenth of them.

I didn't believe it either until I watched the number go the wrong way, so I built a small harness and made it happen on purpose.

## The problem

A connection to MySQL is not free, and it is not just a socket. Every connection is a server-side thread, and every thread that is actually *running* a query wants a CPU, a slice of the buffer pool, and its turn on the same internal locks everyone else is holding. You have a fixed number of cores. Once the number of queries running at the same time passes that, the extra ones are not doing work, they are waiting, and worse, they are making the ones that *are* working slower by fighting them for the same resources.

So "let more callers in" quietly becomes "let more callers contend." The naive shape, one database connection per concurrent caller, means the concurrency you push at MySQL is unbounded by anything except how many clients you happen to have. The database has no say in it. And the moment offered concurrency crosses the core count, you stop buying throughput and start buying latency.

## Every client gets a connection

The setup is a single MySQL 8.0.46, pinned to 4 CPUs, with a 200,000-row table: an indexed primary key `id`, a non-indexed `val`, and a `payload` column to give the rows some weight. The read is a range scan that has to do a little real work rather than a trivial point-lookup, otherwise the server never breaks a sweat and there's nothing to see:

```sql
SELECT id, val FROM reads_test
WHERE id BETWEEN ? AND ? AND val >= ?
ORDER BY val DESC LIMIT 50
```

A single one of those, run serially with nobody else around, takes about 2.6ms. That's the floor. Now the experiment: spin up `C` worker threads, give each its own dedicated MySQL connection, and have all of them run that query as fast as they can for 8 seconds. Then do it again for the next `C`. One connection per busy caller, exactly the shape you get when every app worker holds its own.

Here's throughput as the connection count climbs from 1 to 512:

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
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .qps { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
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
  <h3>Throughput vs. connection count (4 cores)</h3>
  <svg class="cb-svg" viewBox="0 0 720 300" role="img" aria-label="Queries per second against connection count from 1 to 512 on a log-2 x axis, rising to a peak of 1137 QPS at 4 connections then declining to 720 QPS at 512 connections">
    <line class="grid" x1="48" y1="254.4" x2="706" y2="254.4"/>
    <line class="grid" x1="48" y1="134.2" x2="706" y2="134.2"/>
    <line class="grid" x1="48" y1="14" x2="706" y2="14"/>
    <text x="42" y="258" text-anchor="end">0</text>
    <text x="42" y="138" text-anchor="end">600</text>
    <text x="42" y="18" text-anchor="end">1200</text>
    <text x="48" y="284" text-anchor="middle">1</text>
    <text x="194.2" y="284" text-anchor="middle">4</text>
    <text x="340.4" y="284" text-anchor="middle">16</text>
    <text x="486.7" y="284" text-anchor="middle">64</text>
    <text x="632.9" y="284" text-anchor="middle">256</text>
    <text x="706" y="284" text-anchor="end">512</text>
    <polyline class="qps" points="48.0,186.3 121.1,110.1 194.2,26.7 267.3,36.6 340.4,57.1 413.6,68.9 486.7,79.2 559.8,85.8 632.9,94.9 706.0,110.3"/>
    <circle cx="194.2" cy="26.7" r="3.5" fill="var(--cb-green)"/>
    <circle cx="706" cy="110.3" r="3.5" fill="var(--cb-orange)"/>
    <text x="202" y="24">peak 1137 QPS @ 4 conns</text>
    <text x="700" y="128" text-anchor="end">512 conns · 720 QPS</text>
  </svg>
  <figcaption>QPS climbs to 1137 at 4 connections, exactly the core count, then falls the whole rest of the way to 720 at 512. Adding connections past the cores didn't add throughput, it removed it. Measured on MySQL 8.0.46 pinned to 4 CPUs, results in benchmarks/mysql-connection-pool/results/expA_curve.csv.</figcaption>
</figure>

The peak sits at 4 connections, which is not a coincidence, it's the core count. After that every connection you add makes the number go down. It's a gentle decline, 1137 down to 720, so if you only ever look at throughput you might shrug this off as a rounding error and keep bumping `max_connections`. That would be a mistake, because throughput is not where this hurts.

## The tail is where it hurts

Throughput barely moved. The p99 moved like a rocket. Same runs, but plotting the 99th-percentile latency at each connection count:

<figure class="cache-bench">
  <h3>p99 latency vs. connection count</h3>
  <div class="cb-bar-row"><span>1 conn</span><span class="cb-track"><span class="cb-fill" style="--value:0.30%"></span></span><span class="cb-value">5.2ms</span></div>
  <div class="cb-bar-row"><span>2 conns</span><span class="cb-track"><span class="cb-fill" style="--value:0.29%"></span></span><span class="cb-value">4.9ms</span></div>
  <div class="cb-bar-row"><span>4 conns</span><span class="cb-track"><span class="cb-fill" style="--value:0.47%;--bar:var(--cb-green)"></span></span><span class="cb-value">8.1ms</span></div>
  <div class="cb-bar-row"><span>8 conns</span><span class="cb-track"><span class="cb-fill" style="--value:2.37%"></span></span><span class="cb-value">40.7ms</span></div>
  <div class="cb-bar-row"><span>16 conns</span><span class="cb-track"><span class="cb-fill" style="--value:3.40%"></span></span><span class="cb-value">58.3ms</span></div>
  <div class="cb-bar-row"><span>32 conns</span><span class="cb-track"><span class="cb-fill" style="--value:5.44%"></span></span><span class="cb-value">93.2ms</span></div>
  <div class="cb-bar-row"><span>64 conns</span><span class="cb-track"><span class="cb-fill" style="--value:10.74%"></span></span><span class="cb-value">184.1ms</span></div>
  <div class="cb-bar-row"><span>128 conns</span><span class="cb-track"><span class="cb-fill" style="--value:20.39%"></span></span><span class="cb-value">349.6ms</span></div>
  <div class="cb-bar-row"><span>256 conns</span><span class="cb-track"><span class="cb-fill" style="--value:45.03%"></span></span><span class="cb-value">772.1ms</span></div>
  <div class="cb-bar-row"><span>512 conns</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1714.6ms</span></div>
  <figcaption>The tail at 4 connections is 8.1ms. At 512 it's 1714.6ms, 211 times worse, for a workload that got 1.58x slower on throughput over the same span. The slowdown you can barely see in the average is a catastrophe at the tail. Results in benchmarks/mysql-connection-pool/results/expA_curve.csv.</figcaption>
</figure>

8 milliseconds to 1.7 seconds. That's a 211x blow-up on p99 while throughput dropped a mere 1.58x. This is the whole point and it's why the throughput graph lies to you: the database is doing very nearly the same amount of work per second at 512 connections as at 4, it's just that any given query now waits behind hundreds of others for its turn on four cores. Nobody is dropped, everybody is late. The average hides it because most of the mass is still moving, but the reads at the tail, the ones your slowest users actually feel, went from imperceptible to a page that visibly hangs.

## Why more connections is slower

Four cores can run four queries at once. That's the ceiling on real work, and I hit it at 4 connections, which is exactly where throughput peaked. Everything past that is a query that's ready to run but has nowhere to run, so it sits in a runnable queue and the OS shuffles all of them on and off the cores in turns. Each turn costs a context switch, each switch cools the caches the previous query warmed, and every one of them is still holding buffer-pool latches and contending on the same internal mutexes while it waits. You've turned four busy cores into four cores that spend a growing slice of their time managing a crowd instead of answering queries.

One honest note on the shape of this, because the harness earns its keep by being honest. My first version of the query was a pure aggregate with no sort, and on that workload throughput stayed almost dead flat as I added connections and only the tail blew up. That's a real result, but it undersells the story, because a pure CPU-bound aggregate keeps MySQL pegged near saturation no matter what and there's no headroom to lose. Adding the `ORDER BY val DESC` gives each query a real filesort to do, which is closer to what actual reads look like, and *that's* the version where you can watch throughput itself bend back down past the core count. The tell was always the tail. The throughput dip is the same disease showing up on the graph people actually watch.

## A small pool, in front

Here's the part that feels backwards. Keep all 512 callers. They still exist, they still want to read, none of them go away. But instead of 512 connections to MySQL, put a fixed pool of `P` connections in the middle and make every caller borrow one, run its query, and hand it back. The callers queue in the application, cheaply, on a semaphore, instead of queueing inside the database on latches and cores. The database only ever sees `P` queries at once, and if you set `P` near the core count, it sees exactly as much concurrency as it can actually turn into work.

Same 512 offered clients, but now behind a bounded pool:

<figure class="cache-bench">
  <h3>p99 at 512 clients: direct vs. a bounded pool</h3>
  <div class="cb-bar-row"><span>512 direct conns</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1714.6ms</span></div>
  <div class="cb-bar-row"><span>pool of 64</span><span class="cb-track"><span class="cb-fill" style="--value:12.69%"></span></span><span class="cb-value">217.7ms</span></div>
  <div class="cb-bar-row"><span>pool of 32</span><span class="cb-track"><span class="cb-fill" style="--value:6.03%"></span></span><span class="cb-value">103.4ms</span></div>
  <div class="cb-bar-row"><span>pool of 16</span><span class="cb-track"><span class="cb-fill" style="--value:3.75%"></span></span><span class="cb-value">64.3ms</span></div>
  <div class="cb-bar-row"><span>pool of 8</span><span class="cb-track"><span class="cb-fill" style="--value:2.45%;--bar:var(--cb-green)"></span></span><span class="cb-value">42.1ms</span></div>
  <figcaption>512 callers over 8 backend connections answered at 42.1ms p99 and 957 QPS, against 1714.6ms and 720 QPS for 512 direct connections. That's 41x lower tail latency and more throughput, from the same offered load. Note the pool of 64 lands near the 64-connection row of the direct curve: it's the backend connection count that decides this, not how many callers you have. Results in benchmarks/mysql-connection-pool/results/expC_pool.csv.</figcaption>
</figure>

The pool of 8 answers the same 512 callers at a 42.1ms p99 and 957 QPS. Direct, those callers got 1714.6ms and 720 QPS. Fewer connections, 41 times lower tail latency, and *more* throughput, which recovers 84% of the absolute peak the whole system ever reached. And notice the gradient: the tighter the pool, the better it does, right down to 8. The pool of 64 is barely better than going direct, because a pool of 64 lets 64 queries contend, which is the same crowd on the same four cores. The number that matters was never how many clients you have. It's how many of them you let touch the database at once.

## The takeaway

Reads don't get faster because you opened more connections. Past the core count, another connection doesn't add capacity, it just adds one more query fighting the same four cores, and that fight shows up as tail latency long before it shows up as throughput. The fix isn't a bigger `max_connections`, it's a smaller number of connections shared behind a pool, sized somewhere near the cores the database actually has, with everybody else waiting cheaply in the app instead of piling into the engine.

Two things to remember. Watch the p99, not the average, because this failure is nearly invisible in the mean and brutal at the tail. And when a read tier is falling behind, the instinct to let more callers through is usually the exact thing making it slower. Size the pool to the cores the database has, not to the number of callers waiting in front of it.

The harness, the queries, and every number here are [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/mysql-connection-pool) if you want to run it. These are laptop numbers on a database pinned to four cores, so read the ratios and the shapes, not the absolute QPS, your hardware will land somewhere else.
