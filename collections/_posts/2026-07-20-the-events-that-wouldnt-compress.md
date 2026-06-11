---
layout: post
title: The Events That Wouldn't Compress
date: 2026-07-20
description: I batched analytics events and watched zstd hit a wall at 4.7x. The floor turned out to be the events themselves — the identifiers that make each one unique are also the bytes that won't compress.
categories: [systems, performance]
---

The first analytics pipeline I helped run sent one event per request. A button click, a page view, an export started — each one its own little JSON body, its own TLS handshake amortized across nothing, its own line in the bill. It worked. It also cost more than the feature it was measuring, and when someone asked why the ingestion bill kept climbing faster than traffic, the honest answer was that we were paying to ship the same field names a few billion times a day.

So we did the obvious thing. We batched. Group a few hundred events, compress the batch, send one request. And the first time I measured it I expected the compression ratio to just keep climbing as the batches got bigger — more data, more redundancy, more for the compressor to chew on. It climbed for a while. Then it stopped at 4.7x and would not move, no matter how big the batch got.

That plateau is the whole post. It turns out the thing that makes an analytics event *useful* is the same thing that makes it *incompressible*, and once you see why, you stop trying to beat it and start designing around it.

## The problem

An analytics event is mostly boilerplate. Here's a compact one, the kind a click generates:

```json
{"event":"button_clicked","user_id":"u_9f3ac21b7e004d18","session_id":"s_4a1c9e2f7b03","ts":1721030400123,"props":{"page":"/design/edit","referrer":"/home","device":"desktop","country":"US","ab_variant":"B","duration_ms":842,"position":3}}
```

Compact-encoded, mine averaged **292.7 bytes**. Look at what's actually in there. The field names — `event`, `user_id`, `session_id`, `props`, `page`, `device` — repeat on *every single event*. The enum values repeat too: there are maybe a dozen event names, three device types, eight country codes. If you're sending these one at a time, you are re-transmitting that dictionary billions of times, and a compressor that only ever sees one event has nothing to compare it against.

That's the naive cost. And you can watch compression do almost nothing about it when the batch size is one:

<figure class="cache-bench">
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
<h3>Compressed bytes per event, by batch size (zstd level 3)</h3>
<div class="cb-bar-row"><span>raw, uncompressed</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-muted);"></span></span><span class="cb-value">292.6 B</span></div>
<div class="cb-bar-row"><span>batch = 1</span><span class="cb-track"><span class="cb-fill" style="--value: 77.65%; --bar: var(--cb-orange);"></span></span><span class="cb-value">227.2 B</span></div>
<div class="cb-bar-row"><span>batch = 10</span><span class="cb-track"><span class="cb-fill" style="--value: 31.85%; --bar: var(--cb-blue);"></span></span><span class="cb-value">93.2 B</span></div>
<div class="cb-bar-row"><span>batch = 100</span><span class="cb-track"><span class="cb-fill" style="--value: 21.73%; --bar: var(--cb-green);"></span></span><span class="cb-value">63.6 B</span></div>
<div class="cb-bar-row"><span>batch = 1000</span><span class="cb-track"><span class="cb-fill" style="--value: 21.27%; --bar: var(--cb-green);"></span></span><span class="cb-value">62.2 B</span></div>
<figcaption>A single event compresses to 227 B — barely better than raw, because there is nothing to compare it against. Batch a thousand and each event costs 62 B. Measured on Python 3.9.6 / zstandard 0.23.0, results in benchmarks/analytics-event-batching/results/a_amortization.csv.</figcaption>
</figure>

At batch size one, zstd got the event from 292.6 down to 227.2 bytes. That's a ratio of 1.29x, and it's basically the compressor shrugging — a little intra-event redundancy, nothing structural. Batch a thousand of them together and the *same events* cost 62.2 bytes each. Nothing about the events changed. The only thing that changed is that the compressor could finally see that `"session_id":"s_"` shows up on all of them.

## Why batching is really a compression trick

I used to think of batching as an amortization thing — fewer requests, fewer handshakes, less per-message framing overhead. That's real, but it's the small win. The big win is that batching is what *feeds* the compressor. A dictionary compressor like zstd works by finding a byte sequence it has seen before and replacing the repeat with a short back-reference. One event gives it no history. A batch gives it a few hundred near-identical rows, and every field name after the first is a back-reference.

So the ratio climbs with batch size, steeply at first:

