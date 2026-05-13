---
layout:     post
title:      The p99.9 That Only Spiked While We Were Scaling Up
date:       2026-07-20
published:  false
description:    p50 and p99 flat and healthy, p99.9 jumping to 400ms, but only for the fifteen minutes after autoscaling added a node. The trail went fork → smaps → Transparent Huge Pages, and the textbook fix was one I couldn't use.
categories: redis latency thp linux operations
---
<!--
  DRAFT — published:false until real numbers land. Pending benchmarks/redis-thp (Gemini handoff).
  Flip published:false (remove the line) and delete this comment once every [[BENCH:*]] token is
  replaced with a measured value from benchmarks/redis-thp/results/. Token → source map lives in
  benchmarks/redis-thp/HANDOFF.md (section 3).

  Tokens in this file:
    [[BENCH:redis_version]] [[BENCH:kernel]]
    [[BENCH:dataset_keys]] [[BENCH:dataset_bytes]] [[BENCH:ops_rate]]
    [[BENCH:p50_steady]] [[BENCH:p99_steady]] [[BENCH:p999_steady]]
    [[BENCH:p50_sync]] [[BENCH:p99_sync]] [[BENCH:p999_sync_thp_on]]
    [[BENCH:fork_ms]] [[BENCH:latency_doctor]] [[BENCH:anon_hugepages]]
    [[BENCH:p999_sync_thp_off]] [[BENCH:p999_diskless]]
    [[BENCH:sync_duration_thp_on]] [[BENCH:sync_duration_thp_off]]
    [[BENCH:failed_shape_note]]
  Figure bar --value widths and the timeline polyline points are placeholders too; recompute from
  results/latency_percentiles.csv and results/latency_timeline.csv once they exist.
-->

If you've ever had a p50 and p99 that stay flat and boring while your p99.9 quietly jumps to 400ms, and it only happens for a few minutes right after autoscaling adds a node, this is for you. It's one of those failures that hides in exactly the percentile nobody's dashboard defaults to, and correlates with the one event nobody thinks of as a request.

I wanted to reproduce this locally because the first time I met it I chased the wrong thing for most of a day. A checkout service on a Redis cluster, healthy averages, and every time the fleet scaled out the 99.9th percentile spiked and then healed itself in fifteen minutes. Average latency never twitched. If you were only watching p50 you'd swear nothing happened.

[[BENCH:failed_shape_note]] The shapes that didn't reproduce the spike are in the repo too, because the interesting part of this one is the constraint at the end, and pretending it reproduced on the first try would skip the honest part.

The [harness, workload, the smaps captures, and the raw LATENCY output are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-thp). These are numbers from a Linux box I control, the mechanism transfers, the absolute milliseconds do not, and the whole point of the post is a fix that wouldn't be available to me if I didn't control that box. Redis [[BENCH:redis_version]], kernel [[BENCH:kernel]].

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
  <h3>The spike only lived in one percentile</h3>
  <!-- PLACEHOLDER GEOMETRY: recompute polyline points from results/latency_timeline.csv.
       x = time (90..600), y = latency (top=30 peak, bottom=210 zero). p50 line stays flat low;
       p999 line jumps at the scale-out marker and decays back. -->
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="thp-tl-title thp-tl-desc">
    <title id="thp-tl-title">p50 versus p99.9 latency across an autoscale event</title>
    <desc id="thp-tl-desc">p50 stays flat and low; p99.9 spikes at the scale-out marker then decays back over about fifteen minutes.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="26" y="214">0</text>
    <text x="10" y="124">200ms</text>
    <text x="10" y="34">400ms</text>
    <polyline class="p50"  points="90,196 300,196 360,194 600,196" />
    <polyline class="p999" points="90,190 300,188 315,34 380,70 470,150 600,186" />
    <text x="300" y="24">node added</text>
    <line class="grid" x1="312" y1="30" x2="312" y2="210" style="stroke-dasharray:3 3" />
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>p50</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>p99.9</span>
  </div>
  <figcaption>Same primary, same command mix. p50 never notices the scale-out. p99.9 spikes when the new replica pulls its snapshot and heals as the sync finishes. Geometry pending the benchmark run.</figcaption>
</figure>

## What the spike actually looked like

Steady state first, so the spike has something to be measured against. On the loaded primary, serving [[BENCH:ops_rate]] ops/sec against [[BENCH:dataset_keys]] keys ([[BENCH:dataset_bytes]]):

```
p50    [[BENCH:p50_steady]]
p99    [[BENCH:p99_steady]]
p99.9  [[BENCH:p999_steady]]
```

Then a node joins and pulls a full sync. The same window:

```
p50    [[BENCH:p50_sync]]
p99    [[BENCH:p99_sync]]
p99.9  [[BENCH:p999_sync_thp_on]]
```

p50 and p99 shrug. p99.9 goes to [[BENCH:p999_sync_thp_on]]. If your SLO is written against p99, this is invisible, and it's also exactly the 1-in-1000 checkout that a real customer is sitting in front of.

## The trail that pointed at fork, and then past it

Redis has good tooling for this and it took me straight to a plausible-but-wrong answer. `LATENCY HISTORY` and `LATENCY DOCTOR` on the primary both fingered fork:

```
[[BENCH:latency_doctor]]
```

Fork. Fine. A new replica syncs by asking the primary to `BGSAVE`, which forks a child to write the snapshot while the parent keeps serving. Forking a process with a large heap is the classic Redis latency source, so the story writes itself.

