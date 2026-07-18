---
layout:     post
title:      Catching the Bots Without Remembering the Clicks
date:       2026-07-18
description:    A click firehose has bots hammering it from a handful of sources, and the obvious way to flag them is a counter keyed by source. That counter grows forever. Count-Min Sketch, Top-K and a Bloom filter do the same job in a fixed few megabytes. Measured on Redis 7.4.7.
categories: redis probabilistic-data-structures streaming fraud
---

If you've ever had to flag the bots in an ad-click stream, you know the first thing you reach for is a counter keyed by source. A dictionary, source to count, bump it on every click, and anything clicking a thousand times a minute lights up. It works beautifully on a sample. Then you point it at the real firehose, a million distinct sources an hour, and the counter is still holding every one of those million keys long after the humans behind them clicked once and left, and your process is sitting on a few hundred megabytes to remember a fact you'll never use again. The bots are a handful of sources. You're paying to remember everybody else.

## The problem

To catch a high-frequency source you have to count per source, and to dedup replayed clicks you have to remember every click id you've seen, and both of those sets grow with your traffic, not with the fraud. The bots are twenty sources hammering the stream; the memory goes to the million ordinary sources you keep around just to confirm they're ordinary. Exact counting answers the question, but the structure that answers it never stops growing, and on a real firehose "never stops growing" is the whole problem. What you actually want is a structure whose size you pick up front and never pay again, that's still accurate about the thing you care about, the heavy hitters, and is allowed to be sloppy about everything else.

