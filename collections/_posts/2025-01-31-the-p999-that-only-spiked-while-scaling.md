---
layout:     post
title:      The p99.9 That Only Spiked While We Were Scaling Up
date:       2026-07-20
description:    p50 and p99 stay flat while p99.9 rises during a replica's full sync. I built the harness to measure it for real, went looking for the classic Transparent Huge Pages explanation, and it genuinely wasn't there.
categories: redis latency thp linux operations
---

If you've ever had a p50 and p99 that stay flat and boring while your p99.9 quietly jumps, and it only happens for a few minutes right after autoscaling adds a node, this is for you. It's one of those failures that hides in exactly the percentile nobody's dashboard defaults to, and correlates with the one event nobody thinks of as a request.

I wanted to reproduce this locally because the first time I met it I chased the wrong thing for most of a day. A checkout service on a Redis cluster, healthy averages, and every time the fleet scaled out the 99.9th percentile spiked and healed itself a few minutes later. Average latency never twitched. If you were only watching p50 you'd swear nothing happened.

The shape that didn't reproduce is the useful negative here: a read-only workload against the same trigger, same fresh-replica full sync, same everything except the primary never writes. p99.9 barely moved, 9.894ms steady against 10.494ms during the sync, because copy-on-write only costs you anything when the *parent* writes to a page the child still holds a reference to. Read-only traffic never dirties a page, so there's nothing to copy. That shape is in the repo under `attempts/read-only-control/` instead of deleted, because a version of this post where every workload spiked would be a lie by omission.

The full harness, the deterministic workload, and every raw capture are [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-thp). I ran this on my Mac, Docker Desktop, not a Linux box I actually control, and that turned out to be exactly the constraint the second half of this post is about: the standard fix for the dramatic version of this bug needs root on the host kernel, and Docker Desktop won't give you that, so I could only measure the part of the mechanism that doesn't need it. Redis 7.4.0, jemalloc 5.3.0.

## The problem

A p50 and p99 that never move while p99.9 rises during a replica's full sync, invisible to any SLO written against the percentiles everyone actually watches, and easy to misdiagnose as the fork itself when the fork call is cheap. The textbook explanation is Transparent Huge Pages turning ordinary copy-on-write faults into ones 512 times larger, and the textbook fix needs host-kernel access most people running on a managed box, or a Mac, don't have. Below is what's actually measurable without that access, and what happened when I went looking for the huge-page story and it wasn't there.

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
  <h3>The full sync moves one percentile, not both</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">p50 (ms)</p>
      <div class="cb-bar-row"><span>steady</span><span class="cb-track"><span class="cb-fill" style="--value:98%;--bar:var(--cb-blue)"></span></span><span class="cb-value">11.22</span></div>
      <div class="cb-bar-row"><span>disk sync</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">11.50</span></div>
      <div class="cb-bar-row"><span>diskless sync</span><span class="cb-track"><span class="cb-fill" style="--value:99%;--bar:var(--cb-blue)"></span></span><span class="cb-value">11.35</span></div>
    </div>
    <div>
      <p class="cb-panel-title">p99.9 (ms)</p>
      <div class="cb-bar-row"><span>steady</span><span class="cb-track"><span class="cb-fill" style="--value:85%;--bar:var(--cb-orange)"></span></span><span class="cb-value">46.30</span></div>
      <div class="cb-bar-row"><span>disk sync</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">54.48</span></div>
      <div class="cb-bar-row"><span>diskless sync</span><span class="cb-track"><span class="cb-fill" style="--value:81%;--bar:var(--cb-orange)"></span></span><span class="cb-value">44.20</span></div>
    </div>
  </div>
  <figcaption>Same primary, same write load, three scenarios. p50 doesn't move by anything worth mentioning. p99.9 rises about 8ms during a disk-based full sync and sits back at baseline, even slightly under it, once the sync is diskless. Measured on Redis 7.4.0, n=705-1708 samples per scenario, results in benchmarks/redis-thp/results/.</figcaption>
</figure>

## What the numbers actually were

800,000 keys, 400 bytes each, seeded so the run is reproducible (`used_memory_human` after load: 394.72M). Write load stayed on throughout: one probe connection issuing sequential `SET`s and timing true round-trip latency (about 68 ops/sec on that connection alone), plus six bulk-writer connections pipelining batches of 80 to keep real additional write pressure on the primary, independent of the probe's own pacing.

Steady state, no replica attached, n=1708 samples:

```
p50    11.220ms
p99    36.187ms
p99.9  46.300ms
```

Then a fresh replica attaches and pulls a full sync over disk (`repl-diskless-sync no`), same write load running the whole time, n=721:

```
p50    11.502ms
p99    43.091ms
p99.9  54.479ms
```

