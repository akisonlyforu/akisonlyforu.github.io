---
layout:     post
title:      The Commits That Didn't Survive the Failover
date:       2025-06-14
description:    A MySQL primary told a thousand clients their writes were committed, then crashed. The replica came up in seconds, the dashboard went green, and every one of those thousand rows was gone. Async replication will do that quietly; I went to measure exactly how much semi-sync buys you back, and what it costs.
categories: mysql replication high-availability failover
---

The first failover I ever watched go "cleanly" was the scariest one. The primary died, an orchestrator promoted the replica in about four seconds, the app reconnected, the dashboard went back to green, and everyone on the call exhaled. It was only the next morning, reconciling a couple of downstream systems, that we found a few hundred rows the application was certain it had written and the database had never heard of. The failover worked. The data didn't survive it. Those two things are not the same thing, and the gap between them is exactly what asynchronous replication is.

## The problem

With plain asynchronous replication, MySQL tells your client a transaction is committed before it knows whether any replica has received it. The primary writes the commit to its own binary log, returns `OK`, and a separate thread ships that binlog event to the replica whenever it gets around to it. If the primary crashes hard in the window between the `OK` and the shipping, those events are on a disk you can no longer reach, and the replica you promote has never seen them. The client was told "committed". The new primary has no record. Nobody logged an error, because from every component's point of view nothing went wrong.

The setup that produces this is the most ordinary one there is: a primary, one replica, GTID replication, an app that commits and trusts the `OK`. That's most MySQL deployments. So I built exactly that, pinned, and made the primary lie to a thousand clients on purpose.

## What "committed" means on the async path

It's worth being precise about the order of operations, because the whole bug lives in it. On a commit, the primary writes the transaction to its binary log, commits it to the storage engine, and returns success to the client. The replica has its own IO thread that pulls binlog events across the wire into a local relay log, and a separate SQL thread that applies the relay log to the replica's tables. Two independent lags, both invisible to the client: how far behind the IO thread is on *receiving*, and how far behind the SQL thread is on *applying*.

Async replication makes exactly zero promises about either one at commit time. The `OK` you got back means "the primary wrote it down", nothing more. Whether the replica has it is a separate question with its own timeline, and when the primary process dies, that timeline stops wherever it happened to be.

To make the receipt gap deterministic instead of racing it under load, I stopped the replica's IO thread before the burst, `STOP REPLICA IO_THREAD`, which models a replica whose receipt has fallen behind. Then I wrote a thousand single-row inserts to the primary, every one of them returning success, and hard-killed the primary with `docker kill`, a crash and not a clean shutdown. Then I promoted the replica and counted.

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

<figure class="cache-bench">
  <h3>Rows surviving failover, of 1,000 acknowledged commits</h3>
  <div class="cb-bar-row"><span>async, acknowledged</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1000</span></div>
  <div class="cb-bar-row"><span>async, survived</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
  <div class="cb-bar-row"><span>semi-sync, acknowledged</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">1000</span></div>
  <div class="cb-bar-row"><span>semi-sync, survived</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">1000</span></div>
  <figcaption>Same thousand commits, same hard kill of the primary, two replication modes. Under async, all 1,000 were acknowledged to the client and 0 survived the failover. Under semi-sync, all 1,000 survived. Measured on MySQL 8.0.46, results in benchmarks/mysql-semisync-failover/results/.</figcaption>
</figure>

A thousand acknowledged, zero present. That's the async number and it's not a soft edge, it's the whole thousand. The replica's IO thread had never received the events, the binlog that held them died with the primary, and promoting the replica made a database that was authoritative and wrong. Every client that got an `OK` was told the truth about the old primary and a lie about the cluster.

## Semi-sync waits for the replica to say it got it

Semi-synchronous replication changes one thing, and it's the one thing that matters here: the primary will not tell the client a commit succeeded until at least one replica has acknowledged that it wrote the event to its own relay log. You turn it on by loading the plugins, `rpl_semi_sync_source` on the primary and `rpl_semi_sync_replica` on the replica, and enabling both. The default wait point, `AFTER_SYNC`, means the primary waits for that replica acknowledgement *before* it commits to its own storage engine, so there's no window where a client can see a commit that a crash could still erase.

The subtle part, and the part I wanted to prove to myself rather than take on faith, is *what* semi-sync actually guarantees. It guarantees the replica has **received** the event, into its relay log. It says nothing about whether the replica has **applied** it to the tables yet. Those are the two lags from earlier, and semi-sync only closes the first one. So I set up the mirror image of the async test: semi-sync on and verified (`Rpl_semi_sync_source_status = ON`, one connected client), then `STOP REPLICA SQL_THREAD` on the replica so it kept receiving events but stopped applying them. The IO thread still acknowledges receipt, so commits still go through, but the replica's tables are frozen behind a growing relay log.