I built a stream to see how bad the exact version gets and how little the sloppy version costs. A million human sources each clicking a handful of times, twenty planted bots each clicking 50,000 times, twelve percent of the clicks being exact replays of an id already seen. That's 2,430,865 clicks, 2,138,760 unique ids. I ran the same stream through the exact structures and through three probabilistic ones from Redis, Count-Min Sketch, Top-K, and a Bloom filter, and measured the memory each one actually used.

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
.cb-bar-row { display: grid; grid-template-columns: minmax(8rem, 1.3fr) minmax(6rem, 4fr) minmax(4.6rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.4rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .curve { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }
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

## The counter that never stops growing

<figure class="cache-bench">
  <h3>Memory as unique sources grow: exact vs probabilistic</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="gr-title gr-desc">
    <title id="gr-title">Exact memory climbs linearly with unique sources while the probabilistic structures stay flat</title>
    <desc id="gr-desc">The exact source counter plus id-set climbs from 34MB at 100k sources to 317MB at 1M sources; the combined probabilistic structures sit flat near 6MB.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="20" y="214">0</text>
    <text x="8" y="124">160MB</text>
    <text x="8" y="34">320MB</text>
    <polyline class="curve" style="stroke:var(--cb-orange)" points="90,210 141,191 217,166 345,121 600,32" />
    <polyline class="curve" style="stroke:var(--cb-green)" points="90,207 600,207" />
    <circle cx="600" cy="32" r="5" style="fill:var(--cb-orange)" />
    <text x="500" y="26">exact, 317MB</text>
    <text x="360" y="200">probabilistic, 5.8MB</text>
    <text x="126" y="230">100k</text>
    <text x="330" y="230">500k</text>
    <text x="588" y="230">1M</text>
  </svg>
  <figcaption>Exact = Python dict (source→count) + set of seen click-ids. At 100k sources it's 34.1MB (13.6 + 20.5), by 1M sources it's 316.9MB (126.8 + 190.0) and still climbing on a straight line. The three probabilistic structures together (Count-Min Sketch 0.80MB + Top-K 0.067MB + Bloom 4.94MB) are 5.8MB, fixed the moment you allocate them. Measured on Redis 7.4.7, results in benchmarks/redis-heavy-hitters/results/.</figcaption>
</figure>

That orange line is the whole reason to bother. The exact source counter alone was 126.8MB at a million sources, the exact id-set another 190.0MB, and neither has any reason to stop, every new source is a new key, every new click id is a new member. A Redis HASH instead of a Python dict trims the counter to 80.4MB, still linear, still climbing. The green line is what you get when you decide up front how much memory you're willing to spend and refuse to spend a byte more. The interesting part is that the flat line still answers every question the climbing line did.

## Counting without remembering who

A Count-Min Sketch is a small grid of counters, and every source hashes to one cell in each row. You bump those cells on every click and read a source's count back as the smallest of its cells. Collisions make it lie, but only ever upward, and only ever by other sources landing in the same cells, so the count you read is the true count plus some collision noise. The trick is that a heavy hitter's real count is so much bigger than the noise that you can still pick it out.

<figure class="cache-bench">
  <h3>Source counting: exact dict vs Count-Min Sketch</h3>
  <div class="cb-bar-row"><span>exact dict</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">126.8 MB</span></div>
  <div class="cb-bar-row"><span>Count-Min Sketch</span><span class="cb-track"><span class="cb-fill" style="--value:0.63%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.80 MB</span></div>
  <figcaption>Both counted the same 2.43M-click stream. The sketch (width 20000, depth 5) is 0.80MB no matter how many sources it sees. On the 20 planted bots, true count 50,000, the sketch read a mean of 50,057 — overestimating by 57, about 0.11%. Measured on Redis 7.4.7, results in benchmarks/redis-heavy-hitters/results/.</figcaption>
</figure>

The bots came out at 50,000 true and roughly 50,057 estimated, an overshoot of 57 on fifty thousand, noise you'd never notice. Here's the honest part, though, and it's not a bug. That same collision floor added a mean of 58 to the humans too, and a human's real count was 1 to 4, so the sketch reported ordinary sources as having clicked 58, 70, 80 times. If you'd tried to use the sketch to tell two humans apart it would be useless, the floor is bigger than their entire signal. But you don't care about two humans, you care about the twenty sources sitting five hundred times above the floor, and against 50,000 a floor of 58 vanishes. So the sketch can't separate one quiet source from another, the floor drowns them, but the twenty hitters sit so far above it that the noise doesn't reach them, and those twenty are the only ones you were counting for.

## The list of who's hammering you

Count-Min gives you a count if you name a source, but it won't hand you the list of who the heavy hitters are, you'd still have to ask it about every source you ever saw, which is the memory you were trying not to keep. Top-K is the structure that keeps the leaderboard itself. It holds a small number of slots, k of them, and a decaying min-heap, and it fights to keep the busiest sources in those slots and lets the quiet ones fall out. You never enumerate anybody, you just read the board.

<figure class="cache-bench">
  <h3>Top-K leaderboard: planted bot vs the busiest human that made the list</h3>
  <div class="cb-bar-row"><span>planted bot (×20)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">50,000</span></div>
  <div class="cb-bar-row"><span>top human in list</span><span class="cb-track"><span class="cb-fill" style="--value:0.008%;--bar:var(--cb-muted)"></span></span><span class="cb-value">4</span></div>
  <figcaption>Top-K (k=50, width 1000, depth 8) is 0.067MB fixed. All 20 planted bots landed in ranks 1 through 20 at count 50,000 — recall 20/20. The remaining 30 slots are ordinary humans at count 4, and not one human ranked above any bot. Measured on Redis 7.4.7, results in benchmarks/redis-heavy-hitters/results/.</figcaption>
</figure>

Every one of the twenty bots was in the top twenty, at their true 50,000, and the rest of the fifty-slot board was filled with humans stuck at 4, which is the whole point, the gap between a bot and the busiest non-bot is 50,000 to 4 and nothing crosses it. Sixty-seven kilobytes to carry the answer you were about to spend 126 megabytes computing. You still want the sketch alongside it, Top-K tells you who the hitters are and roughly how hard, the sketch lets you interrogate a specific source on demand, but neither of them is holding a key per source.

## Catching the replays

The other half of the job is the replayed clicks, the same click id showing up again, and the exact tool for that is a set of every id you've seen, which was the 190MB half of that first orange line. A Bloom filter does membership without storing the members. It's a bit array and a few hashes; adding an id flips some bits, and to test one you check whether all its bits are already set. If any bit is clear the id is definitely new; if they're all set it's probably a repeat, probably because some other ids might have flipped those same bits between them.

<figure class="cache-bench">
  <h3>Deduping click-ids: exact set vs Bloom filter</h3>
  <div class="cb-bar-row"><span>exact set</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">190.0 MB</span></div>
  <div class="cb-bar-row"><span>Bloom filter</span><span class="cb-track"><span class="cb-fill" style="--value:2.6%;--bar:var(--cb-green)"></span></span><span class="cb-value">4.94 MB</span></div>
  <figcaption>Both deduped 2,138,760 unique ids out of 2.43M clicks. The Bloom filter (capacity 2.5M, target error 0.1%) caught 100% of the 292,105 replays and wrongly flagged 0.011% of genuinely-new ids as seen — 22 out of 200,000. Measured on Redis 7.4.7, results in benchmarks/redis-heavy-hitters/results/.</figcaption>
</figure>

The Bloom filter never missed a real replay, it can't, if you've seen an id its bits are set, so recall was a flat 100%. What it costs you is the other direction, 0.011% of genuinely-new clicks came back marked as already-seen, twenty-two of two hundred thousand, clicks you'd wrongly drop as duplicates. That measured rate came in under the 0.1% I'd sized it for, and that's not luck either, a Bloom filter runs below its nominal error until it fills up, and 2.14 million ids in a filter built for 2.5 million never quite got there. Size it too tight and that false-positive rate climbs; the number you pick is the number of real clicks you're willing to throw away.

## The takeaway

Exact counting answers the question and then punishes you for asking, 317 megabytes at a million sources and a straight line pointing up. The probabilistic three, Count-Min Sketch for on-demand counts, Top-K for the leaderboard, a Bloom filter for dedup, did the same job in 5.8 megabytes you allocate once and never grow, flagged all twenty bots, and caught every replay at eleven false positives in a hundred thousand. The trade you're making is exactness for a fixed error, and you have to know where it lands, the sketch can't tell two quiet sources apart because its collision floor is bigger than their signal, the Bloom filter will drop a tiny fraction of real clicks as fake, and both of them you have to size before you know your traffic, guess too small and the errors grow. But for catching the heavy hitters, the twenty sources that stick out five hundred times above everyone else, the sloppiness is invisible and the memory is flat, and flat is the only thing that survives a real firehose. [The stream, the harness, and every structure's numbers are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-heavy-hitters). Laptop numbers on Redis 7.4.7, not a capacity statement, but the shape holds wherever you run it.
