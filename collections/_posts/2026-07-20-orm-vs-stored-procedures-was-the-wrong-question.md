---
layout: post
title: ORM vs Stored Procedures Was the Wrong Question
date: 2026-07-20
description: Everyone benchmarks ORM against stored procedures and reports a winner. I tried to run that fight fairly on Postgres and it kept dissolving into four different fights. When the generated SQL is identical the two are within 125 microseconds of each other. The real gaps have names, N+1, object hydration, parameterization, and none of them are "ORM vs stored procedure."
categories: [postgres, performance, databases]
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

Every few months the same thread comes back around: someone benchmarks their ORM against a hand-written stored procedure, the stored procedure wins, and the comment section splits into the people who always knew ORMs were slow and the people who say you held it wrong. I've been on both sides of that thread. So I finally sat down to settle it with numbers, held the database constant, held the schema constant, held the data constant, and ran the same query three ways: through SQLAlchemy's ORM, through raw parameterized SQL, and through a PL/pgSQL function standing in for the stored procedure.

The benchmark did not settle it. It kept refusing to. Every time I thought I had one clean comparison, it turned out I was measuring two things at once, and when I pulled them apart the ORM-vs-stored-procedure part of the gap was almost always the small part. The big numbers people quote are real, they're just not measuring what the title says.

## The problem

"ORM vs stored procedure" sounds like one axis, fast on one end and slow on the other. It isn't. It's at least four things wearing one label: what SQL actually gets sent, how many round trips it takes to send it, how much work the client does turning rows back into objects, and whether the query is parameterized. A stored procedure quietly bundles good answers to all four. An ORM lets you pick a bad answer to any of them, and then the benchmark blames the ORM. If you want to know what an ORM actually costs, you have to hold the other three still, one at a time, and most benchmarks don't.

Everything below ran against Postgres 16.14 in Docker, 5,000 customers and 49,942 orders, SQLAlchemy 2.0.51 on psycopg 3.3.4. Every number is a p50 over 1,000 calls, averaged across three full end-to-end runs. These are laptop numbers, they tell you the shape of the difference, not your production capacity.

## When the SQL is the same, so is the speed

Start with the fairest fight I could build. One parameterized lookup, a customer joined to one of their orders, returning the identical rows three ways. The ORM query, a raw psycopg query with the same SQL, and a call to a PL/pgSQL function whose body is that same SQL. Same plan, same execution, same bytes on the wire. The only thing that varies is the layer sitting between me and the socket.

<figure class="cache-bench">
  <h3>Identical single-row query, three ways (p50, microseconds)</h3>
  <div class="cb-bar-row"><span>raw psycopg</span><span class="cb-track"><span class="cb-fill" style="--value:60.0%;--bar:var(--cb-green)"></span></span><span class="cb-value">189 µs</span></div>
  <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:60.8%;--bar:var(--cb-blue)"></span></span><span class="cb-value">192 µs</span></div>
  <div class="cb-bar-row"><span>ORM</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-purple)"></span></span><span class="cb-value">315 µs</span></div>
  <figcaption>189 µs raw, 192 µs stored proc, 315 µs ORM. The stored procedure is within 3 µs of raw psycopg. The ORM adds a flat ~125 µs of its own machinery over the same SQL, and that's the whole story here, it's a constant, not a multiplier. Measured on Postgres 16.14, results in benchmarks/orm-vs-stored-procedures/results/.</figcaption>
</figure>

That's the number the whole post hangs on. When you actually hold the SQL identical, the stored procedure and the raw query are the same thing, 189 versus 192 microseconds, and the ORM is slower by a fixed 125 microseconds it spends building a query object, mapping the result, and bookkeeping. On a single row that's a 1.6x ratio and it looks bad in a bar chart. But it's 125 microseconds. It does not grow with your data, it does not grow with your load, it's the toll you pay once per query for not writing the SQL yourself. If your handler does anything else at all, a template render, a second query, a network hop to some other service, that 125 microseconds disappears into the noise.

So the honest version of the fairest fight is: there is barely a fight. Which means every dramatic benchmark you've seen was measuring one of the next three things instead.

## The N+1 blowup that gets blamed on the ORM

Here's the one everybody's actually seen. Load 100 customers, then load each customer's orders. The naive way, the way an ORM invites you into if you touch a lazy relationship in a loop, is one query for the customers and then one more query per customer. 101 round trips. The stored procedure does it in one.

