---
layout:     post
title:      Sharding RabbitMQ Without Reshuffling Everything
date:       2025-03-30
description:    One queue can't keep up, so you add more and hash messages across them. Then you add a queue and a naive hash reshuffles 89% of your keys mid-flight. RabbitMQ's consistent hash exchange moved 11% instead. Here's the reproduction.
categories: rabbitmq consistent-hashing queues operations
---

You start with one queue and one consumer, and for a while that's plenty. Then the consumer stops keeping up, the queue depth climbs all day and only drains overnight, and one slow message holds up everything behind it. So you do the obvious thing and add more queues with more consumers to work in parallel. And that's where it gets interesting, because now you have to decide which message goes to which queue, and every easy answer to that question is wrong in some specific way.

## The problem

You have a stream of messages that need to spread across several queues so more than one consumer can chew through them, but two things have to stay true while you do it. Messages for the same key, say the same user or the same account, have to keep landing on the same queue, otherwise two consumers process one user's events at the same time and the ordering you were relying on is gone. And you have to be able to add a queue later when traffic grows, without that addition scrambling where everything else lands.

The easy answers each miss one of those. A fanout exchange copies every message to every queue, which isn't sharding, it's duplication. Round-robin across the queues spreads the load evenly but sends one user's events to whichever queue is next, so their order is shot. Hashing in the producer, `queue = queues[hash(key) % N]`, actually gets you both even spread and same-key-same-queue, right up until the day you add a queue. The moment `N` goes from 8 to 9, `hash(key) % 8` and `hash(key) % 9` disagree for almost every key, so almost every key jumps to a different queue at once, and every ordering guarantee you had shatters during the migration.

That last one is the trap worth measuring, because it looks fine until you scale.

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

## What the consistent hash exchange does

RabbitMQ ships a plugin exchange type, `x-consistent-hash`, that does the hashing for you on the broker side. You bind your queues to it, each with a weight, and you publish with the routing key set to your partition key. The exchange hashes the routing key onto a ring, and the queue that owns that point on the ring gets the message. Same key always hashes to the same point, so it always lands on the same queue. And because it's a consistent-hash ring and not a modulo, adding a queue only steals the slice of the ring near the new queue's points, so only the keys in that slice move.

In pika it's barely any code. You declare the exchange, bind the queues with a weight as the routing key, and publish with the partition key as the routing key:

```python
channel.exchange_declare("events", exchange_type="x-consistent-hash")
for q in queues:
    channel.queue_declare(q)
    channel.queue_bind(q, "events", routing_key="1")   # "1" is the weight (ring points)

# the routing key IS the partition key; the broker hashes it
channel.basic_publish("events", routing_key=f"user-{user_id}", body=payload)
```

That's the whole swap. The producer no longer knows or cares how many queues exist, it just stamps each message with the key it should be ordered by.

I ran it against a real RabbitMQ 4.0.9 with the plugin enabled, eight queues bound at equal weight, and a pika producer and consumer, to check the two things I actually cared about: does the hash spread evenly, and what does adding a queue cost.

## It spreads evenly

First the boring-but-necessary one. I published 100,000 messages across 43,126 distinct routing keys into the eight queues and counted where they landed. Ideal is 12,500 per queue:

