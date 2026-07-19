---
layout:     post
title:      The Query That Paid For Everyone Else's Changes
date:       2026-07-20
description:    A one-row indexed lookup query against a Hibernate session holding 8,000 managed entities ran at 1,371 checks/sec. The same query with FlushMode.COMMIT instead of the default AUTO ran at 878,355 checks/sec, 640.7x faster, for identical SQL.
categories: java hibernate performance flame-graphs
---

I first ran into this shape of bug secondhand, in someone else's writeup about `existsById()` costing 95% CPU in a service that looked, on paper, like it was running a cheap indexed lookup. I remember thinking that number had to be exaggerated. It isn't. I built a much smaller version of the same trap this week, a Hibernate session holding 8,000 managed entities and one tiny query run against it, and watched the identical SQL statement run 640 times slower depending on a single flush-mode setting I hadn't touched.

## The problem

Before Hibernate runs any query, it has to make sure the database reflects whatever's pending in memory, otherwise your query could read stale data. With the default `FlushMode.AUTO`, that means a flush before every single query, and a flush means dirty-checking every managed entity currently in the session, comparing each one's current field values against the snapshot Hibernate took when it was loaded. If your session is holding a handful of entities, that's free. If it's holding thousands, because you're in a long batch job, or a request handler that touched a lot of rows and never cleared the session, every query you run pays for a full scan of everyone else's potential changes before it's allowed to execute at all. A one-row `SELECT` becomes O(N) work, and N is however many entities happen to be sitting in memory at the time, not anything related to the query itself.

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
.cb-bar-row { display: grid; grid-template-columns: minmax(7rem, 1.3fr) minmax(6rem, 4fr) minmax(4.2rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
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
@media (max-width: 620px) {
  .cb-panels { grid-template-columns: 1fr; }
}
</style>

## What I actually built

Plain Hibernate against an in-memory H2 database, no Spring involved, so there'd be no framework magic to argue about. I persisted 8,000 entities into a single still-open session, keeping them all managed, and then ran a tiny indexed lookup against random ids for 35 seconds:

```java
Long r = session.createQuery(
                "select w.id from WidgetEntity w where w.id = :id", Long.class)
        .setParameter("id", id)
        .uniqueResultOptional()
        .orElse(null);
```

`hibernate-bad` runs that with Hibernate's default `FlushMode.AUTO` left alone. `hibernate-fixed` sets one line before the loop starts:

```java
session.setHibernateFlushMode(FlushMode.COMMIT);
```

That's the entire diff. Same query, same session, same 8,000 entities, same random ids.

## The number that didn't move (again)

Same story as the regex bug: CPU% is nearly worthless here.

<figure class="cache-bench">
  <h3>CPU load, bad vs fixed (single-threaded, 10-core host)</h3>
  <div class="cb-bar-row"><span>bad</span><span class="cb-track"><span class="cb-fill" style="--value:10.46%;--bar:var(--cb-orange)"></span></span><span class="cb-value">10.5%</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:10.5%;--bar:var(--cb-green)"></span></span><span class="cb-value">10.5%</span></div>
  <figcaption>10.46% avg (max 15.94%) vs 10.5% avg (max 24.14%). Same one busy core either way. Measured on OpenJDK 25.0.1, results in benchmarks/java-high-cpu-debugging/results/.</figcaption>
</figure>

This is the second time in this lab that a flat CPU graph hid a real problem, and it's not a coincidence: any single-threaded hot loop is going to peg its one core whether it's doing useful work or wasted work, and `top` can't see the difference. What can see the difference is throughput, and here it isn't subtle:

<figure class="cache-bench">
  <h3>Checks per second, bad vs fixed</h3>
  <div class="cb-bar-row"><span>bad</span><span class="cb-track"><span class="cb-fill" style="--value:0.16%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1,371</span></div>
  <div class="cb-bar-row"><span>fixed</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">878,355</span></div>
  <figcaption>1,371 checks/sec vs 878,355 checks/sec, for the identical query and the identical CPU budget. Fixed does 640.7x the throughput of bad. The bad bar is not a rendering error, it's genuinely that small relative to fixed on a linear scale.</figcaption>
</figure>

640.7x, for one line of code, against a session holding 8,000 entities. Scale the entity count up, or run this in a request handler that accumulates a bigger persistence context over its lifetime, and the multiplier only gets worse, because the dirty-check cost is O(N) and N is whatever's currently managed, not anything the query itself controls.

## What the flame graph actually shows

The bad flame graph doesn't have one dominant frame the way the regex bug did. It's spread across the flush machinery itself:

<figure class="cache-bench">
  <h3>Where the "bad" run's CPU samples land</h3>
  <div class="cb-bar-row"><span>Field.get</span><span class="cb-track"><span class="cb-fill" style="--value:16%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~16%</span></div>
  <div class="cb-bar-row"><span>Long.equals</span><span class="cb-track"><span class="cb-fill" style="--value:9.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">~9.5%</span></div>
  <div class="cb-bar-row"><span>getPropertyValues</span><span class="cb-track"><span class="cb-fill" style="--value:6%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~6%</span></div>
  <div class="cb-bar-row"><span>prepareEntityFlushes</span><span class="cb-track"><span class="cb-fill" style="--value:5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~5%</span></div>
  <div class="cb-bar-row"><span>Cascade.cascade</span><span class="cb-track"><span class="cb-fill" style="--value:4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~4%</span></div>
  <div class="cb-bar-row"><span>performDirtyCheck</span><span class="cb-track"><span class="cb-fill" style="--value:4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~4%</span></div>
  <div class="cb-bar-row"><span>findDirty</span><span class="cb-track"><span class="cb-fill" style="--value:3%;--bar:var(--cb-blue)"></span></span><span class="cb-value">~3%</span></div>
  <div class="cb-bar-row"><span>everything else</span><span class="cb-track"><span class="cb-fill" style="--value:52.5%;--bar:var(--cb-grid)"></span></span><span class="cb-value">~52.5%</span></div>
  <figcaption>Field.get and Long.equals (reflection reading each entity's fields, then comparing against the loaded snapshot) sit above the named Hibernate flush internals: prepareEntityFlushes, Cascade.cascade, performDirtyCheck, findDirty. The rest is scattered across more of the same machinery, not the query itself.</figcaption>
</figure>

`Field.get` is reflection pulling each managed entity's current field values so they can be compared against what Hibernate loaded them with. `Long.equals` is that comparison. `AbstractFlushingEventListener.prepareEntityFlushes`, `Cascade.cascade`, `DefaultFlushEntityEventListener.performDirtyCheck`, and `DirtyHelper.findDirty` are the flush event pipeline itself, walking the persistence context entity by entity. None of that is the `SELECT`. The actual query, the thing the code visibly asks for, doesn't even register as a named hot frame, it's buried in whatever's left in "everything else." The bug isn't in the query. It's in everything that has to happen before Hibernate will let the query run.

![Flame graph of the hibernate-bad run, wide bands of org/hibernate/event/internal flush and dirty-check frames, Cascade.cascade, Field.get, and Long.equals, above the actual query buried at the edges](/images/posts/java-high-cpu-debugging/flame-hibernate-bad.jpg)

`AbstractFlushingEventListener.flushEverythingToExecutions`, `Cascade.cascade`, `DefaultFlushEntityEventListener.onFlushEntity`, `EntityPersister.getPropertyValues`, all of it Hibernate's own flush pipeline, fills most of the width above `autoPreFlush` and `autoFlushIfRequired`. The query that actually runs is the narrow strip on the far right. Here's the same lookup with `FlushMode.COMMIT` set:

![Flame graph of the hibernate-fixed run, dominated by real H2 query execution frames like IndexCursor.find, TableFilter.next, and MVPrimaryIndex, with no Hibernate flush machinery at all](/images/posts/java-high-cpu-debugging/flame-hibernate-fixed.jpg)

No flush frames anywhere. What's left is what the query actually costs: H2's own `IndexCursor.find`, `TableFilter.next`, and the `MVStore`/`MVPrimaryIndex` b-tree lookup underneath it, plus the JDBC and Hibernate SQL-execution plumbing that has to run for any query regardless of flush mode. This is what a one-row indexed SELECT is supposed to look like on a flame graph.

## Stuff worth remembering

- `FlushMode.AUTO` means every query pays for a full dirty-check of the whole persistence context first, and that cost scales with however many entities happen to be managed, not with the query.
- `session.setHibernateFlushMode(FlushMode.COMMIT)` (or `MANUAL`, if you're comfortable flushing yourself) skips that pre-query flush entirely. One line, 640.7x throughput, same SQL.
- Watch for this in long-lived sessions specifically: a batch job or a request handler that keeps accumulating managed entities without ever clearing or evicting them is exactly the shape that turns a cheap query slow.
- CPU% flattened out identically in this bug and the regex bug in this same lab. Two bugs, two flat CPU graphs, two real problems that only showed up in throughput and the flame graph. That's not a coincidence, it's what single-threaded CPU-bound code always looks like from the outside.
- These are laptop numbers demonstrating the mechanism, [the lab and its flame graphs are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/java-high-cpu-debugging), alongside the regex and busy-spin bugs from the same switchable project.

## The takeaway

`existsById()`-style checks look free because the SQL behind them is free. What isn't free is everything Hibernate does before it's willing to run that SQL, and with the default flush mode, that's a full dirty-check of your entire persistence context, every single call. The fix costs one line and changes no business logic at all, `FlushMode.COMMIT` instead of the default `AUTO`, but you won't find it by staring at CPU% or even at the query itself. You find it by asking a profiler what's actually running underneath the call you can see, and in this case what's running underneath is thousands of field comparisons for changes that were never there.
