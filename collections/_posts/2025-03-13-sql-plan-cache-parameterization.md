---
layout:     post
title:      The Query Plans That Only Ran Once
date:       2026-07-18
description:    Run the same query five hundred times with a different literal each time and SQL Server caches five hundred plans, each used once, eating 66MB. Parameterize it and you get one plan, 136KB. Then parameter sniffing shows up and hands the common value a plan built for a rare one.
categories: sql-server plan-cache parameterization databases
---

Open a SQL Server plan cache expecting a tidy handful of plans, and you can find thousands sitting there instead, each one used exactly once. I wanted to make that happen on purpose, see how bad it gets, and then see what parameterizing the queries actually buys you, because it fixes the bloat and then hands you a different problem.

## The problem

Run the same query over and over with a different literal value baked into the text each time, and SQL Server compiles and caches a separate plan for every one, filling the plan cache with hundreds of plans that each run once and burning CPU recompiling. Parameterizing the query collapses all of that to one shared plan, which is the fix, except that the one shared plan then gets compiled for whatever value showed up first, and on skewed data that plan can be badly wrong for everyone else. This measures both halves.

The setup is the most ordinary thing in the world. You have a query you run constantly, the same shape every time, with a different value plugged in. Look up customer `4123`, then `5561`, then `9080`. Same query, different literal. What SQL Server does with that depends entirely on whether the literal is baked into the text or handed in as a parameter, and the difference is much bigger than it looks.

## What the cache is keyed on

SQL Server compiles a query into a plan and caches it so it doesn't have to compile the same thing twice. The catch is what counts as "the same thing." The cache is keyed on the query text, near enough, so `WHERE customer_id = 4123` and `WHERE customer_id = 5561` are two different texts, which means two different plans, each compiled from scratch and each parked in the cache forever. Do that across a few hundred distinct ids and you get a few hundred plans that will each be used exactly once and then sit there taking up memory.

A parameterized query dodges the whole problem. `WHERE customer_id = @cid` is one text no matter what value `@cid` holds, so it compiles once and every execution reuses the one plan.

I ran it both ways to see the gap. A 2,000,000-row `orders` table joined to a `customers` table, and the same customer lookup run 500 times, first with the id baked into the SQL and then through `sp_executesql` with the id as a parameter. The ad-hoc form:

```sql
SELECT o.id, o.amount, c.region
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.customer_id = 4123;      -- and 499 other literals
```

and the parameterized form:

```sql
EXEC sp_executesql
  N'SELECT o.id, o.amount, c.region
    FROM orders o JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.customer_id = @cid',
  N'@cid int', @cid = 4123;       -- same text every time, @cid varies
```

(I put a join in there on purpose. SQL Server will sometimes auto-parameterize a trivially simple single-table query for you, which would hide the effect. A join is complex enough that it leaves the ad-hoc text alone, which is the behavior I wanted to catch.)

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

## The bloat

Here's how to see what landed in the cache, straight from the DMVs:

```sql
SELECT p.objtype, COUNT(*) AS plans,
       SUM(CAST(p.size_in_bytes AS bigint))/1024 AS kb,
       SUM(CAST(p.usecounts AS bigint)) AS total_use
FROM sys.dm_exec_cached_plans p
CROSS APPLY sys.dm_exec_sql_text(p.plan_handle) t
WHERE t.text LIKE '%FROM orders%'
GROUP BY p.objtype;
```

The ad-hoc run left 500 plans of type `Adhoc` behind, each with a use count of 1, adding up to about 66 MB of plan cache. The parameterized run left a single `Prepared` plan with a use count of 500, at 136 KB. Same 500 lookups, same results, and one of them filled the cache with 500 times the plans and roughly 500 times the memory.

<figure class="cache-bench">
  <h3>500 lookups, two ways</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">cached plans left behind</p>
      <div class="cb-bar-row"><span>ad-hoc</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">500</span></div>
      <div class="cb-bar-row"><span>parameterized</span><span class="cb-track"><span class="cb-fill" style="--value:0.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">1</span></div>
    </div>
    <div>
      <p class="cb-panel-title">plan cache used</p>
      <div class="cb-bar-row"><span>ad-hoc</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">66 MB</span></div>
      <div class="cb-bar-row"><span>parameterized</span><span class="cb-track"><span class="cb-fill" style="--value:0.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">136 KB</span></div>
    </div>
  </div>
  <figcaption>500 ad-hoc lookups left 500 single-use plans and about 66 MB in the cache; the parameterized version left one plan reused 500 times, at 136 KB. The ad-hoc loop also took 1,861 ms against the parameterized 1,168 ms, because it paid to compile a plan on every single call. Measured on SQL Server 2022, 2,000,000 rows, results in benchmarks/sqlserver-plan-cache/results/.</figcaption>
</figure>

And it isn't just memory. The ad-hoc loop took 1,861 ms; the parameterized loop took 1,168 ms. That gap is compilation, paid 500 times over for plans that were thrown away after one use.

## The server setting that softens it