<figure class="cache-bench">
  <h3>Load 100 customers and their orders (p50, microseconds)</h3>
  <div class="cb-bar-row"><span>ORM lazy (1+M)</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">35,095 µs</span></div>
  <div class="cb-bar-row"><span>raw naive (1+M)</span><span class="cb-track"><span class="cb-fill" style="--value:55.2%;--bar:var(--cb-orange)"></span></span><span class="cb-value">19,379 µs</span></div>
  <div class="cb-bar-row"><span>ORM eager join</span><span class="cb-track"><span class="cb-fill" style="--value:17.1%;--bar:var(--cb-purple)"></span></span><span class="cb-value">6,012 µs</span></div>
  <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:2.1%;--bar:var(--cb-blue)"></span></span><span class="cb-value">746 µs</span></div>
  <div class="cb-bar-row"><span>raw eager join</span><span class="cb-track"><span class="cb-fill" style="--value:2.0%;--bar:var(--cb-green)"></span></span><span class="cb-value">704 µs</span></div>
  <figcaption>The top two bars each take 101 round trips, the bottom three each take 1. Naive ORM lazy-loading (35 ms) against a single-call stored procedure (746 µs) is the ~47x gap the internet loves. But raw SQL written the same naive way is also 19 ms, and the ORM told to do a join is 6 ms. The axis that matters is round trips, not ORM. Measured on Postgres 16.14, results in benchmarks/orm-vs-stored-procedures/results/.</figcaption>
</figure>

There's your viral benchmark. 35 milliseconds for the ORM, 746 microseconds for the stored procedure, call it 47x and post it. But look at the other three bars before you do. Raw psycopg written the same naive way, a loop firing one query per customer, is 19 milliseconds, half the ORM but still catastrophic, and it's not using an ORM at all. The thing killing both of them is 101 round trips, not the object mapper. And the moment you tell the ORM to do the join it was always capable of, it drops to 6 milliseconds, one round trip, in the same league as the hand-written query.

The ORM's real sin here isn't slowness, it's that it will happily let you write the slow version and it looks like ordinary code. `for customer in customers: customer.orders` is one line and it's 101 queries. Nothing warns you. That's a genuine, fair criticism of ORMs, they make the expensive thing look cheap. But it's a criticism of the abstraction hiding the round trips, not of the ORM being slow at what it does. Written correctly, the ORM is 6 ms, and the gap to the stored procedure is round trips you chose to make, not a tax the ORM charged you.

## The part that's actually the ORM: turning rows into objects

Now the fight the ORM genuinely loses, and it's worth being honest about it because it's the one people usually skip. Fetch 3,000 rows and hand them back. Raw psycopg gives you tuples. The stored procedure gives you tuples. The ORM gives you 3,000 fully hydrated, change-tracked, identity-mapped objects, and that costs real money.

<figure class="cache-bench">
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Latency, 3,000 rows (p50 µs)</p>
      <div class="cb-bar-row"><span>raw tuples</span><span class="cb-track"><span class="cb-fill" style="--value:27.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">2,630 µs</span></div>
      <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:27.0%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2,608 µs</span></div>
      <div class="cb-bar-row"><span>ORM hydrate</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-purple)"></span></span><span class="cb-value">9,669 µs</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Peak memory (KiB)</p>
      <div class="cb-bar-row"><span>raw tuples</span><span class="cb-track"><span class="cb-fill" style="--value:19.0%;--bar:var(--cb-green)"></span></span><span class="cb-value">843 KiB</span></div>
      <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:18.9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">842 KiB</span></div>
      <div class="cb-bar-row"><span>ORM hydrate</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-purple)"></span></span><span class="cb-value">4,444 KiB</span></div>
    </div>
  </div>
  <figcaption>Same SQL, same 3,000 rows. Raw tuples and stored-proc tuples are identical, 2.6 ms and ~843 KiB. Full ORM hydration is 9.7 ms and 4.4 MiB, about 3.7x the time and 5x the memory, all of it spent client-side building objects the tuples already contained. Measured on Postgres 16.14, results in benchmarks/orm-vs-stored-procedures/results/.</figcaption>
</figure>

This one's fair and the ORM owns it. 2.6 milliseconds to pull tuples, 9.7 milliseconds to turn those same tuples into mapped objects, and five times the memory to hold them. The stored procedure isn't winning here because it's a stored procedure, it's winning because it hands back tuples and stops. Point raw psycopg at the same rows and it ties the stored procedure exactly, 2,630 versus 2,608 microseconds. The delta is entirely the object graph, the identity map, the change tracking that lets you mutate an object and have the ORM figure out the UPDATE. You're paying for a feature. If you're reading 3,000 rows to serialize them straight to JSON, you're paying for a feature you're about to throw away, and SQLAlchemy will let you skip it, that's what the Core API and `.execution_options` are for. But by default, hydration is the one place the ORM is genuinely, measurably heavier, and no amount of holding it right makes it free.