<figure class="cache-bench">
<h3>Compression ratio vs batch size (zstd level 3)</h3>
<svg class="cb-svg" viewBox="0 0 560 240" role="img" aria-label="Line chart of compression ratio rising with batch size then plateauing near 4.7x">
  <line class="grid" x1="46" y1="200" x2="540" y2="200"></line>
  <line class="grid" x1="46" y1="150" x2="540" y2="150"></line>
  <line class="grid" x1="46" y1="100" x2="540" y2="100"></line>
  <line class="grid" x1="46" y1="50" x2="540" y2="50"></line>
  <text x="40" y="204" text-anchor="end">1x</text>
  <text x="40" y="154" text-anchor="end">2x</text>
  <text x="40" y="104" text-anchor="end">3x</text>
  <text x="40" y="54" text-anchor="end">4x</text>
  <text x="40" y="18" text-anchor="end">4.7x</text>
  <line class="grid" x1="46" y1="15" x2="540" y2="15" style="stroke-dasharray: 4 4;"></line>
  <polyline class="p50" points="46,185.5 107,121.5 168,92.5 229,55.2 290,35.6 351,19.1 412,12.65 473,28.45 534,14.05"></polyline>
  <text x="46" y="222" text-anchor="middle">1</text>
  <text x="168" y="222" text-anchor="middle">10</text>
  <text x="351" y="222" text-anchor="middle">100</text>
  <text x="534" y="222" text-anchor="middle">1000</text>
  <text x="290" y="238" text-anchor="middle">batch size (log scale of tested points)</text>
</svg>
<figcaption>1.29x at batch 1 → 3.15x at 10 → 4.62x at 100 → 4.72x at 1000. The dashed line is the ceiling it never clears. Points are evenly spaced by tested index, not linearly by batch size. Measured on Python 3.9.6 / zstandard 0.23.0, results in benchmarks/analytics-event-batching/results/b_ratio_vs_batchsize.csv.</figcaption>
</figure>

From batch 1 to batch 10 the ratio more than doubles, 1.29x to 3.15x. From 10 to 100 it climbs again to 4.62x. And then it just… stops. Batch 100 is 4.62x, batch 250 is 4.75x, batch 1000 is 4.72x. Ten times the batch buys you two percent more ratio. The standard deviation at batch 1000 was ±0.009, so that flatness is real, not noise — I ran fifty batches of a thousand and they all landed on 4.7x.

## The floor is the identifiers

Here's the part that took me a minute. Why 4.7x and not 10x, not 20x? The boilerplate is *extremely* repetitive — you'd think it would crush.

Answer: because the compressible part isn't the whole event. Every event carries three fields that are, by design, high-entropy — a `user_id`, a `session_id`, and (in the real pipeline) a per-event `event_id` for deduplication. Those are random. That's the point of them; an identifier that compressed well would be an identifier that collided. zstd can strip the repeated *schema* down to almost nothing, but it cannot do a thing about sixteen bytes of random hex on every row. So the batch converges to a floor: roughly the size of the incompressible identity payload, plus a rounding error of structure.

That 62 bytes per event at batch 1000 is basically the identifiers, encoded. The schema — everything that made the raw event 292 bytes — has been compressed into the noise. You're paying to ship the thing that makes each event unique, and nothing more. Which is the correct amount to be paying. The lesson I took: **when your compression ratio plateaus, you're not looking at a compressor limit, you're looking at your data's entropy floor.** Stop tuning the compressor. Go look at what in the payload is actually random, and ask whether it needs to be that big.

## So which compressor, and at what level

Once you know the ratio is capped by entropy, the level knob gets a lot less exciting. I put five options through the same batch of 500 events — no compression, gzip at its default 6, and zstd at 3, 9, and 19:

<figure class="cache-bench">
<h3>Codec shootout at batch size 500</h3>
<div class="cb-panels">
<div>
<p class="cb-panel-title">Compression ratio</p>
<div class="cb-bar-row"><span>gzip-6</span><span class="cb-track"><span class="cb-fill" style="--value: 81.09%; --bar: var(--cb-purple);"></span></span><span class="cb-value">4.71x</span></div>
<div class="cb-bar-row"><span>zstd-3</span><span class="cb-track"><span class="cb-fill" style="--value: 76.30%; --bar: var(--cb-blue);"></span></span><span class="cb-value">4.43x</span></div>
<div class="cb-bar-row"><span>zstd-9</span><span class="cb-track"><span class="cb-fill" style="--value: 86.89%; --bar: var(--cb-blue);"></span></span><span class="cb-value">5.05x</span></div>
<div class="cb-bar-row"><span>zstd-19</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-green);"></span></span><span class="cb-value">5.81x</span></div>
</div>
<div>
<p class="cb-panel-title">Compress time per batch (ms)</p>
<div class="cb-bar-row"><span>gzip-6</span><span class="cb-track"><span class="cb-fill" style="--value: 4.42%; --bar: var(--cb-purple);"></span></span><span class="cb-value">1.37 ms</span></div>
<div class="cb-bar-row"><span>zstd-3</span><span class="cb-track"><span class="cb-fill" style="--value: 0.93%; --bar: var(--cb-blue);"></span></span><span class="cb-value">0.29 ms</span></div>
<div class="cb-bar-row"><span>zstd-9</span><span class="cb-track"><span class="cb-fill" style="--value: 3.87%; --bar: var(--cb-blue);"></span></span><span class="cb-value">1.20 ms</span></div>
<div class="cb-bar-row"><span>zstd-19</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">31.05 ms</span></div>
</div>
</div>
<figcaption>zstd-3 gives up ~14% of the ratio of zstd-19 and runs it in ~1% of the time. zstd-19 buys 1.31x more ratio for ~100x the compress cost. Measured on Python 3.9.6 / zstandard 0.23.0, batch 500, n=240 batches, results in benchmarks/analytics-event-batching/results/c_codec_shootout.csv.</figcaption>
</figure>

