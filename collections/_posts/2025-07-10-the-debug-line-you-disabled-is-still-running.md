---
layout:     post
title:      The Debug Line You Disabled Is Still Running
date:       2025-07-10
description:    A DEBUG line that never prints still cost me 5,260 ns a call, because the logger checks the level after you've already built the message. That, sync writes on the request thread, and logging every event instead of sampling: three ways logging quietly eats a hot path.
categories: logging performance python observability operations
---

If you've ever turned a service's log level up to INFO in production, told yourself the debug lines are free now, and moved on, this one is for you. I did exactly that once. A request handler with a couple of chatty `logger.debug(...)` lines in the hot loop, flipped to INFO before the deploy, and I filed it under "handled." The lines weren't printing anymore. I assumed that meant they weren't running.

The CPU profile disagreed. The debug lines were near the top, for a log level that emitted nothing.

## The problem

A disabled log line is not free. Python's `logging` does check the level and drop the line, but it checks *inside* `logger.debug(...)`, and by the time you call `logger.debug(...)` you've already built the argument. If that argument is an f-string that concatenates a few fields and calls `json.dumps` on a payload, all of that work happens first, every call, and then the logger looks at the level and throws the finished string away. You paid to produce a message nobody will ever read.

I wanted to see the number, so I put a `DEBUG` line behind a logger set to `INFO` and ran it a million times three different ways: an eager f-string, a lazy `%`-style call, and one wrapped in an `isEnabledFor` guard.

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
<h3>What a disabled DEBUG line costs, per call</h3>
<div class="cb-bar-row"><span>eager f-string</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">5,260 ns</span></div>
<div class="cb-bar-row"><span>lazy %-format</span><span class="cb-track"><span class="cb-fill" style="--value: 26.99%; --bar: var(--cb-blue);"></span></span><span class="cb-value">1,420 ns</span></div>
<div class="cb-bar-row"><span>isEnabledFor guard</span><span class="cb-track"><span class="cb-fill" style="--value: 1.11%; --bar: var(--cb-green);"></span></span><span class="cb-value">59 ns</span></div>
<figcaption>The same discarded line, three ways to write it. The eager f-string builds the whole message, a <code>json.dumps</code> of the order payload, on every call, and only then does <code>logging</code> look at the level and drop it: 89.8x the cost of the guard, 3.7x the cost of deferred %-formatting, for zero output. Measured on Python 3.12.13, 1,000,000 iterations, median of 5. Results in benchmarks/logging-hot-path/results/exp1_disabled_debug.csv.</figcaption>
</figure>

The eager version cost 5,260 ns a call. The `isEnabledFor` guard cost 59. Nothing was logged in any of the three, so that whole gap is pure waste, work done to produce a string that gets dropped one stack frame later.

Here is the shape of it:

```python
# eager: builds the message every time, then logging drops it
logger.debug(f"processed order {order_id}: {json.dumps(payload)}")

# lazy: %-formatting is deferred, only runs if the record is emitted
logger.debug("processed order %s: %s", order_id, payload)

# guarded: skips even the call overhead when the level is off
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("processed order %s: %s", order_id, payload)
```

The lazy form is the one most people already know they should use, and it does help: 1,420 ns instead of 5,260, because the `%s` interpolation is deferred until `logging` decides to emit, which it never does here. But `json.dumps(payload)` in the eager form runs no matter what, because *you* called it, not the logger. The guard is the only version that skips the argument-building entirely, and on a genuinely hot path that 59-vs-5,260 difference is the one worth caring about.

None of this matters on a line that runs a thousand times a day. It matters a lot on a line that runs inside the loop that runs on every request.

## When the line does print, where does it print

Turning debug off gets you out of the first hole. The second one shows up on the lines you actually do want, the INFO lines, and it's about *where the write happens*, not whether it happens.

A plain `FileHandler` does its work on the thread that called `logger.info(...)`. The formatting, the write, and if you care about durability the `flush` and `fsync`, all of it runs inline, so your request latency now includes a disk write. Python's `logging.handlers` ships a fix for this: a `QueueHandler` on the caller that just drops the record onto a queue, and a `QueueListener` on a background thread that drains the queue and does the actual I/O. The caller pays for an enqueue. The disk pays on someone else's thread.

I timed each `logger.info(...)` call on the calling thread, 50,000 of them, writing to a durable sink both ways.

<figure class="cache-bench">
<h3>Per-call latency on the calling thread, sync vs async handler</h3>
<div class="cb-panels">
<div>
<p class="cb-panel-title">p99 (µs)</p>
<div class="cb-bar-row"><span>sync</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">645.1</span></div>
<div class="cb-bar-row"><span>async</span><span class="cb-track"><span class="cb-fill" style="--value: 1.61%; --bar: var(--cb-green);"></span></span><span class="cb-value">10.4</span></div>
</div>
<div>
<p class="cb-panel-title">p99.9 (µs)</p>
<div class="cb-bar-row"><span>sync</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">5,752</span></div>
<div class="cb-bar-row"><span>async</span><span class="cb-track"><span class="cb-fill" style="--value: 0.43%; --bar: var(--cb-green);"></span></span><span class="cb-value">24.6</span></div>
</div>
</div>
<figcaption>Sync writes on the caller: p50 276 µs, p99 645 µs, p99.9 5,752 µs, and a worst case of 31 ms when an fsync stalled. Async: p50 4.2 µs, p99 10.4 µs, p99.9 24.6 µs. The caller only enqueues; the fsync moves to the background thread. Wall time for the whole run was 14.9 s sync vs 0.23 s on the calling loop async. Measured on Python 3.12.13, 50,000 calls per mode. Results in benchmarks/logging-hot-path/results/exp2_sync_vs_async.csv.</figcaption>
</figure>

