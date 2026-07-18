---
layout:     post
title:      The Expired Keys Your Redis Replica Still Counts
date:       2026-07-18
description:    A replica doesn't expire keys on its own clock, it waits for the primary to tell it. I went to reproduce the classic stale-lock-on-a-replica bug and found modern Redis had quietly fixed half of it, and left the more dangerous half in plain sight.
categories: redis replication expiry operations
---

If you run Redis with a read replica and you've ever trusted a key count coming off that replica, this one's worth a couple of minutes. I went in chasing a specific bit of folklore, that a replica will happily hand you back a lock that expired twenty seconds ago, and the reproduction talked me out of half of what I believed and left the other half quieter and more dangerous than I expected.

## The problem

A read replica will report keys that expired a while ago. Expiring a key is the primary's job: it removes the key and tells the replica, and until that message arrives the replica physically still holds the dead key. So any decision made from a replica's key count, a `DBSIZE` gauge or a running-jobs dashboard, is counting ghosts, and on an older Redis even a plain `GET` off the replica handed back the stale value. This is exactly what a replica does with expired keys, measured across versions.

The setup is the usual idempotency guard. You take a lock per job with `SET job:{id} owner NX EX 30` so two workers can't run the same job, and because that lock lives in Redis you also point a read replica at it to power a "currently running jobs" dashboard and a cheap pre-check, the kind of thing where you glance at the replica before bothering the primary. The pre-check and the dashboard are both reading key state off a node that, it turns out, has opinions about expiry that differ from the primary's.

## Replicas don't expire keys, they wait to be told

Here's the piece that explains everything else. In Redis, expiring a key is the primary's job, not the replica's. The primary decides a key is dead one of two ways: its background active-expire cycle samples keys and notices this one's TTL has passed, or a client touches the key and the primary lazily removes it on access. Either way, the moment the primary removes it, it writes an explicit `DEL` into the replication stream. The replica never looks at a key's TTL and decides on its own to drop it. It holds the key until that `DEL` arrives.

So at any given moment a replica can be physically sitting on keys that are, by the clock, long gone. It isn't broken and nothing is leaking. It's doing exactly what it's built to do, which is wait for instructions.

I wanted to see what that actually looks like from the outside, so I ran a pinned Redis 7.4 primary with a read replica, set a thousand keys with a two-second TTL, waited until every one of them had expired, and then just asked the replica what it thought it was holding. To keep the timing honest I turned the primary's active-expire cycle off for this part, so its background sweeper wasn't racing my measurement.

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
  <h3>The replica's view of 1,000 expired keys</h3>
  <div class="cb-bar-row"><span>DBSIZE reports</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1000</span></div>
  <div class="cb-bar-row"><span>keys that GET returns</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0</span></div>
  <figcaption>Every one of the 1,000 keys had expired. The replica's DBSIZE still counts all 1,000, but GET returns a value for none of them. The count is looking at physical keys; the read is checking the clock. Measured on Redis 7.4.9, results in benchmarks/redis-replica-expiry/results/.</figcaption>
</figure>

The replica's `DBSIZE` came back `1000`. Every single one of those thousand keys had expired, and reading any of them returned nil. The count and the reality had completely parted ways.

## What each command actually does

The interesting part is which observations tell you the truth and which don't. I took one expired key and asked the replica about it every way I could think of. This is straight from the captured run:

```
DBSIZE (counts it?)                         1
GET job:solo                             None
EXISTS job:solo                             0
TTL job:solo                               -2
SCAN finds job:solo?                    False
DBSIZE again (reads deleted it?)            1
```

`GET` says nil, `EXISTS` says no, `TTL` says `-2` (gone), `SCAN` won't even list it. Every read-shaped command masks the expired key and reports it as absent, which is what you want. Only `DBSIZE` still counts it. And notice the last line: after all those reads, `DBSIZE` is still `1`. Reading the key on the replica didn't clean it up, because a replica doesn't delete on access. The same `GET` on the primary does:

```
DBSIZE 1 -> GET returns None (lazy-deletes) -> DBSIZE 0
```

On the primary, touching the expired key lazily removes it and drops the count to zero. On the replica, the key just sits there being counted no matter how many times you look at it.

## The version twist I didn't expect

