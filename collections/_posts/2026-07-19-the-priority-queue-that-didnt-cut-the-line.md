---
layout:     post
title:      The Priority Queue That Didn't Cut the Line
date:       2026-07-19
description:    One queue carried the marketing blast and the OTPs. I added a priority queue and the OTP still waited 3.8 seconds behind the backlog. The culprit was prefetch, not priority. Here's the reproduction.
categories: rabbitmq queues priority-queue notifications operations
---

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

A notification service starts life as one queue. OTPs go in it, password resets go in it, payment receipts go in it, and the 9 a.m. marketing blast goes in it too. This is fine for a long time, because the queue is usually near empty and everything drains the moment it lands. Then marketing schedules a campaign, twenty thousand "you left something in your cart" messages hit the queue in one burst, and somewhere in the middle of that a customer tries to log in and waits fifteen seconds for a code that should have taken twenty milliseconds. Support gets tickets. Marketing did nothing wrong. The queue did exactly what you built it to do.

## The problem

One queue can't tell the difference between traffic that can wait and traffic that can't. A FIFO queue serves in arrival order, so once a bulk campaign is sitting in front of your OTP, the OTP waits for the entire campaign to drain first. It doesn't matter that the OTP is the one message in there a human is actively staring at. The fix everyone reaches for is a priority queue, and the surprising part is that adding one, by itself, barely helped. The OTP still waited almost four seconds. The reason had nothing to do with priority and everything to do with a setting most people never touch.

I reproduced all of it on a single RabbitMQ 4.0.9 broker on my laptop. Twenty thousand bulk messages, then two hundred OTPs injected after the backlog was already enqueued and draining, and I measured how long each OTP sat from publish to the moment a consumer actually got it. Same load every time, only the queue config changed.

## What FIFO does to an OTP

First the naive setup: one plain queue, no priorities. The consumer pulls messages in order and spends about half a millisecond on each, so it drains around 1,300 a second. The backlog is twenty thousand deep. Do the arithmetic and an OTP landing at the back has to wait for roughly twenty thousand messages to clear before its turn, which is exactly what happened.

<figure class="cache-bench">
  <h3>OTP p99 latency, single FIFO queue</h3>
  <div class="cb-bar-row"><span>A · FIFO, prefetch 5000</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">15,282.7 ms</span></div>
  <figcaption>Two hundred OTPs injected behind a 20,000-message backlog, drained at ~1,273 msg/s. p50 was 14,857.7 ms, p99 15,282.7 ms, max 15,290.7 ms. The OTP waits for the whole campaign because the queue has no way to know it's urgent. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-priority-hol/results/.</figcaption>
</figure>

Fifteen seconds at p99. A login code that arrives fifteen seconds late has already failed, the user has hit resend twice and filed a ticket. So you do the obvious thing.

## The obvious fix, and why it didn't work

RabbitMQ has priority queues built in. You declare the queue with a max priority and publish urgent messages higher:

```python
channel.queue_declare(queue="notify", durable=True,
                      arguments={"x-max-priority": 10})

# bulk campaign
channel.basic_publish(exchange="", routing_key="notify", body=payload,
                      properties=pika.BasicProperties(priority=0))

# OTP
channel.basic_publish(exchange="", routing_key="notify", body=payload,
                      properties=pika.BasicProperties(priority=9))
```

Same load as before, OTPs now at priority 9, bulk at 0. I expected this to be the whole story. It wasn't.

<figure class="cache-bench">
  <h3>OTP p99 latency, priority queue with prefetch 5000</h3>
  <div class="cb-bar-row"><span>A · FIFO</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">15,282.7 ms</span></div>
  <div class="cb-bar-row"><span>B · priority, prefetch 5000</span><span class="cb-track"><span class="cb-fill" style="--value:24.95%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3,813.2 ms</span></div>
  <figcaption>Adding priority dropped p99 from 15,282.7 ms to 3,813.2 ms. Better, and still a disaster. 3.8 seconds is not a jump to the front of the line. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-priority-hol/results/.</figcaption>
</figure>

Priority helped, in the sense that fifteen seconds became four. But four seconds is not what "priority" is supposed to buy you, and the number itself is the clue. The consumer drains about 1,300 a second and its prefetch was set to 5,000. Divide 5,000 by 1,300 and you get about 3.8 seconds. The OTP wasn't waiting behind the twenty-thousand-message backlog anymore. It was waiting behind the five thousand messages the consumer had already pulled out of the queue.

## Prefetch is a second queue you forgot about

