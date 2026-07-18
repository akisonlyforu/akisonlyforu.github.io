---
layout:     post
title:      The Read That Couldn't See Its Own Write
date:       2026-07-18
description:    You point your reads at a replica pool to take load off the primary, and then a user saves a setting, the page reloads, and the old value comes back. The write was fine. The read went to a replica that hadn't heard yet. Here's the LSN gate that gives you read-your-writes without giving up the replica.
categories: postgres replication databases scaling
---

You change your display name, hit save, the page reloads, and the old name is still sitting there. You refresh once more and now it's right. Nothing was broken. The write went through the first time you clicked. You just read it back off a replica that hadn't heard about it yet, and by the time you refreshed, it had.

This is one of those bugs that never shows up on your machine, because on your machine the primary and the replica are the same thing. It shows up in production, on the request right after a write, for the one user who was watching.

## The problem

Every read-heavy app eventually wants read replicas. The primary can only do so much, most of your traffic is reads, so you stand up a couple of streaming replicas and point reads at them and let the primary spend its time on the writes. It works. Your primary load drops, your dashboards look great, everybody's happy.

Then the support ticket comes in: "I saved it, it didn't save, so I saved it again." The write landed the first time. The read that came back a few hundred milliseconds later went to a replica that was a few hundred milliseconds behind, and a few hundred milliseconds is a long time when the read is the very next request the user makes. They wrote to the primary and read from a replica that hadn't caught up, and the two of you disagreed about what just happened.

The fix everyone reaches for first is "send that user's reads to the primary for a bit." Which works, and also quietly undoes the entire reason you built the replicas. There's a better version, and it's the one you gate on the write-ahead log instead of on a stopwatch.

## Where the lag comes from

Streaming replication is the primary shipping its write-ahead log — the WAL — to the replicas, which replay it. A write commits on the primary the instant the WAL record is durable there. The replica gets that record over the wire, then applies it. The gap between "committed on the primary" and "applied on the replica" is your replication lag, and it's never exactly zero — it's network, plus replay, plus whatever else the replica is busy with.

Under normal load that gap is small and jumpy, which makes it miserable to reason about. So for the benchmark I made it a knob instead of a mood: PostgreSQL has a `recovery_min_apply_delay` setting that tells a standby to hold each record for a fixed time before applying it. I set it to 250ms on the replica. Now the lag isn't a weather system, it's a number I chose, and I can go measure exactly how the stale reads track it.

The setup is a Postgres 16 primary and one streaming replica in Docker, the replica doing a `pg_basebackup` off the primary on first boot and then coming up in standby with that 250ms apply delay pinned on. Before trusting a single number I checked the replica was actually streaming — `pg_is_in_recovery()` true on the replica, one walsender in `state = streaming` on the primary. It was.

## How long is the window