You can't always parameterize everything. ORMs and generated SQL will hand the server ad-hoc text no matter how you feel about it. For that case there's a server-level setting, `optimize for ad hoc workloads`, that changes what happens on a query's first appearance: instead of caching the whole compiled plan, SQL Server caches a small stub, and only promotes it to a full plan if the exact same query shows up a second time.

```sql
EXEC sp_configure 'optimize for ad hoc workloads', 1;
RECONFIGURE;
```

Since a flood of single-use queries almost never shows up twice, almost none of them get a full plan. I ran the same 500-query ad-hoc flood with it off and then on:

<figure class="cache-bench">
  <h3>Same ad-hoc flood, "optimize for ad hoc workloads" off vs on</h3>
  <div class="cb-bar-row"><span>setting off</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">68,000 KB</span></div>
  <div class="cb-bar-row"><span>setting on</span><span class="cb-track"><span class="cb-fill" style="--value:0.33%;--bar:var(--cb-green)"></span></span><span class="cb-value">222 KB</span></div>
  <figcaption>The identical 500-query ad-hoc workload went from about 66 MB of plan cache to 222 KB with the setting on, because the single-use plans never get past the stub stage. It doesn't make the queries themselves faster, it just stops them from squatting on memory.</figcaption>
</figure>

About 66 MB down to 222 KB, for a setting you can turn on without touching a line of application code. It doesn't make the ad-hoc queries any faster, they still compile every time, it just stops the throwaway plans from squatting in the cache.

## The catch: parameter sniffing

Parameterizing is the right move almost every time, but it isn't free, and the price is a thing called parameter sniffing. Once there's one shared plan, that plan gets compiled for whatever parameter value happened to come through first, and then everybody else gets that same plan whether it fits them or not.

My `orders` table has a skewed `status` column: 1,990,000 rows are `shipped` and only 2,000 are `refunded`. Consider this parameterized query:

```sql
EXEC sp_executesql
  N'SELECT COUNT(amount) FROM orders WHERE status = @s',
  N'@s varchar(16)', @s = 'refunded';
```

The first time it ran, I ran it for `refunded`. The optimizer saw a value matching 2,000 rows, decided a seek on the status index plus a lookup for each match was the cheap way, cached that plan, and returned in 1.4 ms. Then I ran the exact same parameterized query for `shipped`, which matches nearly two million rows. It reused the cached seek-and-lookup plan, and now that "cheap" lookup happened close to two million times. It took 91.8 ms. When I forced the common query to recompile so the optimizer could see the real value and pick a plain scan, it dropped to 12.3 ms.

```sql
-- same query, but let the optimizer see this value
... WHERE status = @s OPTION (RECOMPILE);
```

<figure class="cache-bench">
  <h3>One shared plan, the common value paying for the rare one's plan</h3>
  <div class="cb-bar-row"><span>rare value (primed the plan)</span><span class="cb-track"><span class="cb-fill" style="--value:1.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1.4 ms</span></div>
  <div class="cb-bar-row"><span>common value, sniffed plan</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">91.8 ms</span></div>
  <div class="cb-bar-row"><span>common value, RECOMPILE</span><span class="cb-track"><span class="cb-fill" style="--value:13.4%;--bar:var(--cb-green)"></span></span><span class="cb-value">12.3 ms</span></div>
  <figcaption>The plan compiled for the rare value ran the common value 7.5x slower than a plan that got to see the real value. Same query, same data, the only difference is which value was in the room when the plan was built.</figcaption>
</figure>

Seven and a half times slower, and the only thing that changed was which value the plan happened to be built for. This is the flip side of the reuse you wanted. One shared plan is great as long as it fits every value, but on a skewed column the right plan depends on the value, and then the shared one bites you.

## Stuff worth remembering

- Parameterize the hot ad-hoc queries. That's the difference between one cached plan and hundreds of single-use ones, and between compiling once and compiling on every call.
- When you can't parameterize, like with ORM or generated SQL, turn on `optimize for ad hoc workloads`. It's nearly free and it kept the cache at 222 KB instead of 66 MB in my run, just by not caching a full plan for queries that only ever run once.
- Parameterizing gives you one shared plan compiled for the first value it sees. On skewed columns that's parameter sniffing, and the common value can end up running a plan built for a rare one. `OPTION (RECOMPILE)` on the specific queries where the good plan depends on the value fixes it, at the cost of compiling each time; `OPTIMIZE FOR` and `OPTIMIZE FOR UNKNOWN` are the gentler tools when you know the shape.
- Database-level forced parameterization will kill the bloat wholesale, but it makes sniffing everyone's problem at once, so it's a heavier hammer than it first looks.
- These are laptop numbers demonstrating the mechanism, [the SQL Server container and the script are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/sqlserver-plan-cache). The megabytes and milliseconds came off my machine; the shape of the problem shows up on any SQL Server you point this at.

## The takeaway

Parameterize the hot queries and you trade a cache full of single-use plans for one shared plan that compiles once, which is almost always the right trade. The catch is that the shared plan is built for the first value it sees, so on skewed columns keep `OPTION (RECOMPILE)` in reach for the queries where the good plan genuinely depends on the value. So parameterize where you can, and reach for `OPTION (RECOMPILE)` on the few queries where one plan can't fit every value.
