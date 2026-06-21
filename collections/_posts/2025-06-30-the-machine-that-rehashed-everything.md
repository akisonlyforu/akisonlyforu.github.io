---
layout:     post
title:      The Machine That Rehashed Everything
date:       2026-07-19
description:    You shard a table across four Postgres machines with hash % 4 and everything gets faster. Then you add the fifth machine and find out that changing the divisor moves four-fifths of your rows, because the row count was baked into the address. The fix is to never hash to machines at all, and the whole design turns on the number you pick before any of that happens.
categories: postgres sharding databases scaling
---

The first four shards were easy. You pick a hash, you spread the rows across four Postgres boxes, every query that carries the key lands on exactly one of them, and for a couple of quarters you feel like you solved scaling. Then the fifth machine shows up, not because you planned for it but because four boxes aren't enough anymore, and you go to add it the obvious way, `hash(key) % 5` instead of `% 4`, and somebody asks how much data has to move. You do the arithmetic on a whiteboard and it comes back wrong, or at least wrong compared to what you assumed, because you assumed adding a fifth machine moves about a fifth of the rows and the real answer is that it moves four-fifths of them. The machine count wasn't a capacity knob you turn later. It was written into every row's address the day you sharded, and turning it rewrites all the addresses.

## The problem

When you place a row with `hash(key) % N`, the row's home shard is a function of two things: the hash, which never changes, and `N`, which is the number of machines. The hash is stable, so people think of the placement as stable, but it isn't, because `N` is sitting right there in the formula. The moment you change `N` from 4 to 5, you recompute `hash % N` for every row, and a row only gets to stay put in the rare case that `hash % 5` happens to land on the same number as `hash % 4`. Most rows don't. So the cheap-sounding operation, "add a machine," is actually "recompute the address of every row in the table, then physically move most of them to a different box, while both the old and new placement have to be live so nothing breaks mid-move." You didn't build a system you can grow. You built one that resists growing, and the resistance gets worse as the table gets bigger, which is exactly when you need to grow it.