<figure class="cache-bench">
  <h3>100,000 messages across 8 equal-weight queues</h3>
  <div class="cb-bar-row"><span>chx.a.q0</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,963</span></div>
  <div class="cb-bar-row"><span>chx.a.q1</span><span class="cb-track"><span class="cb-fill" style="--value:94.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,208</span></div>
  <div class="cb-bar-row"><span>chx.a.q2</span><span class="cb-track"><span class="cb-fill" style="--value:97%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,571</span></div>
  <div class="cb-bar-row"><span>chx.a.q3</span><span class="cb-track"><span class="cb-fill" style="--value:98.7%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,795</span></div>
  <div class="cb-bar-row"><span>chx.a.q4</span><span class="cb-track"><span class="cb-fill" style="--value:95.6%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,393</span></div>
  <div class="cb-bar-row"><span>chx.a.q5</span><span class="cb-track"><span class="cb-fill" style="--value:96.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,468</span></div>
  <div class="cb-bar-row"><span>chx.a.q6</span><span class="cb-track"><span class="cb-fill" style="--value:95.4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,373</span></div>
  <div class="cb-bar-row"><span>chx.a.q7</span><span class="cb-track"><span class="cb-fill" style="--value:94.3%;--bar:var(--cb-blue)"></span></span><span class="cb-value">12,229</span></div>
  <figcaption>Ideal is 12,500 per queue. The busiest queue held 12,963 and the quietest 12,208, a max deviation of 3.70%. That's tight even at weight 1, because the plugin drops many ring points per binding rather than one. Measured on RabbitMQ 4.0.9, results in benchmarks/rabbitmq-consistent-hash/results/.</figcaption>
</figure>

The busiest queue was 3.70% over ideal, the quietest a bit under, and everything else clustered in between. If a queue needs to carry more, you give its binding a bigger weight and it takes proportionally more of the ring. Good enough to move on.

## What adding a queue costs

This is the one that matters. I took 10,000 distinct keys and recorded which queue each landed on with eight queues. Then I added a ninth queue and recorded where each key landed again, and counted how many keys had moved. Then I did the same accounting for the naive `hash(key) % N` approach, `% 8` versus `% 9`, on the same keys:

<figure class="cache-bench">
  <h3>Keys that moved queues when going from 8 to 9 (of 10,000)</h3>
  <div class="cb-bar-row"><span>naive hash % N</span><span class="cb-track"><span class="cb-fill" style="--value:88.73%;--bar:var(--cb-orange)"></span></span><span class="cb-value">88.73%</span></div>
  <div class="cb-bar-row"><span>consistent hash exchange</span><span class="cb-track"><span class="cb-fill" style="--value:10.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">10.90%</span></div>
  <figcaption>Adding one queue moved 8,873 of 10,000 keys under modulo hashing, and 1,090 under the consistent hash exchange. 10.90% is essentially the textbook 1/9 (11.1%). Only the keys near the new queue's ring points move. The other 89% stay exactly where they were, and keep their ordering.</figcaption>
</figure>

The modulo version moved 8,873 of the 10,000 keys. Adding a single queue picked up almost every key and dropped it somewhere new, which during a live migration means almost every in-flight user briefly has messages in two queues at once, being drained by two consumers, out of order. The consistent hash exchange moved 1,090, which is 10.90%, near enough the 1/9 you'd predict. The other 89% of keys never noticed the ninth queue existed.

## Same key, same queue

The reason the low remap number matters is that it protects ordering, so I checked ordering directly too. I published three copies of each of 10,000 keys through the eight-queue ring and then looked at how many queues each key's messages ended up spread across. Every key landed in exactly one queue, zero keys showed up in more than one. So as long as each queue has a single consumer draining it, every key is processed in order, and adding a queue only disturbs order for the ~11% of keys that actually move, briefly, instead of all of them.

## The takeaway

If you need to spread a stream across queues so several consumers can keep up, and you need messages for the same key to stay ordered, the consistent hash exchange gives you both, and it lets you add capacity later without reshuffling the keys that were fine where they were. Bind your queues with weights to size them, publish with your partition key as the routing key, and let the broker do the hashing. Two things to keep in mind: it's a plugin, so it has to be enabled (`rabbitmq_consistent_hash_exchange`), and the per-key ordering only holds if each queue has one consumer, because competing consumers on a single queue will reorder its messages themselves. Get those right and adding a queue costs you 11% of your keys for a moment instead of 89%, [the broker, the harness, and the three experiments are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/rabbitmq-consistent-hash). Laptop numbers, RabbitMQ 4.0.9, but the consistent-hashing behaviour is the same wherever you run it.
