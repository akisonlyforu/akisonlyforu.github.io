---
layout:     post
title:      The Query That Asked Every Shard
date:       2026-07-18
description:    One Postgres table outgrows one machine, so you split it across eight. Then you find out that a query carrying the shard key touches one shard in 2ms, and the same query without it has to ask all eight and comes back in 12. The shard key wasn't a detail. It was the whole design.
categories: postgres sharding databases scaling
---

The first table that outgrows a single Postgres doesn't announce itself. It just gets slow in a way that adding an index doesn't fix, and then slow in a way that a bigger box doesn't fix either, and one day you're staring at a table that's most of your database and realizing there's no single machine you can buy your way onto. So you do the thing everyone eventually does, you split it across several machines and hash the rows across them. And the moment you decide that, you've quietly signed up for a much harder question than "how many shards," which is "what do I split on," because that one key decides whether every query you run touches one machine or all of them.

## The problem

When a table lives on one Postgres, every query is easy in exactly one way: there's one place the row could be. The instant you spread that table across eight databases, every query has to first answer "which of the eight." If the query knows the value you sharded on, that's one hash and you go straight to the right shard. If it doesn't, there's nowhere to look but everywhere, so you send the query to all eight, wait for the slowest one, and stitch the answers together. Same query text, same rows in the end, but one version does a hash and one round trip, and the other does eight round trips and a merge. Pick the wrong key and most of your traffic ends up in the second bucket, and you've built a system that gets slower every time you add a shard instead of faster.

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

## Logical shards, not machines

Before any of the query stuff, one idea makes the whole thing tractable: the shard is a database, not a server. I split the table across eight logical shards, `shard_0` through `shard_7`, and all eight of them can sit on one physical Postgres to start with. A tiny router in front does the only clever thing in the system, it takes the value you sharded on, hashes it, and mods by the shard count to pick which database the row lives in:

```python
def shard_for(key, n_shards):
    h = int(hashlib.md5(str(key).encode()).hexdigest(), 16)
    return h % n_shards
```

That's the router. The reason to make shards logical is that "move a shard to its own machine" then becomes an ops task and not a rewrite, you spin up a new Postgres, copy `shard_5` over, point the router at it, and nothing in the application changed because the application was already talking to `shard_5` as a name, not an address. I used one Postgres 16 with eight databases for all of this, which is faithful to how these systems actually start, and it's the routing that matters, not how many boxes the shards happen to be spread over yet.

The table is `objects(id, file_key, created_by, name, updated_at)`, sharded on `file_key`. Everything is indexed, `file_key` and `created_by` both, so nothing below is a missing-index story dressed up as a routing story. Fifty thousand rows, hashed across the eight shards by `file_key`. Now the two queries.

## The query with the shard key, and the one without

Here are the two shapes, and they look almost identical in a code review. One filters on `file_key`, the thing I sharded on. The other filters on `created_by`, which I didn't.

```python
# has the shard key -> router picks ONE shard
SELECT id, name FROM objects WHERE file_key = %s

# no shard key -> nowhere to look but everywhere
SELECT id, name FROM objects WHERE created_by = %s
```

The first one hashes the `file_key`, lands on one shard, runs one indexed lookup. The second one has no idea which shard a given `created_by` is on, because I didn't shard on `created_by`, so it has to run on all eight databases and merge the results. I ran each 500 times with varied random keys, warm connections held open per shard so I'm timing routing and not TCP handshakes, and recorded the latency:

<figure class="cache-bench">
  <h3>Same query, with and without the shard key (N=8)</h3>
  <div class="cb-bar-row"><span>WHERE file_key p50</span><span class="cb-track"><span class="cb-fill" style="--value:3.4%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.40 ms</span></div>
  <div class="cb-bar-row"><span>WHERE file_key p99</span><span class="cb-track"><span class="cb-fill" style="--value:19.3%;--bar:var(--cb-green)"></span></span><span class="cb-value">2.24 ms</span></div>
  <div class="cb-bar-row"><span>WHERE created_by p50</span><span class="cb-track"><span class="cb-fill" style="--value:36.2%;--bar:var(--cb-orange)"></span></span><span class="cb-value">4.21 ms</span></div>
  <div class="cb-bar-row"><span>WHERE created_by p99</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">11.65 ms</span></div>
  <figcaption>The shard-key query touched 1 shard and issued 500 shard-queries total across the run; the other touched all 8 and issued 4,000, scanning 49,779 rows against 2,500. p99 went from 2.24ms to 11.65ms, a 5.2x gap, on the same table for the same logical answer. Measured on PostgreSQL 16.14, results in benchmarks/postgres-sharding/results/.</figcaption>