I wanted to see the real fraction, not the whiteboard estimate, so I loaded 200,000 workspaces into Postgres, 1,202,279 rows in total (a workspace gets somewhere between 1 and 20 rows, skewed, so it looks a little like real tenants), keyed everything by workspace id, and hashed with `blake2b(str(workspace_id))`. Then I asked, for a handful of machine-count changes, how many rows have a different home under the new count than the old one. Not a model of it. The actual rows, counted in SQL.

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
.cb-bar-row { display: grid; grid-template-columns: minmax(9rem, 1.4fr) minmax(7rem, 4fr) minmax(3.8rem, 0.8fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
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

<figure class="cache-bench">
  <h3>Rows that must physically move when you change the machine count (hash % N)</h3>
  <div class="cb-bar-row">
    <span>4 &rarr; 5 machines</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 79.9%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">79.9%</span>
  </div>
  <div class="cb-bar-row">
    <span>4 &rarr; 6 machines</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 66.8%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">66.8%</span>
  </div>
  <div class="cb-bar-row">
    <span>4 &rarr; 8 machines</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 49.8%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">49.8%</span>
  </div>
  <div class="cb-bar-row">
    <span>8 &rarr; 12 machines</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 66.6%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">66.6%</span>
  </div>
  <figcaption>Out of 1,202,279 rows. 4&rarr;5 moves 961,006 rows, 4&rarr;6 moves 802,972, 4&rarr;8 moves 599,328, 8&rarr;12 moves 800,977. Doubling (4&rarr;8) is the kind case at "only" half, because a doubled divisor keeps a clean bit; every other jump is worse. Measured on PostgreSQL 16.14, results in benchmarks/postgres-logical-shards/results/.</figcaption>
</figure>

Look at the 4 to 5 bar for a second, because it's the one that catches people. You added 25% more capacity and it cost you moving 79.9% of the table. The only transition that comes in under half is 4 to 8, the doubling, and that's not because doubling is clever, it's because doubling the divisor happens to preserve one bit of the hash so half the rows keep their address by luck. You cannot schedule your growth around "only ever double," real capacity planning gives you a fifth machine and then a sixth, and every one of those off-by-one steps is a two-thirds-of-the-table migration.

## Stop hashing to machines

Here's the move, and it's the thing you back into eventually if you shard for long enough: don't hash rows to machines at all. Hash them to a fixed, large number of *logical* shards, once, and never change that number. Then keep a small lookup table that says which physical machine each logical shard currently lives on. The row's identity is `hash(key) % 480`, forever, and that value is stamped on the row the day it's written and never recomputed. The machine it sits on is one more indirection away, a table you can edit.

Now adding a machine is a completely different operation. You don't touch the hash. You pick some logical shards, change their row in the lookup table to point at the new box, and move exactly those shards' rows. Nothing else in the table is even read, let alone moved. Going from four machines to six, with 480 logical shards, you want 80 shards per machine at the end, so you leave 80 on each of the original four (320 stay put) and move 160 shards onto the two new boxes. I ran that rebalance for real, moving the rows between schemas in Postgres inside a transaction and counting what actually moved, and then I ran the modulo version on the identical data so the two sit side by side.

<figure class="cache-bench">
  <h3>Same 4 &rarr; 6 rescale, same 1.2M rows: modulo vs a lookup table</h3>
  <div class="cb-bar-row">
    <span>rows moved, hash % N</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 66.8%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">66.8%</span>
  </div>
  <div class="cb-bar-row">
    <span>rows moved, logical + lookup</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 33.4%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">33.4%</span>
  </div>
  <div class="cb-bar-row">
    <span>keys rehashed, hash % N</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 66.8%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">66.8%</span>
  </div>
  <div class="cb-bar-row">
    <span>keys rehashed, logical + lookup</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">0.0%</span>
  </div>
  <figcaption>Modulo moves 802,972 rows and recomputes the home of every key. The lookup table moves 401,550 rows, which is exactly 160 of 480 logical shards, and recomputes zero keys. Row count and the sum-of-ids checksum, (1202279, 722737998060), were bit-for-bit identical before and after the move. Measured on PostgreSQL 16.14, results in benchmarks/postgres-logical-shards/results/.</figcaption>
</figure>

Two things in that chart matter more than the halving of moved rows, and they're the bottom two bars. The first is that the modulo scheme rehashes 66.8% of your keys, and the lookup scheme rehashes 0.0% of them, and that zero is the whole point. Under modulo, "which shard does this key live on" has a different answer before and after the migration, so during the move you have to run both answers at once, double-writing and reconciling, or you take downtime. Under the lookup table the answer to `hash % 480` is the same the whole time, only the pointer moves, so a key's shard never has two truths you have to keep in sync. The second thing is that the checksum came back identical, `(1202279, 722737998060)` before and `(1202279, 722737998060)` after, which is me saying out loud that moving 401,550 rows between schemas didn't lose or duplicate one of them. The 33.4% isn't even a limit, it's just how much had to move to rebalance four boxes into six. The 8 to 12 version told the same story, 160 shards and 33.6% of rows, 0% key churn, same identical checksum.

## Why the number you pick has to be composite

There's a catch hiding in "a fixed large number of logical shards," and it's which number. You only get to choose it once, before any data exists, and you're stuck with it, so it's worth ten minutes. The thing you want is for the logical shards to divide evenly across however many machines you might ever run, because a logical shard that can't be split gets its rows dumped unevenly, one machine ends up with a shard more than its neighbor. 480 is `2^5 · 3 · 5`, which divides cleanly by 2, 3, 4, 5, 6, 8, 10, 12, 15, 16 and a pile of other counts, and that is not an accident, it's why that class of number gets picked. I mapped 480 logical shards across every machine count I could think of and did the same for 479, which is prime, and 500, which looks round but factors badly.

<figure class="cache-bench">
  <h3>Machine counts (of 11 tried) where the logical shards split perfectly evenly</h3>
  <div class="cb-bar-row">
    <span>L = 480 (2<sup>5</sup>&middot;3&middot;5)</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">11 / 11</span>
  </div>
  <div class="cb-bar-row">
    <span>L = 500</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 27.3%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">3 / 11</span>
  </div>
  <div class="cb-bar-row">
    <span>L = 479 (prime)</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">0 / 11</span>
  </div>
  <figcaption>The 11 machine counts tried: 3, 4, 5, 6, 8, 10, 12, 15, 16, 24, 32. 480 divides all of them evenly, 500 only divides 4, 5 and 10, and prime 479 never divides evenly so one machine always carries a shard more. The row imbalance stays small at low machine counts but widens as you scale: at 32 machines, 500 skews to a 1.14x heaviest-to-lightest ratio against 480's 1.06x. Derived from the same hash over the 1.2M rows; the 6-machine and 16-machine rows were confirmed against real per-schema counts. Results in benchmarks/postgres-logical-shards/results/.</figcaption>
</figure>

To be honest about what this chart is and isn't, at the scale I ran, one extra logical shard on a machine is a rounding error, a couple thousand rows out of a hundred thousand, so the row imbalance never gets dramatic and I'm not going to pretend it does. The signal here is the divisibility, not the skew: 480 splits evenly for all eleven counts, 479 for none of them, 500 for three. The skew only starts to bite at high machine counts, where 500 pulls out to a 1.14x heaviest-versus-lightest ratio while 480 stays at 1.06x. The point is that a highly composite count costs you nothing and buys you the option to run any machine count you want with even shards, and a prime or a round-but-badly-factored count quietly takes that option away for the entire life of the system, which you can't fix later because the number is frozen.

## The takeaway

The two-thirds figure is the one to carry around. `hash(key) % N` bakes the machine count into every row's address, so changing the count, which is the whole reason you shard in the first place, rewrites most of the addresses: 79.9% of my rows moved going from four machines to five, 66.8% going to six. The fix is to stop hashing to machines and hash to a fixed, large, highly composite number of logical shards instead, and let a lookup table map those to physical boxes. On the identical 1.2M-row rescale that cut rows moved from 66.8% to 33.4%, and it cut keys rehashed from 66.8% to zero, and that zero is the part that lets you rebalance without a double-write window or downtime, because a key's shard has exactly one truth the whole time. It costs you one level of indirection on every lookup and a small table you have to keep correct, and the number of logical shards is a decision you make once and live with forever, so make it something like 480 that divides by everything and not something round that doesn't. If you're sharding a table today with the machine count sitting in the modulo, the growth you think you're deferring is a two-thirds-of-the-table migration you've already signed up for, you just haven't been billed yet. [The harness, the real data movement, and all three experiments are on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-logical-shards). Laptop numbers on PostgreSQL 16.14, so the row counts are mine and yours will differ, but the fractions come out of the hash and the machine count, and those are the same wherever you run them.