p50 moved 0.28ms. p99 moved about 7ms. p99.9 moved about 8.2ms, a 1.18x lift over steady. That's real and it's reproducible, and I'm not going to dress it up as more than it is: it's nowhere near the "quietly jumps to 400ms" shape the production incident that sent me down this hole actually had.

## The trail that pointed at fork, and Redis's own tooling shrugged

The classic story blames `fork()`. A new replica syncs by asking the primary to `BGSAVE`, which forks a child to write the snapshot while the parent keeps serving, and forking a process with a large heap is a well-known Redis latency source. So I measured it, `INFO stats`, `latest_fork_usec`:

```
disk_sync      1707 usec  (1.707ms)
diskless_sync  2030 usec  (2.030ms)
```

Cheap, both arms. Whatever's costing the 8ms isn't the fork call itself, that's over in about two milliseconds regardless of which sync mode you use.

I also asked Redis to tell on itself:

```
=== LATENCY DOCTOR ===
Dave, no latency spike was observed during the lifetime of this Redis instance, not in
the slightest bit. I honestly think you ought to sit down calmly, take a stress pill,
and think things over.

=== LATENCY HISTORY fork ===
[]
```

That's the actual output, unedited, from every arm, including the one with the measured 8ms lift. `LATENCY DOCTOR`'s default threshold is tuned for the dramatic version of this bug, hundreds of milliseconds, not single digits. If you're relying on Redis's own latency monitor to flag this for you at this scale, it won't, and you have to go look at your own percentiles directly to see it at all.

## The huge-page story, and the honest finding that it wasn't the story here

The textbook mechanism for why this gets dramatic is Transparent Huge Pages. Copy-on-write is what makes the fork cheap in the first place, the parent and child share physical pages until one of them writes, and the cost is deferred to the first write after the fork, one page fault at a time. With normal 4KB pages that fault copies 4KB. With THP backing the heap, the shared unit is a 2MB page, so the same single write can trigger copying 2MB instead of 4, roughly 512 times the work, and that's the mechanism behind a fork that measures in milliseconds turning into a tail that measures in hundreds of them.

I went looking for that in this harness, and it genuinely wasn't there. `/proc/1/smaps_rollup` at the peak of every scenario, disk_sync, diskless_sync, and the read-only control alike, all reported the same line:

```
AnonHugePages:         0 kB
```

Zero, every time, after loading roughly 400MB and idling well past `khugepaged`'s scan interval. The container's own view of `/sys/kernel/mm/transparent_hugepage/enabled` reports `[always] madvise never`, which looks like THP is on, but whatever's promoting pages elsewhere in Docker Desktop's LinuxKit VM (its own `/proc/meminfo` showed other processes accumulating huge pages over the same window) never reached this jemalloc-backed heap. THP was not backing Redis's memory in this environment, at all, for the entire run.

That's plausibly why the 8ms is 8ms and not 400ms: what's left once you take huge-page amplification out of the picture is plain 4KB-page copy-on-write under write pressure, plus whatever the `disk_sync` child pays for actually writing the RDB file to disk before the transfer starts. Both are real costs, neither is multiplied by 512. This harness can't cleanly separate those two from each other, disk I/O and ordinary copy-on-write both disappear together the moment you switch to `diskless_sync`, but it can tell you which lever to pull regardless of which one it is.

## What you do when you can't touch the host

- **`repl-diskless-sync yes`.** This is the actionable one, and it doesn't care whether THP is in play. Instead of forking, writing the RDB to disk, and then transferring the file, the primary streams the snapshot straight to the replica's socket. In this harness that meant a shorter-lived fork (1.088s vs 1.129s), a faster sync (1.519s vs 1.950s attach-to-link-up), and a p99.9 that landed statistically flat against the steady baseline instead of 8ms above it. You don't need root to set this. It's a Redis config value.
- **Don't trust `LATENCY DOCTOR` to catch this for you.** It's tuned for the dramatic version. Put p99.9 on a dashboard next to your scale-out events and look for the correlation yourself, because the built-in tooling will tell you, cheerfully, that nothing happened.
- **If you can check `AnonHugePages`, check it before you blame THP.** It's one read of `/proc/<pid>/smaps_rollup`. If it comes back zero, you're chasing the wrong 512x, and the honest next step is plain fork-and-disk-I/O cost, not a kernel setting.

## The takeaway

I went in expecting to measure the classic story and instead measured the boring one. Forking is cheap everywhere. The tail lift is real, but modest, because nothing here was amplifying it. And Redis never once flagged it on its own, I had to go pull the percentiles myself to see it at all. If you're on bare metal or a Linux box you actually own, go check `AnonHugePages` yourself, you might be looking at the 512x version I couldn't reproduce here. If you're not, `repl-diskless-sync yes` doesn't ask what your kernel is doing before it helps.