</figure>

Five times slower at the tail, and the shape of the work explains all of it. The good query issued one shard-query per call, 500 for the run. The scatter-gather issued eight per call, 4,000 for the run, and it read 49,779 rows to find the ones it wanted where the other read 2,500. The scatter-gather isn't broken, it returns the right rows, it's just doing eight databases' worth of work to answer a question that one database could have answered if I'd sharded on the column the query actually filters by.

## What it costs as you add shards

The part that turns this from a latency footnote into an architecture decision is what happens when you grow. A query pinned to one shard doesn't care how many shards exist, it does one hash and one lookup whether there are two shards or two hundred. A scatter-gather cares a great deal, because "all of them" gets bigger every time you add one. I rebuilt the same dataset at 1, 2, 4, and 8 shards and timed both queries at each size:

<figure class="cache-bench">
  <h3>Mean latency as the shard count grows</h3>
  <svg viewBox="0 0 340 200" role="img" aria-label="Line chart: single-shard latency stays flat near 1ms while scatter-gather rises from 1.7ms to 5ms as shards go from 1 to 8" style="width:100%;height:auto;font-family:inherit">
    <line x1="42" y1="160" x2="310" y2="160" stroke="var(--cb-grid)" stroke-width="1"/>
    <line x1="42" y1="106.9" x2="310" y2="106.9" stroke="var(--cb-grid)" stroke-width="1" stroke-dasharray="2 3"/>
    <line x1="42" y1="53.8" x2="310" y2="53.8" stroke="var(--cb-grid)" stroke-width="1" stroke-dasharray="2 3"/>
    <text x="36" y="163" text-anchor="end" fill="var(--cb-muted)" font-size="9">0</text>
    <text x="36" y="110" text-anchor="end" fill="var(--cb-muted)" font-size="9">2</text>
    <text x="36" y="57" text-anchor="end" fill="var(--cb-muted)" font-size="9">4</text>
    <text x="14" y="92" fill="var(--cb-muted)" font-size="9" transform="rotate(-90 14 92)">mean ms</text>
    <polyline fill="none" stroke="var(--cb-orange)" stroke-width="2" points="42,114.25 131.33,98.01 220.67,74.69 310,27.31"/>
    <polyline fill="none" stroke="var(--cb-green)" stroke-width="2" points="42,132.38 131.33,131.32 220.67,136.68 310,138.88"/>
    <circle cx="42" cy="114.25" r="2.6" fill="var(--cb-orange)"/><circle cx="131.33" cy="98.01" r="2.6" fill="var(--cb-orange)"/><circle cx="220.67" cy="74.69" r="2.6" fill="var(--cb-orange)"/><circle cx="310" cy="27.31" r="2.6" fill="var(--cb-orange)"/>
    <circle cx="42" cy="132.38" r="2.6" fill="var(--cb-green)"/><circle cx="131.33" cy="131.32" r="2.6" fill="var(--cb-green)"/><circle cx="220.67" cy="136.68" r="2.6" fill="var(--cb-green)"/><circle cx="310" cy="138.88" r="2.6" fill="var(--cb-green)"/>
    <text x="42" y="174" text-anchor="middle" fill="var(--cb-muted)" font-size="9">1</text>
    <text x="131.33" y="174" text-anchor="middle" fill="var(--cb-muted)" font-size="9">2</text>
    <text x="220.67" y="174" text-anchor="middle" fill="var(--cb-muted)" font-size="9">4</text>
    <text x="310" y="174" text-anchor="middle" fill="var(--cb-muted)" font-size="9">8</text>
    <text x="176" y="190" text-anchor="middle" fill="var(--cb-muted)" font-size="9">shard count</text>
    <text x="188" y="120" fill="var(--cb-orange)" font-size="9">scatter-gather</text>
    <text x="150" y="150" fill="var(--cb-green)" font-size="9">single shard</text>
  </svg>
  <figcaption>Scatter-gather mean climbs 1.72 → 2.34 → 3.22 → 5.00ms as shards go 1 → 2 → 4 → 8, roughly linear in the shard count because it's doing one more shard-query each time you split. Single-shard mean sits flat at 1.04 / 1.08 / 0.88 / 0.80ms, it never notices. Plotting the mean, not p99: at sub-millisecond scale the p99 tail is jitter-dominated and lumpy (it's in the CSV), the mean is the honest signal here. Measured on PostgreSQL 16.14, results in benchmarks/postgres-sharding/results/.</figcaption>
