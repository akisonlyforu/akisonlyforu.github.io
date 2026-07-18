---
layout:     post
title:      Cranking Up the Confirm Window Made RabbitMQ Slower
date:       2025-05-14
description:    Turn on publisher confirms, throughput drops, so you widen the in-flight window to win it back. Past a moderate sweet spot, widening it made throughput go down, not up. And fire-and-forget's higher number turned out to be a backlog, not throughput. Measured with PerfTest.
categories: rabbitmq flow-control throughput operations
---

You turn on publisher confirms because fire-and-forget isn't good enough, you need to know the broker actually took the message. And the moment you do, your publish rate drops, because now a publisher can only have so many messages outstanding and unconfirmed at once before it has to stop and wait for acks. So you do the obvious thing to win the throughput back, you widen that in-flight window and let more messages sit unconfirmed at a time, more in flight means more pipelining means more speed. Up to a point. Past that point, widening it further made my throughput go down.

## The problem

Publisher confirms put a cap on how many messages a publisher can have in flight unconfirmed at once, and that cap is the knob. Set it to 1 and you're in lockstep, publish a message, wait for the ack, publish the next one, which spends most of its time waiting on round trips. So the instinct is to make the window big, hundreds or thousands, so the publisher basically never has to stop and wait. The trouble is that a big window lets the publisher outrun what the broker and the consumers can actually drain, the messages pile up inside the broker, and the flow-control machinery underneath starts pushing back, so past a certain point a wider window gives you less throughput, not more. And the other instinct, ripping confirms out entirely to go as fast as possible, is worse than it looks, because the bigger publish number you get is mostly messages backing up in a queue nobody is draining fast enough.

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

## How the window behaves

RabbitMQ moves a message through a chain of Erlang processes, and each of those has a bounded buffer, a number of credits it hands the stage feeding it. Confirms plus a bounded in-flight window keep the publisher roughly in step with that chain. Too small a window and the publisher wastes its time on round trips. Too large and it floods the chain, work backs up inside the broker, and everything slows down together. There's a middle, and it's narrower than you'd guess.

I ran it against a real RabbitMQ 4.0.9 broker, one quorum queue with persistent messages, 30 producers and 4 consumers pushing 1KB messages, using PerfTest, and swept the confirm window (`-c N`) from 1 up to 1000.

## The sweep

<figure class="cache-bench">
  <h3>Publish throughput vs confirm window (in-flight limit)</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="cw-title cw-desc">
    <title id="cw-title">Publish throughput against the publisher-confirm in-flight window</title>
    <desc id="cw-desc">Throughput climbs from window 1, peaks at window 16 around 36,600 msg/s, then declines through 256.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="26" y="214">0</text>
    <text x="10" y="124">19k</text>
    <text x="10" y="34">38k</text>
    <polyline class="curve" points="90,165 147,86 203,60 260,45 317,37 373,39 430,53 487,79 543,96 600,76" />
    <circle cx="317" cy="37" r="5" style="fill:var(--cb-blue)" />
    <text x="300" y="26">peak</text>
    <text x="86" y="230">1</text>
    <text x="308" y="230">16</text>
    <text x="478" y="230">256</text>
    <text x="588" y="230">1000</text>
  </svg>
  <figcaption>Send rate in msg/s, confirm window on the x-axis (1, 2, 4, 8, 16, 32, 64, 128, 256, 1000). Lockstep at window 1 does 9,520; it peaks at window 16 (36,598) and then falls to 24,060 at window 256, about a third below the peak. Measured on RabbitMQ 4.0.9, quorum queue, 30 producers, results in benchmarks/rabbitmq-publisher-confirms/results/.</figcaption>
</figure>

Lockstep, at window 1, managed 9,520 msg/s, the publisher spending all its time waiting. It climbs fast as the window opens up, 26,196 at 2, then 31,592, then 34,838, and peaks at window 16 with 36,598 msg/s. And then it turns over. By window 128 it's down to 27,624, at 256 it's 24,060, a third slower than the peak, and cranking it all the way to 1000 didn't rescue it. So the "just make the window huge" instinct is the wrong one, the throughput lives in a moderate window, 16 on this box.

One honest caveat on that number. The RabbitMQ team, running a thousand publishers against a cluster, found the sweet spot down around 2 messages per publisher. Mine came out at 16. The exact value moves with your hardware, your producer count, your message size, so don't take 16 as a magic number. What holds across both is the shape, a hump with a peak and then a decline, not a line that keeps going up the wider you make it.

## Confirms are doing more than durability

Then the other question, why keep confirms at all if fire-and-forget is faster. And it is faster, on paper.

<figure class="cache-bench">
  <h3>Fire-and-forget vs confirms: what's published vs what's actually drained</h3>
  <div class="cb-bar-row"><span>fire-and-forget, sent</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">47,790</span></div>
  <div class="cb-bar-row"><span>fire-and-forget, received</span><span class="cb-track"><span class="cb-fill" style="--value:61.4%;--bar:var(--cb-muted)"></span></span><span class="cb-value">29,322</span></div>
  <div class="cb-bar-row"><span>confirms (-c 2), sent</span><span class="cb-track"><span class="cb-fill" style="--value:49.4%;--bar:var(--cb-green)"></span></span><span class="cb-value">23,628</span></div>
  <div class="cb-bar-row"><span>confirms (-c 2), received</span><span class="cb-track"><span class="cb-fill" style="--value:49.5%;--bar:var(--cb-green)"></span></span><span class="cb-value">23,632</span></div>
  <figcaption>msg/s. Fire-and-forget publishes 47,790 but only 29,322 is drained, so 18,468 msg/s is piling up in the queue. With a confirm window of 2, sent and received are 23,628 and 23,632, near identical. The bounded window holds the publisher to what's actually landing.</figcaption>
</figure>

Fire-and-forget, no confirms at all, published 47,790 msg/s, well above any confirmed run, which looks like a clear win until you look at how fast the consumers were draining it, 29,322 msg/s. The publisher is shoving 47,790 messages a second into a queue that's emptying at 29,322, so 18,468 messages a second are just accumulating. That's not sustained throughput, that's a backlog forming, and on a real broker that backlog eventually hits a memory or length limit and the broker slams the publishers with flow control. With confirms and a window of 2, the send and receive rates came out at 23,628 and 23,632, basically the same number, because the bounded window keeps the publisher in step with what's actually getting drained. A lower headline number, but one you can hold all day.

To be straight about it, on a 12-second run on a laptop I saw the send-versus-receive gap, not the full collapse the RabbitMQ team wrote about, the one where fire-and-forget throughput drops like a stone once the broker's memory alarms fire. You need a bigger, longer run to push it that far. But the mechanism that gets you there is sitting right in the gap between 47,790 sent and 29,322 received.

## The takeaway

Don't reach for a huge confirm window to buy back the throughput confirms cost you, there's a sweet spot and it's more moderate than the instinct says, wide enough that the publisher isn't stalling on every round trip, narrow enough that it isn't flooding the broker faster than the broker and consumers can drain it. On my box that was 16, on yours it'll be something else, so measure it with PerfTest rather than guessing. And don't turn confirms off to go faster, the bigger publish number you get is a backlog building up, not work getting done, and confirms with a bounded window are quietly doing flow control for you, keeping what you send in line with what actually lands. [The broker, the harness, and the sweep are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/rabbitmq-publisher-confirms). Laptop numbers, RabbitMQ 4.0.9, but the shape of the curve is the same wherever you run it.
