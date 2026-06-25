---
layout:     post
title:      What a Span Actually Costs
date:       2025-08-04
description:    "OpenTelemetry instrumentation is not free, but the SDK is rarely where it hurts. A span costs a few microseconds and each attribute adds a bit more, which almost never matters. What matters is the span processor: SimpleSpanProcessor exports on the request thread and pays the full collector round trip on every single call, and the fix is a batch processor plus head sampling. Here is the reproduction, in microseconds."
categories: opentelemetry observability tracing performance instrumentation
---

Someone on the team wired up tracing on a Friday. Not a big change, the auto-instrumentation plus a few manual spans around the parts we actually cared about, the kind of diff that looks harmless in review because it is mostly imports and one `TracerProvider` setup. It merged. By Monday the p99 on one endpoint had gone from single-digit milliseconds to something that showed up on a dashboard nobody had looked at in a while, and the first instinct, the wrong one, was that OpenTelemetry is heavy and we should rip it out.

OpenTelemetry is not heavy. A span is a few microseconds. What was heavy was the one line nobody reads, the span processor, which in the copied-from-a-tutorial setup was exporting every span synchronously on the request thread and paying the full trip to the collector before the handler could return. That bug is a ghost, it does not show up in a unit test, it does not show up under one request, it only shows up when real traffic is trying to get through the same doorway.

## The problem

Adding tracing to a hot path has two very different costs and they get lumped together. One is the SDK cost, the price of creating a span and recording attributes in memory, and it is small. The other is the export cost, the price of shipping that span to a collector, and it is not small, it is a network round trip. The mistake is letting the second one happen on the thread that is trying to answer the request. Do that and you have not added observability, you have added a synchronous network call to every single request, and you measured it as latency because that is exactly what it is.

So I built a harness to separate the two. One experiment for the raw SDK cost of a span, one for what the processor choice does to request latency, one for what sampling buys back. Everything below is measured, laptop numbers, arm64, otel-sdk 1.29.0 on Python 3.9.6.

## What a span actually costs

First the honest baseline. A tight loop doing a fixed little unit of work, sixty-four float multiplies, and then the same loop wrapped in a span, with a no-export processor so we are only paying for the SDK and nothing hits the network. Then the same span with 5 attributes and with 20, because attributes are where people get careless.

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
  <h3>Cost per operation, span vs no span (in-process, no export)</h3>
  <div class="cb-bar-row">
    <span>no tracing</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 4.9%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">1,211 ns</span>
  </div>
  <div class="cb-bar-row">
    <span>span, 0 attrs</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 38.7%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">9,632 ns</span>
  </div>
  <div class="cb-bar-row">
    <span>span, 5 attrs</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 54.9%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">13,651 ns</span>
  </div>
  <div class="cb-bar-row">
    <span>span, 20 attrs</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">24,885 ns</span>
  </div>
  <figcaption>An empty span adds about 8.4 microseconds over the bare work (1,211 ns to 9,632 ns). Five attributes push it to 13.7 microseconds, twenty to 24.9 microseconds, so an attribute costs roughly 800 ns of the total. Real numbers, but keep the scale in mind: even the 20-attribute span is 25 microseconds. If your handler does one database query it has already spent a thousand times that. Measured on otel-sdk 1.29.0, Python 3.9.6, 300k iterations per arm, results in benchmarks/otel-sdk-overhead/results/.</figcaption>
</figure>

Read that chart the right way. Yes, a span is roughly eight times the cost of the trivial work I gave it, and yes attributes add up, put twenty attributes on a span in a loop that runs a million times and you will feel it. But the units are microseconds. Nobody's endpoint got slow because of 25 microseconds. The SDK is not your problem. I wanted that number on the table first so that when the next chart looks alarming, you know it is not the span creation that did it.

## The processor is where it actually hurts

Same span, a handful of attributes, but now we export it. I modeled the collector as a 5ms round trip, which is generous for a call leaving the box and coming back, and ran the same request twice: once with `SimpleSpanProcessor`, which exports each span as it ends, and once with `BatchSpanProcessor`, which queues spans and ships them in the background. Five thousand requests, single threaded, measuring the latency the caller actually sees.

<figure class="cache-bench">
  <h3>Per-request p99 latency, Simple vs Batch (export RTT modeled at 5 ms)</h3>
  <div class="cb-bar-row">
    <span>SimpleSpanProcessor</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">6.615 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>BatchSpanProcessor</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 0.7%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">0.046 ms</span>
  </div>
  <figcaption>Simple pays the full 5 ms export round trip on the request thread, every request: p50 6.372, p90 6.495, p99 6.615, and a ceiling of about 160 requests/sec because each one waits for the collector. Batch hands the span to a background queue and returns: p50 0.016, p90 0.029, p99 0.046, and 49,476 requests/sec on the same loop. That green bar is 0.046 ms next to 6.615, it is drawn at the 2px minimum so you can see it at all. Two orders of magnitude, same span, same attributes, one line of setup different. Measured on otel-sdk 1.29.0, results in benchmarks/otel-sdk-overhead/results/.</figcaption>
