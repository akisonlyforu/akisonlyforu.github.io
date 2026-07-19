---
layout:     post
title:      The Flame Graph That Found 25 Years a Day
date:       2026-07-20
description:    A plain walkthrough of what a flame graph actually shows, why Java was invisible to Linux perf for years, and how Netflix read one graph well enough to hand back 13 million minutes of CPU time a day.
categories: java performance cpu-profiling flame-graphs
---

*This one is theory, not my own benchmark. It's my reading of two Netflix posts that taught me how to look at a flame graph: [Java in Flames](https://netflixtechblog.com/java-in-flames-e763b3d32166) and [Saving 13 Million Computational Minutes per Day](https://netflixtechblog.com/saving-13-million-computational-minutes-per-day-with-flame-graphs-d95633b6d01f). The numbers and the story are theirs. I'm crediting them up front and using my own drawings so I'm not lifting anyone's images. If you want measured flame graphs I captured myself, [that lab is elsewhere on this site](/blog/the-cpu-number-that-didnt-move/).*

If you've ever stared at a service pinning a core, watched `top` tell you it's "busy," and had no idea what it was busy *doing*, this is the picture that fixes that. CPU% tells you the machine is busy. It says nothing about what your code is busy doing, and once you've read a good flame graph you stop guessing at the difference.

## The problem

A profiler samples the stack a few hundred or a few thousand times a second. Each sample is a full call stack, top to bottom, a frozen answer to "what was this thread doing right now." Do that for thirty seconds and you have tens of thousands of stacks. The problem was never collecting them. The problem is that tens of thousands of stack traces is an unreadable wall of text, and the thing eating your CPU is hiding inside it as a frame that shows up in, say, 40% of the samples but is buried under a hundred different call paths that lead to it.

You want the answer to one question: which function was on CPU the most, and how did the code get there. A flat list of hot functions can't tell you the second half, it loses the call context. A flame graph keeps both.

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

## What the picture actually is

Take every stack, stand each one up as a tower of boxes with the entry point on the bottom and the on-CPU function on top, then merge the identical towers together. Boxes that share the same call path fuse into one wider box. That's the whole trick. Netflix put it in one line: "The y axis is stack depth, and the x axis spans the sample population."

Two things fall out of that, and they're the only two you really need to hold onto:

- **Width is the answer.** The x-axis is not time. It's how many samples that frame appeared in, so a box that's half the width of the graph was on CPU, directly or through something it called, in half your samples. Wide means expensive. Netflix's advice is exactly this blunt: "Focus on the widest functions, which were present in the profile the most."
- **Height is just how you got there.** Stacking upward is parent calling child. A tall tower isn't slow, it's deep. Only the *top edge* of the graph is a function actually running on CPU. Everything under it is on the stack, waiting for its child to return.

<figure class="cache-bench">
  <h3>How to read one: width is CPU, height is call depth</h3>
  <svg viewBox="0 0 720 230" width="100%" role="img" aria-label="An illustrative flame graph. A wide base frame 'main' spans the full width. Above it, 'handleRequest' takes two thirds and 'gcWorker' the rest. Above handleRequest, 'parseJson' and 'render' split the width, and at the top a wide orange 'Pattern.match' box sits above parseJson as the widest on-CPU leaf.">
    <!-- level 0 -->
    <rect x="2" y="186" width="716" height="28" rx="2" fill="#23856d"></rect>
    <text x="360" y="204" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">main</text>
    <!-- level 1 -->
    <rect x="2" y="154" width="470" height="28" rx="2" fill="#2f9d82"></rect>
    <text x="237" y="172" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">handleRequest</text>
    <rect x="476" y="154" width="242" height="28" rx="2" fill="#7b5bb5"></rect>
    <text x="597" y="172" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">gcWorker</text>
    <!-- level 2 -->
    <rect x="2" y="122" width="300" height="28" rx="2" fill="#3bb597"></rect>
    <text x="152" y="140" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">parseJson</text>
    <rect x="306" y="122" width="166" height="28" rx="2" fill="#3bb597"></rect>
    <text x="389" y="140" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">render</text>
    <!-- level 3 -->
    <rect x="2" y="90" width="300" height="28" rx="2" fill="#d65f3c"></rect>
    <text x="152" y="108" text-anchor="middle" font-size="12" fill="#ffffff" font-family="monospace">Pattern.match</text>
    <rect x="306" y="90" width="120" height="28" rx="2" fill="#4c8fbf"></rect>
    <text x="366" y="108" text-anchor="middle" font-size="11" fill="#ffffff" font-family="monospace">encode</text>
    <!-- axis annotations -->
    <text x="6" y="34" font-size="12" fill="#666666">↑ y axis = stack depth (parent below, child above)</text>
    <text x="6" y="54" font-size="12" fill="#666666">→ x axis = share of samples, not time. Wider = more CPU.</text>
    <line x1="4" y1="66" x2="716" y2="66" stroke="#999999" stroke-dasharray="3 3"></line>
    <text x="360" y="228" text-anchor="middle" font-size="11" fill="#666666">the whole width = 100% of samples</text>
  </svg>
  <figcaption>My own drawing, not a captured profile. The orange <code>Pattern.match</code> is the widest box on the top edge, so it's where the CPU actually went, and you can read straight down the tower to see it was reached through <code>parseJson → handleRequest → main</code>. Netflix's colors carry the same idea: green for Java, yellow for C++, red for system code, intensity randomized just to tell neighbours apart.</figcaption>
</figure>