zstd-3 came out at 4.43x in 0.29 ms per batch — 509 MB/s of input. gzip-6 got a slightly better 4.71x but took 1.37 ms, about a fifth the throughput. And zstd-19, the one you reach for when you think ratio is everything, got 5.81x — the best on the board — for 31.05 ms per batch. That's a hundred times the compress cost of zstd-3 to move the ratio from 4.4 to 5.8. On a pipeline doing billions of events, thirty-one milliseconds of CPU per batch of five hundred is not a compression setting, it's a capacity problem.

The decompress side barely moved across all of them, 0.06–0.18 ms, which matters because your consumers decompress far more often than your producers compress. zstd-3 is the boring correct answer here: most of the ratio, a fraction of the cost, and it decompresses fast. (Ignore the `none` row's throughput number in the raw CSV — it's dividing by a near-zero time and reads as two million MB/s, which just means "instant".)

## The bill you didn't expect: duplicates

There's a second half to batching that nobody warns you about, and it's not a compression problem at all. Once you batch and compress and ship over a network, you have to decide what happens when a send is *ambiguous* — the server committed the batch but the 200 OK got lost on the way back. The producer doesn't know it succeeded, so it retries. And now that batch is in the pipeline twice.

The honest guarantee a system like this can offer is **at-least-once**: if you got a 200, every event is delivered to every consumer at least once. Not exactly once. At least once. Which means duplicates are not a bug, they're a documented output, and the duplicate rate just tracks how often sends go ambiguous:

<figure class="cache-bench">
<h3>Duplicate rate at the consumer vs producer retry probability</h3>
<div class="cb-bar-row"><span>retry p = 0.5%</span><span class="cb-track"><span class="cb-fill" style="--value: 9.05%; --bar: var(--cb-blue);"></span></span><span class="cb-value">0.46%</span></div>
<div class="cb-bar-row"><span>retry p = 1%</span><span class="cb-track"><span class="cb-fill" style="--value: 20.57%; --bar: var(--cb-blue);"></span></span><span class="cb-value">1.05%</span></div>
<div class="cb-bar-row"><span>retry p = 2%</span><span class="cb-track"><span class="cb-fill" style="--value: 39.17%; --bar: var(--cb-orange);"></span></span><span class="cb-value">1.99%</span></div>
<div class="cb-bar-row"><span>retry p = 5%</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">5.08%</span></div>
<figcaption>40,000 events per run. Duplicate rate tracks retry probability almost exactly, because each ambiguous send re-delivers its events. Dedup by event_id recovered exactly 40,000 unique events every time. Measured on Python 3.9.6, results in benchmarks/analytics-event-batching/results/d_atleastonce_dedup.csv.</figcaption>
</figure>

At a 1% retry rate I got a 1.05% duplicate rate — 423 extra events on 40,000. At 5%, it was 5.08%. It tracks the retry rate because that's all it is: the events that rode a retried batch, delivered twice. And this is exactly why every event needs that random `event_id` — the same field that put a floor under our compression ratio is the field that makes dedup possible. I dedup'd each run by `event_id` and got back exactly 40,000 unique events, every time, at every retry rate.

So the identifiers cut both ways. They're the bytes that won't compress, and they're the bytes a consumer uses to throw away the duplicate copy of a click. Same field, both jobs.

## The takeaway

Batching analytics events is a compression play before it's a throughput play — a single event barely compresses because the compressor has no history, and the ratio only shows up once you give it a few hundred near-identical rows to back-reference against. But it converges to a floor, and the floor is your data's entropy: the random per-event identifiers that don't compress no matter how big the batch gets. For my events that floor was 4.7x and about 62 bytes per event, and once you're there, chasing a higher zstd level costs you 100x the CPU for single-digit percent gains. Use zstd-3, size your batches into the hundreds, and stop.

And remember what those incompressible bytes are for. At-least-once delivery means retries, retries mean duplicates, and the duplicate rate just tracks how flaky your network is — about 1% duplicates at a 1% retry rate. The only reason a consumer can clean that up is the per-event id, which is also the exact thing capping your compression ratio. You're paying in bytes for the identifier, and getting dedup in return. Budget for both.

The harness is at [github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/analytics-event-batching](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/analytics-event-batching) — event generator, the four experiments, and the CSVs. These are laptop numbers meant to show the shape of the thing, not capacity planning for your pipeline; run it against your own event schema, because your entropy floor is set by your identifiers, not mine.