</figure>

This is the whole post in one chart. The span cost the same microseconds in both runs, the difference is who waits for the export. `SimpleSpanProcessor` exports inline, so the handler blocks on the round trip and your p99 becomes the collector's latency plus your work, and your throughput collapses to whatever the collector can ack, 160 requests a second in this run. `BatchSpanProcessor` drops the span into a queue and a background thread flushes it, so the request thread pays almost nothing and gets its throughput back, 49,476 a second here.

The trap is that `SimpleSpanProcessor` shows up in every quickstart because it is the simplest thing to write and it works perfectly in the demo, where the collector is localhost and there is one request at a time. Ship it and the collector is a hop away and there are thousands of requests at a time, and now every one of them is standing in line for a network call you did not know you added. Ask me how I know.

## Sampling is the knob

Batching moves the export off the hot path, but you are still creating and recording every span, and at real volume the SDK cost from the first chart plus the queue pressure is not nothing. The other knob is head sampling: decide at span creation whether this trace is one you will keep, and if it is not, the span is non-recording, it skips the attribute writes and never gets exported. I ran the instrumented path with `BatchSpanProcessor` to a real collector container and turned the `TraceIdRatioBased` sampler from keep-everything down to keep-nothing.

<figure class="cache-bench">
  <h3>Hot-path throughput vs head-sampling ratio</h3>
  <div class="cb-bar-row">
    <span>sample 100%</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 13.6%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">31,932 ops/s</span>
  </div>
  <div class="cb-bar-row">
    <span>sample 10%</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 52.3%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">122,831 ops/s</span>
  </div>
  <div class="cb-bar-row">
    <span>sample 1%</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 94.5%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">222,024 ops/s</span>
  </div>
  <div class="cb-bar-row">
    <span>sample 0%</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">234,866 ops/s</span>
  </div>
  <figcaption>At 100% the path does 31,932 ops/sec and the collector received 138,766 spans. Drop to 10% and throughput more than triples to 122,831 (29,748 exported), at 1% it is 222,024 (3,022 exported), and at 0% it is 234,866, essentially the uninstrumented ceiling. Unsampled spans are non-recording, so they skip attribute recording and export entirely, which is why the exported count tracks the ratio and the throughput climbs as you sample less. Measured on otel-sdk 1.29.0 against otelcol-contrib, 300k iterations per ratio, results in benchmarks/otel-sdk-overhead/results/.</figcaption>
</figure>

The shape is what you want it to be, throughput rises smoothly as the sample ratio falls, because an unsampled span is close to free. Going from 100% to 10% roughly quadruples the hot-path throughput and you still keep one trace in ten, which for most services is plenty to see the shape of your traffic and every error if you sample errors separately at the tail. One honest wrinkle from the run: at 100%, pushing 300k spans single threaded as fast as the loop can go, the batch queue overflowed and the collector only received 138k of them. That is the queue doing its job, dropping under pressure rather than blocking the producer, and it is exactly the backpressure story you want, but it is worth knowing the drop is there.

## The takeaway

Instrumentation is cheap. Export is not, and the only real decision is whether the export blocks the request. Three things, in order of how much they matter:

Use `BatchSpanProcessor`, never `SimpleSpanProcessor`, anywhere a request is waiting. This is the entire p99 story, one line of setup, two orders of magnitude. `SimpleSpanProcessor` is fine for a short-lived script or a test, and nowhere else.

Sample at the head to buy back throughput, and sample low, 1 to 10 percent is normal for high-volume services. If you need every error trace, that is what tail sampling in the collector is for, keep head sampling aggressive and let the tail decide what to persist.

Do not obsess over attributes, but do not spray them either. Twenty attributes on a span is 25 microseconds, invisible next to any real work, but twenty attributes on a span inside a million-iteration loop is a different sentence. Put spans around units of work, not around every line.

The harness, all three experiments and the raw CSVs, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/otel-sdk-overhead). These are laptop numbers meant to show the mechanism and the size of each effect, not capacity planning for your service, the RTT in the second experiment is modeled with a sleep rather than a live network so the Simple-versus-Batch contrast stays clean. Run it against your own collector before you quote a millisecond figure to anyone.