First question: if I write a row and then read it back off the replica after waiting D milliseconds, how often is the read stale? So I did exactly that — update a row on the primary, wait D, read it off the replica, check whether the replica had the new value yet. 300 trials at each delay.

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
.cb-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.cb-panel-title { margin: 0 0 0.55rem; color: var(--cb-muted); font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.cb-bar-row { display: grid; grid-template-columns: minmax(6.5rem, 1.2fr) minmax(7rem, 4fr) minmax(3.8rem, 0.8fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .p999 { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .p50 { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
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
<h3>Stale reads off the replica, by how soon after the write you read</h3>
<div class="cb-bar-row"><span>0 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">100.0%</span></div>
<div class="cb-bar-row"><span>50 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">100.0%</span></div>
<div class="cb-bar-row"><span>100 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">100.0%</span></div>
<div class="cb-bar-row"><span>200 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span><span class="cb-value">100.0%</span></div>
<div class="cb-bar-row"><span>250 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 4.7%; --bar: var(--cb-orange);"></span></span><span class="cb-value">4.7%</span></div>
<div class="cb-bar-row"><span>300 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-orange);"></span></span><span class="cb-value">0.0%</span></div>
<div class="cb-bar-row"><span>500 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-orange);"></span></span><span class="cb-value">0.0%</span></div>
<div class="cb-bar-row"><span>1000 ms</span><span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-orange);"></span></span><span class="cb-value">0.0%</span></div>
<figcaption>Read anywhere inside the 250ms apply delay and every read is stale — 100% at 0, 50, 100, 200ms. At the 250ms boundary it collapses to 4.7%, and by 300ms it's zero. The cliff sits exactly where I told the replica to hold. Measured on PostgreSQL 16.14, 300 trials per delay, results in benchmarks/postgres-read-your-writes/results/.</figcaption>
</figure>

The shape is the whole point. Below the apply delay every single read is stale — not most, all 300 of them. Then it falls off a cliff right at 250ms: 4.7% at the boundary, zero past it. The lag isn't a fuzzy risk that slowly fades, it's a wall, and the wall is wherever the replica's real lag happens to be that second.

Which is exactly why "just wait 500ms before you read" is a bad fix dressed as a good one. You picked 500 because the lag was 250 today. The lag isn't 250 tomorrow — it's 250 until a replica gets busy, or the network hiccups, or somebody runs a big write, and then your 500ms guess is stale too and the ticket comes back. You're guessing at a number the database already knows exactly.

## The gate

The database knows exactly because every write has an address in the WAL — a log sequence number, an LSN. When a write commits on the primary you can ask `pg_current_wal_lsn()` and get the position that write sits at. And on the replica you can ask `pg_last_wal_replay_lsn()` and get how far it's replayed. Compare the two and you're not guessing anymore: you know whether this replica has caught up past the write you care about.

So instead of a stopwatch, you gate on the LSN. After a user writes, you stash the LSN their write committed at. Before that user reads, you check whether the replica has replayed past it. If it has, read the replica — the row is there. If it hasn't, this one read goes to the primary. Everybody else keeps hitting the replicas.

The write side captures the LSN:

```python
exe(prim, "UPDATE user_counter SET val = %s WHERE user_id = %s", (val, uid))
truth[uid] = val
if gated:
    last_lsn[uid] = q1(prim, "SELECT pg_current_wal_lsn()")
```

The read side asks the replica one yes/no question before trusting it:

```python
use_replica = True
if gated and uid in last_lsn:
    caught = q1(repl, "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (last_lsn[uid],))
    use_replica = bool(caught)

if use_replica:
    served = q1(repl, "SELECT val FROM user_counter WHERE user_id = %s", (uid,))
else:
    served = q1(prim, "SELECT val FROM user_counter WHERE user_id = %s", (uid,))
```

To see what that buys you I ran a mixed workload — 300 users, a seeded stream of writes and reads, some users reading their own row right after they wrote it, which is the worst case. Then I ran the exact same op stream through two routers: naive, where every read goes to the replica, and gated, where reads check the LSN first.

<figure class="cache-bench">
<h3>Same 3,788 reads, two routers</h3>
<div class="cb-panels">
<div>
<p class="cb-panel-title">Naive — always the replica</p>
<div class="cb-bar-row"><span>Stale reads</span><span class="cb-track"><span class="cb-fill" style="--value: 37.6%; --bar: var(--cb-orange);"></span></span><span class="cb-value">37.6%</span></div>
<div class="cb-bar-row"><span>Off the replica</span><span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-blue);"></span></span><span class="cb-value">100.0%</span></div>
<div class="cb-bar-row"><span>Fell to primary</span><span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-purple);"></span></span><span class="cb-value">0.0%</span></div>
</div>
<div>
<p class="cb-panel-title">Gated — LSN check first</p>
<div class="cb-bar-row"><span>Stale reads</span><span class="cb-track"><span class="cb-fill" style="--value: 0%; --bar: var(--cb-orange);"></span></span><span class="cb-value">0.0%</span></div>
<div class="cb-bar-row"><span>Off the replica</span><span class="cb-track"><span class="cb-fill" style="--value: 62.4%; --bar: var(--cb-blue);"></span></span><span class="cb-value">62.4%</span></div>
<div class="cb-bar-row"><span>Fell to primary</span><span class="cb-track"><span class="cb-fill" style="--value: 37.6%; --bar: var(--cb-purple);"></span></span><span class="cb-value">37.6%</span></div>
</div>
</div>
<figcaption>Naive routing served 1,425 stale reads out of 3,788 — 37.6% wrong — while pinning 100% of traffic on the replica. The LSN gate cut stale reads to exactly zero and still kept 62.4% of reads on the replica, sending only the 37.6% that were reading their own just-written row to the primary. Measured on PostgreSQL 16.14, seed 1234, results in benchmarks/postgres-read-your-writes/results/.</figcaption>
</figure>

Naive routing got 37.6% of its reads wrong — 1,425 of 3,788 came back stale — and it happily served all of them off the replica, fast and confidently incorrect. The gate got zero wrong. Not "fewer," zero, because the check is exact: a read either sees a replica that has provably replayed past the write, or it doesn't and goes to the primary.

And here's the part that matters for whether you keep your replicas: the gate still served 62.4% of reads off the replica. It only fell back to the primary for the 37.6% that were reading their own write — precisely the reads that couldn't be trusted anywhere else. It didn't retreat everyone to the primary. It sent exactly the reads that needed the primary to the primary, and left the rest on the replicas, which is the whole reason you built them.

## The takeaway

Read replicas don't lie to you. They just haven't heard yet, and the moment you read from one right after a write is the moment they're most likely to be behind. The clean fix isn't to guess how far behind with a timer and it isn't to give up and send everything to the primary. It's to capture the write's LSN, and gate the read on whether the replica has actually replayed past it.

The cost is real but small. You have to stash a per-user last-write LSN somewhere fast — I used a dict here; in production that's a Redis key per user, which is exactly where these systems put it. And each gated read pays one extra round trip to ask the replica "are you caught up," or it skips that and goes straight to the primary. In exchange you get read-your-writes with zero stale reads and you keep most of your reads on the replicas.

The naive router was wrong 37.6% of the time and never once knew it. The gate was wrong zero times, because it stopped guessing at the lag and started asking the WAL. The replica already knows whether it's caught up — ask it.

The harness — primary, replica, both experiments, the raw CSVs — is at [github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-read-your-writes](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-read-your-writes). These are laptop numbers from two containers on one machine, not capacity planning — your lag, your fallback rate, and your stale window depend on your replicas, your network, and your write pattern. Run it against numbers that look like yours before you trust any of them.