Except when I actually measured the fork call, it was [[BENCH:fork_ms]]. Nowhere near the spike. The fork itself was cheap. That's the moment where you either declare victory on the wrong diagnosis, or you keep pulling.

So I looked at what the process memory was actually made of, straight from `/proc/<pid>/smaps`:

```
AnonHugePages:   [[BENCH:anon_hugepages]]
```

There it is. The heap was dominated by anonymous huge pages, which means Transparent Huge Pages was on and Redis's memory was backed by 2MB pages instead of 4KB ones. And that changes everything about what happens *after* the fork.

## Why THP turns a cheap fork into a 400ms tail

The fork is cheap because of copy-on-write, the parent and child share the same physical pages until one of them writes. The cost isn't paid at fork time. It's paid later, one page fault at a time, every time the still-serving parent writes to a shared page and the kernel has to copy it so the child's snapshot stays consistent.

With normal 4KB pages, each of those copy-on-write faults copies 4KB. Annoying, cheap, over in microseconds. With Transparent Huge Pages on, the shared pages are 2MB, so every single copy-on-write fault copies 2MB instead of 4KB. That's 512 times the work per fault. A checkout write that touches one key can now stall behind a 2MB page copy, and it happens over and over for the entire duration of the snapshot the new replica is pulling.

So the spike isn't really the fork at all, it's the copy-on-write faults that come after it, each one amplified 512x because THP made the pages 512x bigger. It only shows up during a full sync because that's the one time the primary keeps a forked child alive long enough to eat a storm of those faults while it's still serving traffic, and it heals in fifteen minutes because that's how long the sync takes, after which there's no child left to copy pages for.

<figure class="cache-bench">
  <h3>Same sync, three memory configurations</h3>
  <div class="cb-bar-row"><span>THP on (disk sync)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">[[BENCH:p999_sync_thp_on]]</span></div>
  <div class="cb-bar-row"><span>THP never (disk sync)</span><span class="cb-track"><span class="cb-fill" style="--value:22%;--bar:var(--cb-green)"></span></span><span class="cb-value">[[BENCH:p999_sync_thp_off]]</span></div>
  <div class="cb-bar-row"><span>THP on + diskless</span><span class="cb-track"><span class="cb-fill" style="--value:30%;--bar:var(--cb-blue)"></span></span><span class="cb-value">[[BENCH:p999_diskless]]</span></div>
  <figcaption>p99.9 on the primary during a replica full-sync. Turning THP off fixes it directly. Diskless replication softens it without host access by streaming the snapshot instead of forking to disk. Bar widths are placeholders until results/latency_percentiles.csv is filled.</figcaption>
</figure>

## The textbook fix, and why I couldn't use it

Every article about this ends the same way, and it's correct:

```bash
echo never > /sys/kernel/mm/transparent_hugepage/enabled
```

Turn THP off, the page faults go back to 4KB, the tail flattens. When I ran that on the box I control, the same sync's p99.9 dropped to [[BENCH:p999_sync_thp_off]]. Done.

Here's the catch that no bare-metal post has to deal with: on a managed Redis cluster you don't own the host. There's no shell on the node, no `/sys/kernel/mm/` to write to, and the provider isn't going to flip THP for you. So the standard fix, the one every article ends on, is just off the table for a lot of people, and you're left needing an answer that lives inside Redis config and your own scheduling instead.

## What you do when you can't touch the host

- **`repl-diskless-sync yes`.** This is the big one. Instead of the child forking and writing the RDB to disk while the parent keeps dirtying shared pages for the whole write, the primary streams the snapshot straight to the replica socket. The fork is shorter-lived and the window where COW faults pile up shrinks with it. In the harness, flipping this on with THP still on brought the same sync's p99.9 to [[BENCH:p999_diskless]], and the sync finished in [[BENCH:sync_duration_thp_on]] rather than dragging. It doesn't remove the 512x amplification, it shrinks the window you pay it in.
- **Schedule scale-out off-peak, and size so it's rare.** If a full sync is going to cost a tail spike you can't fully kill, the move is to not take one during peak checkout. Pre-scale ahead of known load, set autoscaling thresholds with hysteresis so the fleet isn't adding and removing nodes near the edge, and run enough headroom that full syncs are an occasional event rather than a traffic-shaped drumbeat.
- **Alert on p99.9, correlated with scale events.** The reason this took me a day the first time is that nobody was looking at the percentile it lived in. Put p99.9 on the board next to a scale-out annotation, and the spike lining up with the node joining is most of the way to the answer.

## How to prove it's THP and not fork

This is the habit that pulled me off the wrong answer, so it's the one I'd actually keep:

- Measure the fork call itself. Redis exposes `latest_fork_usec` in `INFO stats`. If that number is small and your spike is large, the fork is not your problem, something after the fork is.
- Read `/proc/<pid>/smaps` (or `smaps_rollup`) and look at `AnonHugePages`. If it's a large share of RSS, THP is backing your heap and every COW fault is a 2MB copy.
- Confirm the host setting: `cat /sys/kernel/mm/transparent_hugepage/enabled`. `[always]` is the smoking gun. On a managed service you may not even be able to read it, which is itself the tell that the textbook fix is off the table.

The fork ends up being a red herring, and what you can actually do about the real cause comes down to whether you own the kernel. If you do, you turn THP off and move on. If you're on a managed cluster you can't, so you shrink the window instead with diskless replication and you stop taking full syncs in the middle of peak checkout. It's the same root cause both times, but the box you're running on decides which half of the fix is even available to you.