I walked in expecting to catch the replica red-handed returning a live-looking lock to a `GET`, the version of this bug that gets quoted around. On Redis 7.4 that doesn't happen. Since Redis 3.2 the replica masks expired keys on reads, and by now that masking covers `GET`, `EXISTS`, `TTL`, and `SCAN`. So your dedup pre-check reading the lock off the replica gets nil, exactly like it should, and the scary version of this bug is already handled for you.

What the masking does not cover is the count. `DBSIZE` still reports the ghost, because it's counting physical keys in the keyspace, not asking each one whether it's still alive. So the danger moved. It used to be "the replica lies to your `GET`"; on a modern Redis it's "the replica's count includes the dead," and anything that reasons about keyspace size off a replica, a `DBSIZE` gauge, a running-jobs number, a cleanup that counts before it acts, is quietly counting expired keys as live ones.

## Proving the "used to be"

I didn't want to hand-wave the "used to be," so I pinned an actual pre-masking Redis, 3.0.7, and ran the exact same script against it. Same primary, same replica, same expired key. Here's the replica on 3.0:

```
DBSIZE (counts it?)                         1
GET job:solo                            owner
EXISTS job:solo                             1
TTL job:solo                                0
SCAN finds job:solo?                    False
DBSIZE again (reads deleted it?)            1
```

There it is, the bug the folklore is actually about. `GET` on the replica hands back `owner`, the value of a lock that expired seconds ago, and `EXISTS` says yes. A dedup guard doing its cheap pre-check against this replica would see a live lock and skip a job that nothing is actually holding. Run the thousand-key version and all 1,000 come back with their stored value, where 7.4 masked every single one. (`SCAN` is its own inconsistent story across versions and I'm not going to untangle it here; the two reads a lock check actually uses, `GET` and `EXISTS`, both hand you the ghost on 3.0.)

So the read path really is fixed on a modern Redis in the way that matters most: the replica will not lie to your `GET` anymore. What it still does is count the dead, which is why the `DBSIZE` half of this outlived the fix and the `GET` half didn't.

## About that "replica DBSIZE runs higher" claim

The other thing people say is that a replica's `DBSIZE` sits consistently above the primary's. I went looking for that gap on purpose, with the primary's active-expire cycle on and 5,000 keys expiring at once, sampling both counts every 200 milliseconds. The peak replica-minus-primary gap was zero. The keys expire, the primary's active cycle sweeps them, the `DEL`s land on the replica right behind, and the two counts fall together.

That drift is real when replication is lagging or the primary's expire cycle is behind, but it's a symptom of lag, not a standing property of replicas. On a healthy, low-lag replica you won't see a permanent gap. The ghosts absolutely exist in the window between a key expiring and the primary getting around to killing it, that window is just short when everything's healthy, which is exactly when you'll forget it's there.

## Stuff worth remembering

- A replica never expires a key on its own. It holds the key until the primary sends a `DEL`, so a replica can physically contain keys that expired a while ago.
- On a modern Redis (this run was 7.4.9), the replica masks expired keys on `GET`, `EXISTS`, `TTL`, and `SCAN`, so those all correctly say "gone." `DBSIZE` does not, it counts the ghosts. On a pre-3.2 Redis (I checked against 3.0.7) even `GET` and `EXISTS` handed back the ghost, which is the older, scarier version of this bug and the reason the folklore exists.
- Don't make a TTL-sensitive correctness call off a replica's key presence or its counts. A replica pre-check is a fine optimization, but the source of truth for a lock or an idempotency guard has to be the primary.
- If you need a "how many are running" number off a replica, store an explicit `expires_at` in the value and filter by it yourself, or treat the count as an upper bound that includes ghosts.
- The whole thing is reproducible in a couple of minutes, [primary, replica, and the script are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-replica-expiry). The default compose runs 7.4 (watch the replica count a thousand keys that every read swears are gone); a second `docker-compose.legacy.yml` runs 3.0 against the same script so you can watch the old unmasked-read version too.

## The takeaway

A replica holds expired keys until the primary tells it to drop them, so its key counts include the dead even when modern reads correctly say they're gone. The rule that falls out of that: don't make a TTL-sensitive decision, a lock check, an idempotency guard, a count, from a replica. Read those off the primary, or store an explicit `expires_at` in the value and judge it against your own clock. Keep the replica for the load you can afford to be a little wrong about.
