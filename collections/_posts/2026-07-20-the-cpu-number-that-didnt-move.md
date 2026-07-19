---
layout:     post
title:      The CPU Number That Didn't Move
date:       2026-07-20
description:    I built the classic unanchored-regex CPU bug on purpose and profiled it with async-profiler. Process CPU load barely moved, 9.9% to 10.05%, while throughput dropped 8x, 1.86M lines/sec to 14.79M. top would have told me nothing was wrong.
categories: java performance cpu-profiling flame-graphs
---

I'd read the same story a handful of times on different blogs: someone's log-scanning code is quietly eating a core, they pull a flame graph, and the whole thing is `Pattern.match` wearing a costume. I always nodded along and never once opened a profiler myself to watch it happen. So this week I built the bug on purpose, in a small Java program with nothing else running on it, and pointed async-profiler at it until I had my own numbers instead of someone else's screenshot.

## The problem

Somewhere in a log-scanning hot path, someone wants to know if a line contains the word "ERROR". The obvious-looking way to write that in Java is `line.matches(".*ERROR.*")`. It reads fine, it's correct, and it's slower than it has any reason to be, because `String.matches()` already requires the entire input to match the pattern; the `.*` on either side of `ERROR` is dead weight. Java's regex engine is a backtracking NFA, not a DFA, so for every line that does *not* contain "ERROR" (the common case in a healthy service log) it still has to try anchoring the literal at every offset before it can give up. That's the bug people mean when they say "unanchored regex," and it's the one from the classic async-profiler war stories.

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

Two variants of the same loop, matching against the same pool of 2,000 synthetic log lines (about 5% real `ERROR` lines, the rest ordinary `INFO`/`DEBUG` noise), each run for a fixed 35 seconds:

```java
boolean isMatch = fixed
        ? line.contains("ERROR")                 // O(n) single pass
        : line.matches(".*ERROR.*");              // unanchored, backtracking-heavy full match
```

Sampling `getProcessCpuLoad()` once a second and counting lines processed was the whole measurement. Then I ran each variant under `asprof -e cpu` for a real flame graph instead of guessing at what the engine was doing internally.

## The number that didn't move

Here's the part that would have fooled me if I'd only had `top` open. Process CPU load, averaged over the run:

<figure class="cache-bench">
  <h3>CPU load, bad vs fixed (process CPU, normalized to 1 core of 10)</h3>
  <div class="cb-bar-row"><span>bad</span><span class="cb-track"><span class="cb-fill" style="--value:9.9%;--bar:var(--cb-orange)"></span></span><span class="cb-value">9.9%</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:10.05%;--bar:var(--cb-green)"></span></span><span class="cb-value">10.05%</span></div>
  <figcaption>9.9% avg (max 10.84%) vs 10.05% avg (max 11.31%). Basically the same number. Measured on OpenJDK 25.0.1, results in benchmarks/java-high-cpu-debugging/results/.</figcaption>
</figure>

Both variants are single-threaded and both keep one core continuously busy the whole run, so on a 10-core host that's ~10% of total capacity either way, whether that core is doing useful work or not. If a teammate had paged me with "CPU looks fine, must be something else," I'd have believed them. CPU% measures how busy the machine is, not what it's busy doing, and that distinction is the entire reason this bug is worth writing about.

Throughput is where the bug actually shows up:

<figure class="cache-bench">
  <h3>Lines matched per second, bad vs fixed</h3>
  <div class="cb-bar-row"><span>bad</span><span class="cb-track"><span class="cb-fill" style="--value:12.56%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1.86M</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">14.79M</span></div>
  <figcaption>1,857,476 lines/sec vs 14,790,202 lines/sec, for the identical CPU budget. Fixed does 8.0x the work. Measured on OpenJDK 25.0.1, results in benchmarks/java-high-cpu-debugging/results/.</figcaption>
</figure>

Same core, same 35 seconds, 8x fewer lines checked. That's the cost of the backtracking, and it's invisible to anything that only watches CPU%.

## What the flame graph actually shows

The bad flame graph isn't subtle once you open it. Two frames account for about 88% of all samples:

<figure class="cache-bench">
  <h3>Where the "bad" run's CPU samples land</h3>
  <div class="cb-bar-row"><span>CharPropertyGreedy.match</span><span class="cb-track"><span class="cb-fill" style="--value:46%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~46%</span></div>
  <div class="cb-bar-row"><span>Pattern$Slice.match</span><span class="cb-track"><span class="cb-fill" style="--value:42%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~42%</span></div>
  <div class="cb-bar-row"><span>everything else</span><span class="cb-track"><span class="cb-fill" style="--value:12%;--bar:var(--cb-grid)"></span></span><span class="cb-value">~12%</span></div>
  <figcaption>CharPropertyGreedy.match is the greedy ".*" retrying its match at each offset. Pattern$Slice.match is the literal "ERROR" check it keeps retrying against. Together, almost nine out of ten samples. The fixed run doesn't touch the regex engine at all.</figcaption>
</figure>

`CharPropertyGreedy.match` is the greedy quantifier doing its backtracking, `Pattern$Slice.match` is the literal-string matcher it keeps re-invoking against the shifted offset. Nothing else in the call stack gets a look-in. `regex-fixed`'s flame graph, by contrast, barely has a call stack worth mentioning; `String.contains()` is a single pass and doesn't leave much of a shadow.

## Stuff worth remembering

- CPU% alone can't tell you a single-threaded hot loop is doing 8x less work than it should. Throughput or wall-clock-per-unit-of-work is the number that actually moves.
- `String.matches()` already anchors the whole string. Wrapping the pattern in `.*` on both sides changes nothing about correctness and costs you the backtracking engine's worst case.
- A flame graph turns "the CPU is busy" into "the CPU is busy doing this specific, nameable thing," which is the whole reason to reach for one before reaching for more hardware.
- These are laptop numbers demonstrating the mechanism, [the lab and its flame graphs are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-high-cpu-debugging). It's a small switchable Maven project with two more bugs in it, a spinning thread that isn't actually idle and a Hibernate session that pays for changes it never made, both worth their own look.

## The takeaway

If you're debugging a CPU problem and `top -H` says the number is fine, that only tells you nobody's core is pegged past what you'd expect, not that the work happening on that core is worth doing. The regex bug here didn't move CPU% at all, bad and fixed sat within a rounding error of each other, and the only way to see that eight out of every nine lines of throughput had gone missing was to look at what the CPU was actually spending its cycles on. Anchor your patterns, or better, skip the regex engine entirely when a plain substring check does the same job, and don't trust a flat CPU graph to mean nothing's wrong.