You can walk it bottom-up, following code from parent to child the way it ran, or top-down, reading the top edge as the list of things burning CPU right now. Same graph, two directions. Netflix uses both, and a third one I'll get to.

## Why Java couldn't draw one for years

Here's the part that surprised me. Linux has had a solid sampling profiler for a long time, `perf`, and for a C or C++ service you point it at a running process and it just works. Java was invisible to it, and for two separate reasons that both had to be fixed before any of the above was possible.

The first is names. The JVM compiles methods on the fly, "just-in-time," so the machine code for `handleRequest` didn't exist when the process started and isn't in any symbol table `perf` knows how to read. The profiler sees an address, not a method. The fix is a small JVMTI agent, perf-map-agent, that gets the JVM to dump a `/tmp/perf-PID.map` file mapping those JIT'd addresses back to method names, sizes, and hex offsets. Now the addresses have names.

The second is worse, and it's the one I find genuinely interesting. To walk a stack cheaply, the profiler follows a chain of frame pointers, one register (RBP on x86-64) that each function is supposed to leave pointing at the previous frame. The JVM, chasing speed, reclaimed that register and used it as a general-purpose one. Faster code, but the stack-walk chain is broken, so `perf` would grab the top function and then fall off a cliff. No tower. Just a floor.

The fix was a JVM flag, `-XX:+PreserveFramePointer`, that keeps RBP doing its actual job so the chain stays intact. It isn't free, Netflix measured it at "between 0 and 3% extra CPU, depending on the workload," which is a real cost you pay to be able to see anything at all. Even then some frames vanish into inlining, the graph "looks perhaps one third as deep" as a full `jstack`, but as they put it, "enough remain that we can figure out what's going on." A few percent of CPU to be able to profile the other ninety-seven is a trade I'd take every time.

## The read that found 25 years a day

The payoff post is the one about a CPU-bound microservice, and what I like about it is that the obvious readings *didn't* work. Bottom-up, the lower frames were all framework code, the application logic only showed up near the top with a couple percent each, nothing to grab. Top-down, the widest leaves were "map get/put calls," which is true and useless, every service on earth spends time in hash maps.

So they did the third thing: middle-out. Collapse the framework frames, filter down to the application's own packages, and re-merge. Now one package stood out at "almost 44% of CPU samples," and inside it a single legacy method was sitting on "25% of CPU samples" all by itself. That method already had a dynamic flag to turn it off, and when they checked, it "no longer returns distinct results." It was dead weight that nobody had noticed because nobody could see it. They flipped the flag on a canary and watched CPU and latency both drop.

<figure class="cache-bench">
  <h3>Before and after, as a share of all CPU samples</h3>
  <div class="cb-panel-title">the hot application package</div>
  <div class="cb-bar-row"><span>before</span><span class="cb-track"><span class="cb-fill" style="--value:44%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~44%</span></div>
  <div class="cb-bar-row"><span>after</span><span class="cb-track"><span class="cb-fill" style="--value:18%;--bar:var(--cb-green)"></span></span><span class="cb-value">~18%</span></div>
  <div class="cb-panel-title" style="margin-top:0.9rem;">the dead legacy method inside it</div>
  <div class="cb-bar-row"><span>before</span><span class="cb-track"><span class="cb-fill" style="--value:25%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~25%</span></div>
  <div class="cb-bar-row"><span>after</span><span class="cb-track"><span class="cb-fill" style="--value:0.4%;--bar:var(--cb-green)"></span></span><span class="cb-value">&lt;1%</span></div>
  <figcaption>Bars are literal: full track = 100% of CPU samples. The package fell from ~44% to ~18%, the one method from ~25% to a fraction of a percent. Numbers reported in Netflix's post linked at the top, not measured by me.</figcaption>
</figure>

The headline is the arithmetic on the other side of that flag. One dead method, gone, "reduces the service's computation time by more than 13 million minutes (or almost 25 years) of CPU time per day." Not per year. Per day. That's the shape of a fleet: a small percentage of a lot of machines is an absurd number, and the only reason anyone found the method to delete is that a flame graph made 25% of the samples point at a single name.

## A real one, for shape

Everything above is Netflix's story and my own diagram. If you want to see a genuine captured Java flame graph, here's one I profiled myself on a regex that backtracks on every non-matching log line:

![Flame graph of a regex hot path, with java/util/regex Pattern$CharPropertyGreedy.match and Pattern$Slice.match as two wide boxes stacked above String.matches, together about 88% of the width](/images/posts/java-high-cpu-debugging/flame-regex-bad.jpg)

Same reading rules. The two widest boxes on the top edge, `CharPropertyGreedy.match` and `Pattern$Slice.match`, are about 88% of the samples between them, and you can read straight down to `String.matches` to see how the code got there. That's the [whole lab, numbers and all](/blog/the-cpu-number-that-didnt-move/), if you want measured instead of borrowed.

## The takeaway

A flame graph converts "the CPU is busy" into "the CPU is busy doing this specific, nameable thing, reached this specific way," and that second sentence is the only one you can act on. Read it by width, not height, and remember the top edge is what's actually running.

Two things worth keeping:

- Getting a language *into* the profiler can be its own project. For Java that meant naming JIT'd methods and paying 0 to 3% CPU to keep the frame pointer honest, and it was worth every fraction of a percent. If your runtime shows nothing useful in `perf`, that's usually the problem, not the absence of one.
- The obvious reads miss the win. Bottom-up drowned in framework, top-down drowned in hash maps, and the 25% method only appeared once they filtered to their own code and looked in the middle. When a flame graph looks like it has no answer, try collapsing the frames that aren't yours before you conclude there's nothing there.
