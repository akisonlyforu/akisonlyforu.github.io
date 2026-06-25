---
layout:     post
title:      Sampled to Keep, Dropped Anyway
date:       2025-07-30
description:    Tail sampling in the OpenTelemetry Collector buffers whole traces in memory until a decision timer fires, so the memory it needs is rate times decision_wait, and at high traffic that number is bigger than the container. When the buffer overflows it drops the oldest traces first, which are exactly the error traces you turned tail sampling on to keep. Here is the reproduction, including the OOMKill loop.
categories: opentelemetry observability tail-sampling tracing performance
---

The first time I saw it, the collector pod was in a restart loop. Not crashing on a bad config, not failing a health check, just quietly climbing in memory for about half a minute and then getting OOMKilled, coming back, and doing it again. `kubectl get pods` showed a restart count going up like a clock. The config had shipped weeks earlier and worked fine in staging, where the traffic was a trickle. Production was not a trickle.

The processor doing this was `tail_sampling`. We had turned it on to be smart about cost, keep the error traces and the slow ones, throw away the boring 200s that all look the same. That part worked. The part nobody mentioned is that a tail sampler has to hold whole traces in memory long enough to decide, and "long enough" times "how many arrive" is a number you can compute, and that number was bigger than the memory limit.

## The problem

Head sampling decides at the first span: roll the dice when the trace starts, keep it or drop it, done. Tail sampling can't do that. To keep the slow traces you have to wait until the trace is finished to know it was slow, so the collector buffers every span of every trace in a ring buffer and starts a per-trace timer called `decision_wait`. Only when that timer fires does it run your policies and decide. So the traces sitting in memory at any moment are roughly the ones that arrived in the last `decision_wait` seconds, which is `rate × decision_wait`, times some headroom. At 1,000 traces/sec with a 60s wait that is 60,000 traces resident, all the time. At ~10 to 50KB a trace, that is real memory, and when it crosses the container limit the kernel kills the collector. Worse, when the ring buffer fills before your traffic slows down, the processor evicts the **oldest** traces to make room, and the oldest traces are the ones whose decision hasn't fired yet, which can be exactly the error traces you built this thing to keep.

I wanted to watch all three of those happen, so I ran a contrib collector locally, pointed a load generator at it, and read the numbers back off the collector's own metrics endpoint.

## What the processor is actually holding

The config is small. This is the whole processor:

```yaml
processors:
  tail_sampling:
    decision_wait: ${env:DECISION_WAIT}
    num_traces: ${env:NUM_TRACES}
    expected_new_traces_per_sec: ${env:EXPECTED_NEW_TPS}
    policies:
      - name: keep-errors
        type: status_code
        status_code:
          status_codes: [ERROR]
      - name: keep-slow
        type: latency
        latency:
          threshold_ms: ${env:LATENCY_THRESHOLD_MS}
```

Two policies: keep a trace if any span has status ERROR, or if the trace took longer than the latency threshold. `decision_wait` is how long each trace waits before those policies run. `num_traces` is the size of the ring buffer, the hard cap on how many traces can be in memory at once. The docs suggest sizing it as `rate × decision_wait × 2`, and that `× 2` is the whole story: it is not a safety margin you can ignore, it is telling you how much RAM the processor is going to want.

The collector exposes its internals on a Prometheus endpoint, so I didn't have to guess. The metric that mattered most was `otelcol_processor_tail_sampling_sampling_traces_on_memory`, the live count of traces in the buffer, plus `..._sampling_trace_dropped_too_early` for evictions and `..._global_count_traces_sampled` for what survived. The names carry a `sampling_` infix in this build, which I only found by curling the endpoint on the running collector, so don't trust a metric name from a blog, including this one, check yours.

## Memory tracks decision_wait, exactly like the formula says

First experiment: fix the rate at 1,000 traces/sec, give the ring buffer enough room, and turn the `decision_wait` dial from 5s up to 60s. Then watch the collector's resident memory. If the formula is real, memory should climb with the wait.

It does.

<figure class="cache-bench">
  <h3>Collector RSS vs decision_wait (rate fixed at 1,000 traces/sec)</h3>
  <div class="cb-bar-row">
    <span>decision_wait 5s</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 23.2%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">133.5 MB</span>
  </div>
  <div class="cb-bar-row">
    <span>decision_wait 15s</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 38.2%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">220.0 MB</span>
  </div>
  <div class="cb-bar-row">
    <span>decision_wait 30s</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 55.7%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">320.9 MB</span>
  </div>
  <div class="cb-bar-row">
    <span>decision_wait 60s</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">576.0 MB</span>
  </div>
  <figcaption>Peak resident memory climbs from 133.5 MB at a 5s wait to 576 MB at 60s, same rate the whole time. The 60s bar is orange because 576 MB is already past a 512 MB container limit, which is the exact config from the story that OOMed. Traces held in memory peaked at 10,000 / 30,000 / 30,000 / 85,000 for the four waits. Above 30s the generator can't keep pushing the full rate once the collector is under memory pressure, so the buffer never fully fills to its formula cap, which is why the count flattens even though RSS keeps climbing. Measured on otelcol-contrib v0.156.0, ~2.5KB per trace, results in benchmarks/otel-tail-sampling/results/.</figcaption>
</figure>