</figure>

That's the whole argument for caring about the shard key, drawn in two lines. The green line is a query that treats sharding as free capacity, split all you like, it stays put. The orange line is a query that pays for every shard you add, and the more you scale to relieve the load, the more each of those queries costs. Ship enough orange-line queries and sharding makes your p99 worse the more you shard, which is a genuinely confusing incident to debug if you didn't see it coming.

## Why the shard key is the whole decision

The reason the shard key gets picked so carefully in these systems isn't just this one table, it's what happens when tables have to be joined. I added a `comments` table, and here's the thing you get to choose: shard it on the same key as `objects`, or a different one. Shard `comments` on `file_key` too, and a comment for a given file lands on the same shard as the file, so joining them is a single-shard join, both sides are already sitting on `shard_3` together. Shard `comments` on something else, say the comment's author, and now a file's comments are scattered across all eight shards, so to join them you're back to asking everyone.

I built both. `comments` colocated on `file_key`, and a `comments2` sharded on `author`, then ran the same logical join, "this object and its comments," against each:

<figure class="cache-bench">
  <h3>Joining objects to comments: same key vs different key (N=8)</h3>
  <div class="cb-bar-row"><span>colocated p50</span><span class="cb-track"><span class="cb-fill" style="--value:1.5%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.31 ms</span></div>
  <div class="cb-bar-row"><span>colocated p99</span><span class="cb-track"><span class="cb-fill" style="--value:9.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">2.07 ms</span></div>
  <div class="cb-bar-row"><span>cross-shard p50</span><span class="cb-track"><span class="cb-fill" style="--value:31.8%;--bar:var(--cb-orange)"></span></span><span class="cb-value">6.67 ms</span></div>
  <div class="cb-bar-row"><span>cross-shard p99</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">20.96 ms</span></div>
  <figcaption>Colocated join stays on 1 shard, p99 2.07ms. Cross-shard join fans out to all 8 and merges, p99 20.96ms, a 10.1x gap for the identical result. The only difference is which column I sharded the second table on. Measured on PostgreSQL 16.14, results in benchmarks/postgres-sharding/results/.</figcaption>
</figure>

Ten times slower at the tail, and the only decision that produced it was made once, at table-creation time, months before this join was ever written. That's the whole reason the shard key gets picked so carefully. You're not choosing how to store rows, you're choosing which future queries get to be cheap, and the queries that filter or join on the shard key stay on one machine forever while everything else pays the fan-out tax. Get related data onto the same key and your common paths are single-shard by construction. Miss it, and you find out the expensive way, one confusing p99 graph at a time.

## The takeaway

Sharding a table is easy, the hash is four lines. The hard and permanent part is the shard key, because it silently sorts every query you'll ever write into two piles: the ones that carry the key and touch one shard, and the ones that don't and touch all of them. In my runs that was 2.24ms versus 11.65ms for a plain lookup, and 2.07ms versus 20.96ms for a join, same rows, same answer, decided entirely by which column the data was keyed on. So before you pick a shard key, look at the queries you run most and the joins you can't avoid, and shard on the thing they filter by, so your hot paths land on one shard and your fan-outs are the rare exception you tolerate instead of the default you built. And keep the shards logical, databases behind a router, so the day one shard gets hot you move it to its own machine without touching a line of application code. [The router, the schema, and all three experiments are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-sharding). Laptop numbers on PostgreSQL 16.14, so read the ratios and not the absolute milliseconds, but the one-shard-versus-all-shards split is the same wherever you run it.