The tail is where it hurts. At p99.9 the synchronous handler was 5,752 µs, the async one was 24.6, and the sync worst case was a 31 ms fsync stall sitting right in the middle of a request. Move the I/O to a background thread and the caller stops caring about disk weather.

One honest caveat, because it changes the picture. My first version of this experiment used a plain buffered `FileHandler` with no fsync, and async *lost*: the OS page cache swallowed the writes so cheaply that the queue-and-thread machinery just added overhead, and async tail latency came out worse than sync. That run is in the repo under `results/attempts/` with a note. The chart above uses a durable sink that flushes and fsyncs every record, which is what forces the same real I/O cost to be paid, and the only variable left is which thread pays it. If your logs go to a buffer the kernel flushes lazily, async buys you much less. If they go somewhere that actually blocks, a socket, a slow disk, a full pipe, it buys you the whole tail.

## The cheapest fix is to not log the line at all

The last one isn't about cost per line, it's about how many lines. At Twitter-or-any-large-service scale the expensive thing about an INFO line that fires on every event is that it fires on every event. Most of those lines are identical in shape and nobody reads 999 out of 1,000 of them. So don't write them: sample. Log one in a hundred, keep a counter, move on.

<figure class="cache-bench">
<h3>Logging every event vs sampling one in a hundred</h3>
<div class="cb-panels">
<div>
<p class="cb-panel-title">Log bytes written, 1M events</p>
<div class="cb-bar-row"><span>every event</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">75.7 MB</span></div>
<div class="cb-bar-row"><span>1% sample</span><span class="cb-track"><span class="cb-fill" style="--value: 1.0%; --bar: var(--cb-green);"></span></span><span class="cb-value">757 KB</span></div>
</div>
<div>
<p class="cb-panel-title">Workload throughput (ops/sec)</p>
<div class="cb-bar-row"><span>1% sample</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-green);"></span></span><span class="cb-value">10.6M</span></div>
<div class="cb-bar-row"><span>every event</span><span class="cb-track"><span class="cb-fill" style="--value: 2.06%; --bar: var(--cb-orange);"></span></span><span class="cb-value">217K</span></div>
</div>
</div>
<figcaption>A million events. Logging all of them wrote 1,000,000 lines and 75,667,678 bytes at 217,325 ops/sec. Sampling one in a hundred wrote 10,000 lines and 756,666 bytes and ran the workload at 10,561,711 ops/sec: 100x fewer lines, 100x fewer bytes, 48.6x the throughput because most iterations never touch the logger at all. Measured on Python 3.12.13, 1,000,000 events per mode. Results in benchmarks/logging-hot-path/results/exp3_sampling.csv.</figcaption>
</figure>

100x fewer lines, 100x fewer bytes, and the workload ran 48.6x faster, because 99 iterations out of 100 skipped the whole logging call. The throughput jump is really just the first two experiments cashing out at volume: every line you don't log is an argument you don't build and a write you don't do.

The catch is obvious and worth saying out loud. Sampling throws away individual events, so it's great for "how often does this happen" and useless for "show me the exact request that failed." Sample the chatty success path, keep every error, and know which of your log lines are which.

## The takeaway

Three separate holes, three separate fixes, and the good news is they're all cheap:

- **A disabled log line still runs your argument code.** Guard hot-path debug lines with `isEnabledFor`, or at least pass args lazily with `%s` instead of building an f-string. The line that prints nothing was still costing me 5,260 ns.
- **A synchronous handler puts disk latency on your request thread.** A `QueueHandler` plus a background `QueueListener` moves the I/O off the caller and flattens the tail, from a 5,752 µs p99.9 down to 24.6. It trades a little durability for it: records sitting in the queue are lost if the process dies, so don't put your audit log behind it.
- **The cheapest line is the one you never write.** Sample high-volume INFO down to 1% and you get 100x less data and most of the CPU back, as long as you keep every error and know you've given up per-event detail.

The one thing to remember: logging is code that runs on your hot path, and it charges you even when it produces nothing. Treat it like any other thing in that loop and measure it.

The [harness, the three experiments, and the raw CSVs are on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/logging-hot-path), including the buffered-sink attempt where async lost. These are numbers from a laptop and a Docker VM, not a capacity plan: the ratios travel, the absolute microseconds depend on how your disk and your fsync behave, and Experiment 2 in particular is a shape, async an order of magnitude under sync, not a fixed figure. Python 3.12.13.