Same thousand inserts. Each commit now blocked until the replica acknowledged receipt, so by construction the events were on the replica's disk before any client heard `OK`. Then the same hard `docker kill` of the primary. On promotion I started the SQL thread, let it drain the relay log it had been sitting on, and counted: **1000 acknowledged, 1000 present, 0 lost**. The replica hadn't applied a single one of those rows at the moment the primary died. It didn't matter, because the durable copy was already in the relay log, and draining it after promotion recovered every one. Semi-sync protected the transmit, and the transmit was enough.

## So what does it cost

Waiting for a round trip on every commit is not free, and the honest answer is that it depends entirely on the distance between your primary and your replica. I measured per-commit latency for two thousand single-row transactions in each mode, on a healthy cluster with the replica applying normally.

<figure class="cache-bench">
  <h3>Per-commit latency, async vs semi-sync (2,000 commits each)</h3>
  <div class="cb-bar-row"><span>async p50</span><span class="cb-track"><span class="cb-fill" style="--value:8.1%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1.06 ms</span></div>
  <div class="cb-bar-row"><span>semi-sync p50</span><span class="cb-track"><span class="cb-fill" style="--value:8.4%;--bar:var(--cb-purple)"></span></span><span class="cb-value">1.09 ms</span></div>
  <div class="cb-bar-row"><span>async p99</span><span class="cb-track"><span class="cb-fill" style="--value:15.0%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1.96 ms</span></div>
  <div class="cb-bar-row"><span>semi-sync p99</span><span class="cb-track"><span class="cb-fill" style="--value:13.7%;--bar:var(--cb-purple)"></span></span><span class="cb-value">1.79 ms</span></div>
  <div class="cb-bar-row"><span>async max</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">13.06 ms</span></div>
  <div class="cb-bar-row"><span>semi-sync max</span><span class="cb-track"><span class="cb-fill" style="--value:51.2%;--bar:var(--cb-purple)"></span></span><span class="cb-value">6.69 ms</span></div>
  <figcaption>Bar widths are relative to the largest value, async max at 13.06 ms. On a single laptop the primary and replica share a host, so the acknowledgement round trip is sub-millisecond and the semi-sync cost lands inside the noise, p50 1.09 vs 1.06 ms. On a real network, that round trip is the whole penalty, and the gap widens with the distance. Measured on MySQL 8.0.46, results in benchmarks/mysql-semisync-failover/results/.</figcaption>
</figure>

Read that chart for the shape, not the verdict. Here the two modes are indistinguishable, and if anything semi-sync's tail is tighter, which is just the noise of a shared-host benchmark. The thing the numbers can't show you on a laptop is the one thing that actually costs you in production: semi-sync adds one network round trip to every commit, and if your replica is in another availability zone, that round trip is your added commit latency, on the write path, every single time. The mechanism is real even where my numbers are quiet. Don't take "1.09 vs 1.06" to a capacity meeting.

## The catch that turns synchronous back into async

There's one more thing about semi-sync that I think matters more than the latency, because it's the part that quietly gives back the guarantee you paid for. Semi-sync is not "wait forever for a replica". It's "wait up to `rpl_semi_sync_source_timeout`, then give up and fall back to async". The default is ten seconds. So I turned semi-sync on and then stopped the replica's IO thread entirely, so no replica could acknowledge anything, and timed a single commit.

```
commit stalled : 10008 ms before falling back to async
status after   : Rpl_semi_sync_source_status = OFF
```

The commit hung for the full ten-second timeout, then succeeded, and the primary silently switched itself to asynchronous replication. It stays that way until a replica catches up and reconnects. This is the sharp edge: the moment your only semi-sync replica falls behind or dies, your "synchronous" cluster spends ten seconds stalling every commit and then becomes exactly the async cluster from the top of this post, the one that loses acknowledged writes on a crash, and nothing in the application layer tells you the guarantee is gone. You find out the way I found out the first time, the next morning.

## The takeaway

Async replication's `OK` means "the primary wrote it down", and if the primary dies before the replica catches up, everything it acknowledged in that window is gone. In my test that was a clean thousand out of a thousand. Semi-sync with `AFTER_SYNC` fixes it by refusing to acknowledge a commit until a replica has the event in its relay log, and it held all thousand rows through a hard kill even though the replica hadn't applied one of them yet, because what it guarantees is receipt, not apply, and receipt is what survives a crash.

The cost is a network round trip on every commit, invisible on a single host and very much not invisible across zones, so measure it on your actual topology and not on a laptop. And the caveat that matters most: with a single semi-sync replica, a timeout drops you back to async without a word, so if you actually need the guarantee to hold, run more than one replica and set `rpl_semi_sync_source_wait_for_replica_count` above one, so losing a replica degrades your latency instead of silently degrading your durability. The one thing to remember is that a failover completing and your data surviving it are two different events, and only one of them shows up green on the dashboard.

The harness that produced every number here, both compose files, the failover script, and the raw CSVs, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/mysql-semisync-failover). These are laptop measurements demonstrating the mechanism, not capacity numbers for your cluster.