## The plan cache advantage that Postgres already gave everyone

The last one is the argument I was most sure I'd confirm, and it's the one that surprised me. The folklore says stored procedures win because their plans get cached, while ORMs generate ad-hoc SQL that reparses and replans every single time and thrashes the plan cache. So I ran the same logical query 500 times three ways: ad-hoc SQL with the literal values concatenated into the string, a parameterized version, and the stored procedure, and I watched `pg_stat_statements` to count how many distinct statements each one produced.

<figure class="cache-bench">
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Distinct pg_stat_statements entries after 500 calls</p>
      <div class="cb-bar-row"><span>ad-hoc concat</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1</span></div>
      <div class="cb-bar-row"><span>parameterized</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">1</span></div>
      <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Parse + plan time per call (ms)</p>
      <div class="cb-bar-row"><span>ad-hoc concat</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0.011 ms</span></div>
      <div class="cb-bar-row"><span>parameterized</span><span class="cb-track"><span class="cb-fill" style="--value:3.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.0004 ms</span></div>
      <div class="cb-bar-row"><span>stored proc</span><span class="cb-track"><span class="cb-fill" style="--value:1.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.0002 ms</span></div>
    </div>
  </div>
  <figcaption>The thing folklore says differs (statement-cache entries) is identical, all three collapse to a single entry, because modern pg_stat_statements normalizes literals. The thing that actually differs (parse+plan work) is real, ad-hoc replans every call, but at 0.011 ms it's about 3 microseconds of the query's total. End to end the three finished at 249, 214, and 210 µs. Measured on Postgres 16.14, results in benchmarks/orm-vs-stored-procedures/results/.</figcaption>
</figure>

The cache-thrashing story is just false on a modern Postgres. Ad-hoc concatenation with 500 different literal values produced one entry in `pg_stat_statements`, not 500, because Postgres normalizes literals out of the statement fingerprint before it records them. The plan cache doesn't explode. What is true, and I don't want to wave it away, is that ad-hoc SQL does pay to parse and plan every call, and parameterized queries and stored procedures don't, that's the 0.011 ms versus 0.0004 ms in the right panel, a real 20-to-30x ratio. But look at the absolute number. It's eleven microseconds, on a query that takes 210. End to end all three finished within 40 microseconds of each other. The parameterization advantage is real and it is almost never the thing you can feel.

And here's the part that matters for the ORM argument: this axis isn't about ORMs at all. ORMs parameterize. That's the default, it's how they defend against SQL injection. The slow bar in that chart, the ad-hoc string concatenation, is the thing you get when you skip the ORM and hand-build SQL with an f-string. The stored procedure's plan-caching edge is really just parameterization, and the ORM already gives you parameterization for free.

## The takeaway

I set out to benchmark ORM against stored procedures and I couldn't, because it isn't one comparison. Once you hold the database still and pull the confounds apart, here's what's actually left:

- **Same SQL, same speed.** A parameterized ORM query and a stored procedure running identical SQL are 125 microseconds apart, a fixed client-side constant, not a multiplier. On anything but a tight loop it's invisible.
- **N+1 is round trips, not ORMs.** The 47x blowup is 101 round trips against 1, and raw hand-written SQL does it just as badly. The fair complaint is that ORMs make the round trips easy to not see. Written as a join, the ORM is back in the pack.
- **Hydration is the one real ORM tax.** Turning 3,000 rows into tracked objects costs ~3.7x the time and 5x the memory over raw tuples, and it's the only place the ORM is genuinely heavier. Skip it when you don't need the objects.
- **The plan-cache argument is mostly folklore.** Postgres normalizes literals, so ad-hoc SQL doesn't thrash `pg_stat_statements`. It pays real parse-and-plan cost, about 3 microseconds a call, and the ORM sidesteps it anyway by parameterizing.

So if you're reaching for a stored procedure because you read that ORMs are slow, you're solving the wrong problem three times out of four. Fix your round trips, skip hydration when you're just serializing, and let the ORM parameterize the way it already wants to. The one time a stored procedure genuinely wins on speed is when you're moving a lot of rows through heavy set-based logic and you want to do it inside the database without hydrating anything, and even then, raw SQL from your app gets you the same win. The one thing to remember: before you benchmark two things, make sure you're only comparing two things. I wasn't, and neither is most of the internet.

The harness, all four experiments and the raw CSVs, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/orm-vs-stored-procedures). Numbers are from my laptop, so read them as ratios and shapes, not as your capacity.
