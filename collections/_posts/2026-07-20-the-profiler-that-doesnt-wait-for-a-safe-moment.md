---
layout:     post
title:      The Profiler That Doesn't Wait for a Safe Moment
date:       2026-07-20
description:    Why async-profiler's samples land where your code actually runs instead of where the JVM parks it. Safepoint bias, AsyncGetCallTrace, the four clocks it can sample on, and why you'd want it running in production all the time.
categories: java performance cpu-profiling flame-graphs profiling
---

In three earlier posts I pointed async-profiler at bugs I'd built on purpose, a [regex quietly eating a core](/blog/the-cpu-number-that-didnt-move/), a [thread that was never actually idle](/blog/the-thread-that-was-never-actually-idle/), and a Hibernate session paying for changes it never made. Every time, I put up the flame graph and said "that's the actual profiler output, not a mockup," and moved on. What I never explained is why I trust that output at all, or why I reach for async-profiler instead of the sampler built into every JDK. So this one is the companion piece: not a bug, just how the tool works and why its picture of your program is the honest one.

*Everything here I pulled from three write-ups I keep going back to: the [async-profiler manual by Krzysztof Słusarski](https://krzysztofslusarski.github.io/2022/12/12/async-manual.html), [Atlassian's post on continuous JVM profiling](https://www.atlassian.com/blog/atlassian-engineering/continuous-profiling-of-jvm), and a [hands-on async-profiler walkthrough](https://handsonculture.blog/posts/async-profiler/). Credit up front so nobody has to guess where the ideas came from.*

## The problem

You want to know where a Java program spends its time, so you attach a sampling profiler. Most of the classic ones, the sampler in VisualVM, anything built on JVMTI's `GetStackTrace`, a shell loop around `jstack`, work the same way underneath: to read a thread's stack safely, they wait until that thread is parked at a **safepoint**. A safepoint is a spot where the JVM knows the thread's state is consistent enough to inspect, and the JIT only plants safepoint polls at specific places, mostly method returns and loop back-edges. Tight, hot, inlined code can run for a long time without passing one.

So here's the trap. A thread is burning cycles deep inside some hot method that has no safepoint poll in it. The profiler wants a sample. It can't read the stack where the thread actually is, so it waits, and the sample gets recorded only once the thread finally reaches the next safepoint, which might be a different method entirely. Every sample drifts toward wherever the safepoints happen to be. Your hottest code, the stuff with no safepoints because the JIT optimized it hard, is exactly the code that gets under-counted. This is safepoint bias, and it means the profile can point you at the wrong method with total confidence. You go optimize a getter while the real fire burns two frames down.

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
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-text); font: 12px system-ui, sans-serif; }
.cb-svg text.muted { fill: var(--cb-muted); }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .box { fill: none; stroke: var(--cb-grid); stroke-width: 1.5; }
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

## What async-profiler does instead

async-profiler doesn't ask the thread to stop somewhere convenient. It gets the kernel to interrupt the thread wherever it is, with a signal, `SIGPROF` on a wall or CPU timer, or a `perf_events` overflow when you're counting hardware cycles. Inside that signal handler it calls `AsyncGetCallTrace`, an undocumented-but-stable HotSpot entry point that walks the Java stack from an arbitrary point of execution, no safepoint required. The sample gets attributed to the frame the thread was really in when the timer fired, not the next safepoint it would have limped to.

