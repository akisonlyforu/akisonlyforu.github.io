---
layout:     post
title:      The Messages RabbitMQ Confirmed and Lost Anyway
date:       2026-07-18
description:    A publisher confirm is supposed to mean the broker has your message. On a classic mirrored queue I confirmed 5,000 messages and a single node failure lost all 5,000. Here's the reproduction on RabbitMQ 3.13, and what quorum queues do differently on 4.0.
categories: rabbitmq quorum-queues reliability operations
---

A publisher confirm is a promise. You send a message, the broker sends back an ack, and that ack is supposed to mean the message is safe, it's written down, it survived, you can forget about it and move on with your day. I built a system once that leaned on that promise for years and it was fine. Then one afternoon a broker node died, another took over exactly like it was supposed to, and a batch of messages that had been confirmed, acked, promised-safe, were simply gone. No error, no warning. The publisher had done everything right and the broker had told it so.

That was the classic RabbitMQ mirrored-queue trap, and it bit enough people over enough years that RabbitMQ eventually pulled mirrored queues out of the product entirely. So the question I actually wanted answered in 2026: does it still happen? I put three RabbitMQ nodes in Docker, reproduced the loss on the old model, then ran the exact same murder against the new one. Here's what came back.

## The problem

A publisher confirm on a classic mirrored queue tells you the master node has your message. That's it. It does not tell you a single mirror also has it. RabbitMQ's default when the master dies is to promote a mirror even if that mirror never caught up, because an empty queue that's online beats a queue that's gone. So the failover can hand the master's job to a mirror that was sitting there empty, and every message that only ever lived on the dead master goes down with it. Your confirm was real. It only ever covered the master.

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
.cb-group { padding-top: 0.8rem; border-top: 1px solid var(--cb-grid); }
.cb-group:first-of-type { padding-top: 0; border-top: 0; }
.cb-group-label { margin: 0 0 0.35rem; color: var(--cb-muted); font-size: 0.78rem; font-weight: 700; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .fixed { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .jittered { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
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

## What a confirm actually promises

On a mirrored queue the confirm comes back once the message is on the master and on all *synchronised* mirrors. The whole thing turns on that one word, synchronised. A mirror that joined the cluster late, or one whose sync you never finished, isn't synchronised. It's just there, empty, waiting to be told to catch up. And the default policy setting, `ha-promote-on-failure=always`, says promote a mirror when the master dies whether or not it ever caught up. Put those two together and you get the failure: you publish to the master, you get your confirms, the master dies before an empty mirror has synced, and RabbitMQ promotes the empty mirror because that's what you told it to do.

I set `ha-sync-mode=manual` to keep the mirrors from auto-catching-up, which sounds like I'm rigging it until you've watched a multi-gigabyte queue that never manages to finish syncing, or met the operator who turned auto-sync off on purpose because the sync itself stalls the queue while it runs. It's a real configuration people run in production, and it makes the loss deterministic instead of a race.

## I confirmed 5,000 messages and lost all 5,000

Three nodes, RabbitMQ 3.13.7, a durable classic queue mirrored across all three. The policy:

```
rabbitmqctl set_policy ha-all "^ha\." \
  '{"ha-mode":"all","ha-sync-mode":"manual","ha-promote-on-failure":"always"}'
```

Then the sequence, in order:

- Kill the two mirror nodes, so the queue's master is the only copy.
- Publish 5,000 persistent messages to the master with publisher confirms on, and wait for every ack.
- Start the two mirrors back up. They rejoin the cluster, but unsynchronised, `synchronised_slave_pids` is an empty list.
- Kill the master.

The publish side is the ordinary pika confirm loop, nothing clever:

```python
channel.confirm_delivery()
for i in range(5000):
    channel.basic_publish(
        exchange="", routing_key="ha.q",
        body=payload,
        properties=pika.BasicProperties(delivery_mode=2),  # persistent
    )
# with confirms on, basic_publish raises if the broker doesn't ack the message
```

The broker positively confirmed all 5,000. Then I killed the master, RabbitMQ promoted `rmq2`, one of the mirrors that had rejoined empty, and I reconnected to the survivor and drained the queue. It handed me nothing. `confirmed = 5000, recovered = 0`. Every message the broker had promised me was gone, and at no point did anything report an error. That's the part that makes it dangerous, the loss is completely silent.

## Same murder, quorum queue

Quorum queues are the replacement, and they're the reason RabbitMQ could remove mirrored queues at all. A quorum queue is a Raft group. A message isn't confirmed until a majority of the nodes have it written down, so the confirm means the message survived to more than one place before you were told it was safe, not just to whichever node happened to answer you.

Same three nodes, RabbitMQ 4.0.9, a quorum queue this time:

```python
channel.queue_declare("qq", durable=True, arguments={"x-queue-type": "quorum"})
```

Same sequence: publish 5,000 with confirms (5,000 of 5,000 acked), find the current leader through the management API, kill it. A new leader was elected, I reconnected and drained. `confirmed = 5000, recovered = 5000`. The kill that erased everything on the mirrored queue did nothing here, because by the time each message was confirmed it already lived on a majority of the nodes, and killing one node out of three leaves that majority standing.

<figure class="cache-bench">
  <h3>Confirmed messages recovered after one node failure (of 5,000)</h3>
  <div class="cb-bar-row"><span>Mirrored (3.13)</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
  <div class="cb-bar-row"><span>Quorum (4.0)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">5,000</span></div>
  <figcaption>Both brokers confirmed all 5,000 messages. After a single node failure the mirrored queue handed back 0 and the quorum queue handed back all 5,000. Measured on RabbitMQ 3.13.7 (mirrored) and 4.0.9 (quorum), results in benchmarks/rabbitmq-message-loss/results/.</figcaption>
</figure>

The two runs diverge at exactly one moment, the failover, and it's worth seeing where each queue's messages go through it:

<figure class="cache-bench">
  <h3>Queue message count through the same failover</h3>
  <svg class="cb-svg" viewBox="0 0 700 270" role="img" aria-labelledby="rmq-tl-title rmq-tl-desc">
    <title id="rmq-tl-title">Mirrored queue versus quorum queue message count across a node failure</title>
    <desc id="rmq-tl-desc">Both queues hold 5,000 messages after publishing. At failover the mirrored queue drops to 0 and stays there; the quorum queue holds 5,000 through the new-leader election and the drain.</desc>
    <line class="grid" x1="50" y1="56" x2="680" y2="56"></line>
    <line class="grid" x1="50" y1="138" x2="680" y2="138"></line>
    <line class="grid" x1="50" y1="220" x2="680" y2="220"></line>
    <text x="42" y="60" text-anchor="end">5k</text>
    <text x="42" y="142" text-anchor="end">2.5k</text>
    <text x="42" y="224" text-anchor="end">0</text>
    <text x="55" y="242" text-anchor="middle">declared</text>
    <text x="208" y="242" text-anchor="middle">5,000 confirmed</text>
    <text x="365" y="242" text-anchor="middle">cluster disrupted</text>
    <text x="523" y="242" text-anchor="middle">failover</text>
    <text x="660" y="242" text-anchor="middle">drained</text>
    <polyline class="fixed" points="50,220 208,56 365,56 523,220 680,220"></polyline>
    <polyline class="jittered" style="stroke: var(--cb-green)" points="50,220 208,56 365,56 523,56 680,56"></polyline>
  </svg>
  <div class="cb-legend">
    <span><i class="cb-swatch" style="--swatch:var(--cb-orange)"></i>Mirrored (3.13)</span>
    <span><i class="cb-swatch" style="--swatch:var(--cb-green)"></i>Quorum (4.0)</span>
  </div>
  <figcaption>On the mirrored queue an unsynchronised mirror is promoted at failover and the 5,000 confirmed messages disappear. On the quorum queue a new leader is elected and every message is still there to drain. Measured on RabbitMQ 3.13.7 and 4.0.9, results in benchmarks/rabbitmq-message-loss/results/.</figcaption>
</figure>

## The catch

That safety isn't free, and the cost shows up the moment you lose the majority. I took the same quorum queue and removed nodes one at a time, publishing 50 confirmed messages at each step:

<figure class="cache-bench">
  <h3>Publisher confirms that succeeded as nodes are removed (of 50)</h3>
  <div class="cb-bar-row"><span>3 of 3 (majority)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">50 / 50</span></div>
  <div class="cb-bar-row"><span>2 of 3 (majority)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">50 / 50</span></div>
  <div class="cb-bar-row"><span>1 of 3 (minority)</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0 / 50</span></div>
  <figcaption>With a majority alive, two or three of the three nodes, every publisher confirm succeeds. Drop to a single node and there's no majority left to agree the write happened, so all 50 confirms block instead of acking a message the cluster can't guarantee. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-message-loss/results/.</figcaption>
</figure>

With two of three nodes up, every confirm still went through, because two is a majority of three. Drop to one node and the confirms just stopped, all 50 of them blocked. There's no majority left to agree a write happened, so the queue refuses the write rather than accept one it can't stand behind. Your publisher blocks and waits instead of getting an ack it can't trust. That's the opposite of what the mirrored queue did, and it's the trade you're actually making: a minority failure costs you nothing, and a majority failure stops your writes instead of losing them. You have to build the publisher side expecting to occasionally be told to wait.

## The takeaway

If you're still on RabbitMQ 3.x with classic mirrored queues, this failure is live, and publisher confirms will not save you from it, a confirm on a mirrored queue only ever meant the master had the message. Go look at what `ha-promote-on-failure` is set to before you do anything else. On 4.x the decision is made for you, mirrored queues are gone and quorum queues are the default, and that removal is the whole point, the old thing could lose data it swore it had. Just know the shape of what you signed up for: your messages survive losing a minority of the cluster, and in exchange, losing a majority stops writes rather than dropping them, so set your publisher timeouts and back-pressure with that in mind.

Three nodes, two RabbitMQ versions, the full failover sequence and the raw numbers [are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/rabbitmq-message-loss). Laptop numbers on RabbitMQ 3.13.7 and 4.0.9, but the behaviour, confirmed-then-lost on an unsynced promotion, nothing lost on a quorum majority, is the same wherever you run it. Both builds confirmed all 5,000 messages. Only the 4.0 one still had them a minute later.