Nothing about the traffic changed between those four bars. Same rate, same trace size, same everything. The only thing I moved was how long each trace sits in the buffer before the collector decides, and the memory followed it up to 576 MB. If your container has a 512 MB limit, which is a completely normal limit, a 60s `decision_wait` at 1,000 traces/sec doesn't fit, and it never did. It fit in staging because staging was doing 20 traces/sec.

## The part that stings: it drops the oldest first

Memory pressure is one failure. Here is the other one, and it's the one that would have cost me a debugging night if I hadn't gone looking.

Set `decision_wait` long, 30s, so no trace gets a decision during the test. Inject 500 error traces first. Then flood the collector with normal traces, more than the ring buffer can hold, all before that 30s timer fires for anyone. The error traces are now the oldest things in the buffer. What happens to them?

<figure class="cache-bench">
  <h3>Error traces kept, out of 500 sent (buffer overflowed before any decision fired)</h3>
  <div class="cb-bar-row">
    <span>num_traces 2,000</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 0.5%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">0 / 500</span>
  </div>
  <div class="cb-bar-row">
    <span>num_traces 200,000</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">500 / 500</span>
  </div>
  <figcaption>Same 500 error traces, same flood of normal traffic behind them. With an undersized ring (num_traces 2,000) the collector evicted every single error trace before its decision fired, 100% lost, with 6,500 traces reported dropped too early. Size the ring to hold the traffic (200,000) and all 500 error traces survive, nothing dropped. The policy kept both configs identical, only the buffer size changed. Measured on otelcol-contrib v0.156.0, results in benchmarks/otel-tail-sampling/results/.</figcaption>
</figure>

Zero. With the small ring, all 500 error traces were gone before the collector ever ran the policy that was supposed to keep them. The `dropped_too_early` counter reported 6,500 traces evicted before their decision. This is the quiet failure, and it is so much worse than the OOM, because the OOM at least tells you. This one doesn't. The collector is up, it's green, it's exporting traces, your storage bill went down, and the whole time it is throwing away the errors first because they are the oldest things in a buffer that is too small. You would trust that pipeline. You would build alerts on it. And on the day something actually breaks, the trace you go looking for was evicted three hours ago to make room for a flood of healthy 200s.

The fix is the same knob that fixes the memory, `num_traces`, which is the thing I got wrong about this processor for a while. I thought `decision_wait` was the memory dial and `num_traces` was just a safety cap. It's the other way around. On this build a trace's memory isn't released the instant its decision fires, it stays in the ring until `num_traces` eviction reclaims the slot, so `num_traces` is the real memory knob, and the `rate × wait × 2` rule is really about sizing that ring.

## And then it just dies

To make sure the OOM wasn't hypothetical, I gave the container a 400 MB limit, set `decision_wait` to 90s, and pushed 3,000 traces/sec with `num_traces` sized to actually fill. The projected working set is well past 400 MB, so this should die.

It died in 67 seconds. RSS climbed to about 357 MB against the 400 MB cap, the kernel OOMKilled the container, and it came back with a restart count of 1. Left running, that is the restart loop from the top of this post, on a timer, forever, until someone lowers `decision_wait` or raises the limit or shrinks the buffer. (One honest wrinkle: after the restart the container's `State.OOMKilled` flag resets to false, so the durable evidence of the kill is the restart count going up under the cap while memory was climbing, not the flag.)

## So is it worth it

Yes, and it's not close, which is the frustrating part, because the thing that OOMs you is also genuinely good at its job. Last experiment: a realistic mix, 1% error traces and 1% slow ones, the policy keeping both, a ring big enough to decide correctly. How much traffic actually made it to storage?

<figure class="cache-bench">
  <h3>Spans stored: tail sampling keeping only errors and slow traces</h3>
  <div class="cb-bar-row">
    <span>received</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">30,000</span>
  </div>
  <div class="cb-bar-row">
    <span>exported</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 2%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">600</span>
  </div>
  <figcaption>30,000 spans in, 600 out, a 98.0% reduction, keeping every ERROR trace and everything slower than 500ms. That is the payoff, and it is why people turn this on despite the memory risk. The reduction scales with how rare your interesting traces are. Measured on otelcol-contrib v0.156.0, results in benchmarks/otel-tail-sampling/results/.</figcaption>
</figure>

98% less to store, and you kept the traces you'd actually open. That is a real bill, cut hard, for a policy that took six lines of YAML. The catch is that the 98% and the OOM are the same mechanism. You save that much precisely because the collector held everything long enough to be picky, and holding everything long enough is the thing that costs the memory.

## The takeaway

Tail sampling is worth it, and it will crash you if you treat `num_traces` as an afterthought. Before you ship it, do the arithmetic the docs are quietly handing you: take your real peak rate, multiply by `decision_wait`, multiply by 2, and that is roughly how many traces the collector will hold. Multiply that by 10 to 50KB and compare it to your container limit. If it doesn't fit, it will OOMKill on a loop the first time production traffic shows up, and long before that, if `num_traces` is set too small, it will quietly evict your oldest traces to stay under the cap, which are the error traces you turned this on to keep. None of it is hidden, the `× 2` in the formula was the warning all along. Size the buffer, cap the rate you feed it, and give it the memory the arithmetic says it needs.

The harness, the collector config, and every number in the charts are [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/otel-tail-sampling). These are laptop numbers on a single collector, meant to show the mechanism, not to size your production fleet, your trace sizes and rates are your own.

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