Priority only sorts the messages that are still sitting in the queue in the *ready* state. The moment a consumer with a prefetch of 5,000 connects, the broker hands it up to 5,000 messages at once and marks them unacknowledged, in flight, gone from the ready set. Those messages now live in a buffer on the client side, in plain arrival order, and priority has no say over them anymore. Your urgent OTP arrives at the broker, gets sorted to the front of the ready queue correctly, and then waits for the consumer to chew through the five thousand bulk messages it already grabbed before it ever asks for the next one.

You built a priority queue and then quietly put a five-thousand-deep FIFO in front of it. The prefetch buffer is a queue too, it just doesn't look like one because it lives in your consumer and nobody named it.

## The real fix

Drop the prefetch. With a prefetch of 1, the consumer holds exactly one message at a time and asks the broker for the next one only after it acks. Every time it asks, the broker re-checks the ready queue and priority actually gets to do its job, so a fresh OTP jumps the entire backlog.

```python
channel.basic_qos(prefetch_count=1)
```

<figure class="cache-bench">
  <h3>OTP p99 latency, priority queue with prefetch 1</h3>
  <div class="cb-bar-row"><span>A · FIFO</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">15,282.7 ms</span></div>
  <div class="cb-bar-row"><span>B · priority, prefetch 5000</span><span class="cb-track"><span class="cb-fill" style="--value:24.95%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3,813.2 ms</span></div>
  <div class="cb-bar-row"><span>C · priority, prefetch 1</span><span class="cb-track"><span class="cb-fill" style="--value:0.02%;--bar:var(--cb-green)"></span></span><span class="cb-value">3.6 ms</span></div>
  <figcaption>Prefetch 1 dropped p99 from 3,813.2 ms to 3.6 ms, a roughly 1,000x improvement over the naive priority queue, and over 4,000x over plain FIFO. p50 was 2.1 ms, max 4.1 ms. The OTP now cuts the line for real. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-priority-hol/results/.</figcaption>
</figure>

Three and a half milliseconds at p99, behind a twenty-thousand-message backlog. That's the line-cutting you thought you were buying when you declared the priority queue. The only thing that changed between the four-second version and this one is a single integer.

## The cost is real, and it's throughput

Prefetch 1 isn't free, and if it were, RabbitMQ would default to it. Every message now pays a full round trip: deliver, process, ack, ask for the next. On my laptop that dropped bulk drain from about 1,300 a second to 823.

<figure class="cache-bench">
  <h3>Bulk drain throughput</h3>
  <div class="cb-bar-row"><span>A · FIFO, prefetch 5000</span><span class="cb-track"><span class="cb-fill" style="--value:97.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1,272.9/s</span></div>
  <div class="cb-bar-row"><span>B · priority, prefetch 5000</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1,309.1/s</span></div>
  <div class="cb-bar-row"><span>C · priority, prefetch 1</span><span class="cb-track"><span class="cb-fill" style="--value:62.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">823.0/s</span></div>
  <figcaption>The prefetch-1 consumer drains bulk about 37% slower because it can't pipeline, it waits for an ack before the broker sends the next message. That's the tax you pay for OTPs that cut the line. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-priority-hol/results/.</figcaption>
</figure>

So prefetch is a straight trade. High prefetch pipelines the consumer and gives you throughput, at the cost of a deep client-side buffer that priority can't reorder. Prefetch 1 gives priority full control and instant OTPs, at the cost of a third of your bulk throughput. In practice you don't want either extreme, you want a small prefetch, something like 10 or 20, low enough that the client buffer is a few messages instead of thousands, high enough that the consumer isn't idling on every round trip.

The other answer, and the one I'd actually reach for in production, is to stop making OTPs share a queue with a marketing campaign at all. Give critical traffic its own queue and its own consumers, and the bulk backlog can't get in front of it no matter how deep it is. Priority queues are the single-queue tool for when you can't or don't want to split them. Either way, the lesson is the same.

## The takeaway

A priority queue only reorders messages the broker still holds. Anything your consumer has already prefetched is a private FIFO that priority can't touch, so a high prefetch quietly rebuilds the head-of-line blocking you added the priority queue to fix. If urgent messages share a queue with a bulk backlog, keep the prefetch small, small enough that the client-side buffer can't hide a campaign in it, and measure the p99 of the urgent traffic under a real backlog rather than trusting that "priority" means what it sounds like. And if you can afford it, give the urgent traffic its own queue and skip the whole problem.

The harness that produced these numbers is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/rabbitmq-priority-hol). These are laptop numbers meant to show the shape of the effect, not a capacity statement for your broker, run it against your own setup before you trust the magnitudes.