<figure class="cache-bench">
  <h3>Same instant, two profilers, two answers</h3>
  <svg class="cb-svg" viewBox="0 0 720 250" role="img" aria-labelledby="sp-title sp-desc">
    <title id="sp-title">Where a sample gets attributed under safepoint bias versus async-profiler</title>
    <desc id="sp-desc">A thread runs a hot method with no safepoint poll inside. A timer fires mid-method. async-profiler attributes the sample to the hot method; a safepoint-only profiler can only record it once the thread reaches the next safepoint, attributing it to the wrong frame.</desc>

    <text x="20" y="30">Thread timeline (time runs left to right)</text>

    <!-- execution track -->
    <rect x="20" y="70" width="470" height="34" rx="4" fill="var(--cb-orange)" fill-opacity="0.18" stroke="var(--cb-orange)" stroke-width="1.5"></rect>
    <text x="255" y="91" text-anchor="middle">hotMethod(), no safepoint poll inside</text>

    <rect x="490" y="70" width="210" height="34" rx="4" class="box"></rect>
    <text x="595" y="91" text-anchor="middle" class="muted">return / loop back-edge</text>

    <!-- safepoint marker -->
    <line class="grid" x1="490" y1="58" x2="490" y2="116"></line>
    <text x="490" y="52" text-anchor="middle" class="muted">next safepoint</text>

    <!-- sample fires -->
    <line x1="230" y1="40" x2="230" y2="68" stroke="var(--cb-blue)" stroke-width="2"></line>
    <path d="M230,70 l-5,-9 l10,0 z" fill="var(--cb-blue)"></path>
    <text x="230" y="34" text-anchor="middle" fill="var(--cb-blue)">timer fires here</text>

    <!-- async-profiler attribution -->
    <line x1="230" y1="104" x2="230" y2="150" stroke="var(--cb-green)" stroke-width="2"></line>
    <path d="M230,150 l-5,-9 l10,0 z" fill="var(--cb-green)"></path>
    <text x="245" y="170" fill="var(--cb-green)">async-profiler &#8594; hotMethod()  &#10003; where it actually was</text>

    <!-- safepoint-only attribution -->
    <path d="M230,104 C230,128 490,128 490,150" fill="none" stroke="var(--cb-orange)" stroke-width="2" stroke-dasharray="5 4"></path>
    <path d="M490,150 l-5,-9 l10,0 z" fill="var(--cb-orange)"></path>
    <text x="505" y="205" text-anchor="end" fill="var(--cb-orange)">safepoint-only &#8594; whatever runs at the next</text>
    <text x="505" y="221" text-anchor="end" fill="var(--cb-orange)">safepoint  &#10007; wrong frame</text>
  </svg>
  <figcaption>The timer fires while the thread is deep in a method with no safepoint. async-profiler records the frame it interrupted; a <code>GetStackTrace</code>-based profiler has to wait for the next safepoint and books the sample there. The hotter and better-optimized the code, the fewer safepoints it has, and the more the biased profile under-counts it.</figcaption>
</figure>

One flag matters here. Those non-safepoint program counters need debug info to resolve back to line numbers and inlined frames, and the JIT only keeps that around if you ask. Run with `-XX:+UnlockDiagnosticVMOptions -XX:+DebugNonSafepoints` and the flame graph resolves cleanly; skip it and the hot JIT-compiled frames come back vague. It's cheap, turn it on.

## The four clocks

Once you can sample a stack from anywhere, "profiling" stops being one thing. A flame graph is just a pile of stacks counted up, and async-profiler will build that pile on top of whichever clock you hand it. The event you pick is the whole question you're asking:

- `-e cpu` counts CPU cycles via `perf_events`. This is "what is the machine burning its cores on." It only sees threads that are actually running.
- `-e wall` samples every thread on a wall-clock timer whether it's running or parked. This is "where does the elapsed time go," including time spent blocked, sleeping, or waiting on something else.
- `-e alloc` samples allocations. New TLABs show up as one kind of frame, slow-path allocations that spilled straight into Eden as another. This is "what's feeding the garbage collector."
- `-e lock` samples threads waiting on monitors. This is "who's stuck behind a lock, and which one."

The CPU-versus-wall distinction is the one that has burned me in spirit even when the tool didn't. The manual has a clean example: a request handler where CPU profiling makes `mathConsumer()` look like nearly the whole cost, because on the CPU clock it is, it's the only part actually spinning a core. Switch to wall-clock and the same method is around 4% of the request, because the request spends the rest of its life waiting on an external system that never shows up on a CPU profile at all.

<figure class="cache-bench">
  <h3>Same 100 requests, profiled on two different clocks</h3>
  <p class="cb-panel-title">CPU clock: what's burning a core</p>
  <div class="cb-bar-row"><span>mathConsumer()</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~100%</span></div>
  <p class="cb-panel-title" style="margin-top:0.9rem">Wall clock: where the time goes</p>
  <div class="cb-bar-row"><span>mathConsumer()</span><span class="cb-track"><span class="cb-fill" style="--value:4%;--bar:var(--cb-green)"></span></span><span class="cb-value">~4%</span></div>
  <div class="cb-bar-row"><span>waiting on external system</span><span class="cb-track"><span class="cb-fill" style="--value:96%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~96%</span></div>
  <figcaption>A worked example from the async-profiler manual, not my own measurement. If you're chasing latency and you profile on the CPU clock, you'll optimize the one method that isn't the problem. Pick the clock that matches the question: <code>cpu</code> for a hot loop, <code>wall</code> for a slow request.</figcaption>
</figure>

The rule of thumb I use: reach for `-e cpu` when a core is pegged, and `-e wall` when a request is slow but nothing looks busy. They answer different questions and they will disagree, and the disagreement is usually the interesting part.

## Reading the flame graph

The picture that comes out is the same shape regardless of clock. Every sample is a stack, drawn bottom-up with the entry point at the base and the leaf at the top. Identical frames sitting next to each other get merged into one wide bar, so **width is share of the samples**, which on the CPU clock means share of the time, on the alloc clock share of the bytes, on the lock clock share of the wait. The top edge is where the resource is actually being spent; everything below it is just the call chain that got there. You read it by scanning the top for the widest plateau and following it straight down to see who called it.

Here's the CPU flame graph from the regex bug in the first post, the real one:

![Flame graph of the regex-bad run, showing java/util/regex/Pattern$CharPropertyGreedy.match and Pattern$Slice.match dominating the stack above String.matches](/images/posts/java-high-cpu-debugging/flame-regex-bad.jpg)

Two frames, `Pattern$CharPropertyGreedy.match` and `Pattern$Slice.match`, take up almost the entire width, stacked right above `String.matches`. That wide plateau at the top is the answer: the backtracking regex engine is where every cycle goes. There's nothing to interpret, no counter to squint at, the shape just tells you. And because those samples came in through `AsyncGetCallTrace` and not a safepoint, the width is honest, the engine frames aren't drifting somewhere else on the graph.

## Why you'd leave it running

The last piece is the one I underrated for a long time. A profiler you attach during an incident only ever shows you incidents, and only the incidents slow enough that a human was around to attach it. Atlassian's argument for continuous profiling is exactly that gap: someone or something has to be there to capture a profile at the right moment, so transient problems get missed entirely, and even when you do catch one you've got nothing to compare it against because you never profiled the healthy state.

Always-on flips it. You schedule async-profiler to dump stacks on a regular cadence, ship those samples somewhere columnar and cheap, Atlassian went with ORC files on S3 queried through Athena, and then you can slice any time window after the fact and diff it against a normal baseline. Overhead is the tax you're paying for that, and it stays low enough to run in production because sampling a stack every few milliseconds is cheap next to the work the program is already doing.

<figure class="cache-bench">
  <h3>Continuous profiling, end to end</h3>
  <svg class="cb-svg" viewBox="0 0 720 150" role="img" aria-labelledby="cp-title cp-desc">
    <title id="cp-title">Continuous profiling pipeline</title>
    <desc id="cp-desc">The JVM with an async-profiler agent produces scheduled stack dumps, which land in a columnar store on object storage, which you query and aggregate over any time window and diff against a baseline.</desc>
    <rect x="10" y="40" width="150" height="60" rx="6" class="box"></rect>
    <text x="85" y="66" text-anchor="middle">JVM +</text>
    <text x="85" y="84" text-anchor="middle">async-profiler agent</text>

    <rect x="196" y="40" width="150" height="60" rx="6" class="box"></rect>
    <text x="271" y="66" text-anchor="middle">scheduled</text>
    <text x="271" y="84" text-anchor="middle">stack dumps</text>

    <rect x="382" y="40" width="150" height="60" rx="6" class="box"></rect>
    <text x="457" y="66" text-anchor="middle">columnar store</text>
    <text x="457" y="84" text-anchor="middle" class="muted">ORC on S3</text>

    <rect x="568" y="40" width="150" height="60" rx="6" class="box"></rect>
    <text x="643" y="62" text-anchor="middle">query any window,</text>
    <text x="643" y="80" text-anchor="middle">diff vs baseline</text>

    <path d="M160,70 l32,0" stroke="var(--cb-muted)" stroke-width="1.5" marker-end="url(#cp-arrow)"></path>
    <path d="M346,70 l32,0" stroke="var(--cb-muted)" stroke-width="1.5" marker-end="url(#cp-arrow)"></path>
    <path d="M532,70 l32,0" stroke="var(--cb-muted)" stroke-width="1.5" marker-end="url(#cp-arrow)"></path>
    <defs>
      <marker id="cp-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 z" fill="var(--cb-muted)"></path>
      </marker>
    </defs>
  </svg>
  <figcaption>Schematic of Atlassian's setup. The value isn't any single flame graph, it's having every window on file so you can look at the one you didn't know you needed until later.</figcaption>
</figure>

The example that sold me: a code path that was under 0.5% of total runtime, invisible to any "show me the hot methods" view, quietly causing a latency regression because it spent twice as long `BLOCKED` as `RUNNABLE`. On a CPU profile it's a rounding error. On aggregated wall-clock samples over time, with a baseline to compare against, it stands out. That's a bug you cannot catch by attaching a profiler after someone complains, because by then you've lost the window and you never had the healthy one to diff against.

## The takeaway

async-profiler earns its place by refusing to wait for a convenient moment. It interrupts the thread with a signal wherever it is and reads the stack through `AsyncGetCallTrace`, so the samples land on the code that's actually running instead of drifting to the nearest safepoint the way `jstack`-style samplers do. Turn on `-XX:+DebugNonSafepoints` so those samples resolve to real frames. Then pick your clock deliberately: `cpu` when a core is pegged, `wall` when a request is slow, `alloc` when the GC is busy, `lock` when threads are stuck, they're four different questions and the tool answers whichever one you asked. And if you only ever run it during an incident, you'll only ever see incidents, and never the baseline that makes the small, slow, transient bugs visible. The flame graphs in my earlier posts were the honest picture for exactly these reasons, I just hadn't said so.
