---
layout:     post
title:      Too Many Clients Already
date:       2026-07-19
description:    Traffic doubles, the app opens more connections to Postgres, and the database starts turning callers away with FATAL sorry too many clients already. The query was never the problem, the connection was. Same load through PgBouncer in transaction mode answered every caller with a tenth of the backends. Here's the reproduction.
categories: postgres pgbouncer connection-pooling databases performance
---

Traffic doubles on a Friday and the app does the reasonable thing, it opens more connections to Postgres so more work can happen at once. For a while that even looks like the fix. Then the error log starts filling with the same line over and over, `FATAL:  sorry, too many clients already`, and the callers that hit it aren't slow, they're rejected. The database didn't run out of CPU, it didn't run out of disk, it ran out of the one resource nobody put on a dashboard, which is permission to connect at all.

The frustrating part is that the queries were never the problem. `SELECT 1` against Postgres is about as cheap as work gets. What isn't cheap is the connection you ran it on, and once you're opening and closing those fast enough, or holding enough of them at once, the connection is the whole cost. I didn't fully believe how lopsided that was until I put a pooler in front and watched the same load stop hurting, so I built a small harness and made both cases happen on purpose.

## The problem

A connection to Postgres is not a socket, it's a process. Every client that connects gets its own backend process on the server, forked at connect time, and that process does the TCP handshake, the auth exchange, some per-backend setup, and only then is it ready to run your one-line query. When your app opens a connection per request, runs a query, and closes it, you are paying for a whole process lifecycle to do a microsecond of actual work. You're not paying for the query, you're paying for the connection it rode in on.

And there's a hard ceiling on top of it. `max_connections` is fixed at server start, and a few of those slots are reserved for the superuser, so the real number of clients that can be connected at once is a wall you hit abruptly. Not a slowdown, a wall. Cross it and Postgres doesn't queue you, it rejects you with `too many clients already`. So the naive shape, one connection per caller, means the number of processes on your database is set by how many clients happen to show up, and the database has no say in it right up until it says no to everyone.

A pooler fixes both halves of that. PgBouncer sits between the app and Postgres, keeps a small set of real backend connections warm, and hands them out to clients a transaction at a time. Thousands of app connections, ten real ones behind them. Here's what that actually buys, measured.

## The connection costs more than the query

First experiment is the churn case, the one that looks like normal app traffic. Open a connection, run one tiny query, close it, five thousand times, across fifteen workers. Once straight at Postgres, once at PgBouncer sitting in front of the same Postgres. Same query, same machine, the only difference is who answers the connect.

<figure class="cache-bench">
  <h3>5,000 connect → query → close, throughput</h3>
  <div class="cb-bar-row"><span>direct to Postgres</span><span class="cb-track"><span class="cb-fill" style="--value:48.3%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1,590/s</span></div>
  <div class="cb-bar-row"><span>via PgBouncer</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">3,289/s</span></div>
  <figcaption>Same one-line query, same Postgres. The pooler kept a handful of backends warm instead of forking a fresh one per unit, and did 2.1x the work for it. Measured on Postgres 16.14 behind PgBouncer 1.25.2, results in benchmarks/pgbouncer-connection-pool/results/.</figcaption>
</figure>

Twice the throughput and I never touched the query. All PgBouncer did was stop paying the connect cost every single time. When the client "connects" to the pooler it's grabbing an already-open backend from a warm pool, so the fork and the auth and the setup happened once, earlier, not on the hot path. The per-request latency shows the same thing from the other side.

<figure class="cache-bench">
  <h3>Per-request latency, connect included</h3>
  <div class="cb-bar-row"><span>direct · p50</span><span class="cb-track"><span class="cb-fill" style="--value:52.9%;--bar:var(--cb-orange)"></span></span><span class="cb-value">9.00 ms</span></div>
  <div class="cb-bar-row"><span>direct · p95</span><span class="cb-track"><span class="cb-fill" style="--value:80.1%;--bar:var(--cb-orange)"></span></span><span class="cb-value">13.62 ms</span></div>
  <div class="cb-bar-row"><span>direct · p99</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">17.01 ms</span></div>
  <div class="cb-bar-row"><span>pooled · p50</span><span class="cb-track"><span class="cb-fill" style="--value:24.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">4.23 ms</span></div>
  <div class="cb-bar-row"><span>pooled · p95</span><span class="cb-track"><span class="cb-fill" style="--value:36.3%;--bar:var(--cb-green)"></span></span><span class="cb-value">6.18 ms</span></div>
  <div class="cb-bar-row"><span>pooled · p99</span><span class="cb-track"><span class="cb-fill" style="--value:55.1%;--bar:var(--cb-green)"></span></span><span class="cb-value">9.37 ms</span></div>
  <figcaption>The pooled p99, 9.37ms, is lower than the direct p50, 9.00ms. Half of the pooled requests are faster than the median direct request, and the tail is roughly halved too. That whole gap is connection setup you weren't measuring. Measured on Postgres 16.14, results in benchmarks/pgbouncer-connection-pool/results/exp_a_latencies.csv.</figcaption>
</figure>

The line I keep coming back to is that the pooled p99 came in under the direct p50. The slowest one percent of requests through the pooler beat the middle request going direct, and both are running the identical `SELECT`. None of that is query time. It's the connect you stopped doing.

## The wall you hit all at once

Throughput is the nice version of the story. The ugly version is the burst. I set `max_connections` to 25 on purpose, which after the reserved superuser slots leaves about 22 usable, and then I threw 100 clients at the database at once, each holding its connection just long enough that they overlap. Direct, then pooled.

<figure class="cache-bench">
  <h3>100 simultaneous clients, max_connections = 25</h3>
  <div class="cb-bar-row"><span>direct, rejected</span><span class="cb-track"><span class="cb-fill" style="--value:75%;--bar:var(--cb-orange)"></span></span><span class="cb-value">75 / 100</span></div>
  <div class="cb-bar-row"><span>pooled, rejected</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0 / 100</span></div>
  <figcaption>Going direct, 75 of the 100 clients were turned away, every one of them with <code>FATAL:  sorry, too many clients already</code>. Through PgBouncer, all 100 succeeded. Same ceiling, same 25 real slots, the difference is who's allowed to queue for them. Measured on Postgres 16.14, results in benchmarks/pgbouncer-connection-pool/results/exp_b_outcomes.csv.</figcaption>
</figure>

Direct, three quarters of the burst got nothing but the error. Not a slow answer, an error, the kind your app has to decide what to do with, and usually the answer is a 500 or a retry storm that makes the next second worse. Through the pooler, every client got served. PgBouncer accepts up to `max_client_conn` connections, which I'd set to 1000, and only ever holds a small pool of real backends open to Postgres. When the pool is busy the extra clients wait in PgBouncer's queue for a backend to free up, they don't get shoved into Postgres and bounced. The wall is still there, Postgres still only has 25 slots, but the pooler turned "rejected" into "wait a moment," and a moment is almost always what the client wanted anyway.

That last count is the honest, lumpy part of this benchmark. Direct sheds a big chunk every run, but the exact survivor count wobbles between roughly 15 and 25 depending on how many of the ~22 slots happen to be free at the instant the burst lands. The shape never wobbles, direct always sheds most of the burst and pooled always loses nobody, but I'm not going to pretend the 75 is a precise constant. It's the sign that matters, not the last digit.

## Backends are processes, and you're paying for all of them

The third thing a pooler buys is quieter and it's about what's actually running on the box. I held 20 clients open at once, doing brief work, and counted the client backend processes Postgres had spun up for them.

<figure class="cache-bench">
  <h3>Client backend processes for 20 held clients</h3>
  <div class="cb-bar-row"><span>direct</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">20</span></div>
  <div class="cb-bar-row"><span>via PgBouncer</span><span class="cb-track"><span class="cb-fill" style="--value:50%;--bar:var(--cb-green)"></span></span><span class="cb-value">10</span></div>
  <figcaption>20 clients, 20 backends going direct. The same 20 clients through PgBouncer needed 10 backends, exactly the default_pool_size, because in transaction mode the pool multiplexes them. Measured on Postgres 16.14 with default_pool_size=10, results in benchmarks/pgbouncer-connection-pool/results/exp_c_backends.csv.</figcaption>
</figure>

Direct, it's one backend per client, no surprise, 20 clients means 20 processes. Pooled, the same 20 clients rode on 10 backends, exactly the `default_pool_size` I'd configured, because transaction pooling means a backend belongs to a client only for the length of a transaction and then goes back in the pool for the next client. The clients think they each have a private connection. They're sharing ten.

Now the honest caveat, because this is the one that didn't reproduce the way the topic promises. I also measured total Postgres backend memory and it barely moved, 36573 KB direct versus 36453 KB pooled, basically a tie. The reason is that `ps` RSS counts each backend's slice of `shared_buffers`, which is the same shared memory over and over, and an idle backend's actual private memory is tiny. At 20 backends on a laptop the memory difference is noise. The process count is real and the memory story is real too, but you don't see the memory bite until you're at hundreds or thousands of backends, each with its work_mem allocations and its private state, and that's exactly the scale where you can't run the experiment on a laptop. So I'm showing you the count, which reproduces cleanly, and telling you plainly that the RSS number didn't, rather than dressing up a 120 KB difference as a result.

## So what does transaction pooling cost

Nothing's free. Transaction pooling is the aggressive mode, the one that gives you the 20-to-10 multiplexing, and it works precisely because a backend isn't tied to a client between transactions. Which means anything that lives on the connection across transactions is off the table. Session-level `SET` that you expect to persist, `LISTEN`/`NOTIFY`, session-scoped advisory locks, `WITH HOLD` cursors, and the big one in practice, server-side prepared statements, because the statement you prepared on one backend isn't there when your next transaction lands on a different backend. Most app frameworks have a switch for this, you turn off server-side prepared statements or point them at PgBouncer's compatibility settings, and you move on. But you have to know it's there, because the failure mode is a confusing "prepared statement does not exist" that shows up only under pooling.

If your app genuinely needs session state, PgBouncer has `session` mode, which hands a backend to a client for the whole connection and gives most of that back. You lose the transaction-level multiplexing but you still kill the connect churn and you still get the queue in front of `max_connections`. Two of the three wins here survive session mode. Only the process-count one needs transaction mode.

## The takeaway

If your app opens connections to Postgres directly and your traffic is spiky or your connection count is climbing, put a pooler in front of it. PgBouncer in transaction mode, a `default_pool_size` in the low tens, `max_client_conn` set high, pointed at a Postgres whose `max_connections` you can now keep sane instead of cranking. In this run that one box bought 2.1x the throughput on the churn workload, cut the p99 roughly in half, turned a 75% rejection burst into zero failures, and served 20 clients on 10 backends. The cost is one config file, one more process to run, and a checklist item to turn off server-side prepared statements.

The thing to remember underneath all three experiments is that a Postgres connection is a process, not a socket. Everything expensive here, the setup you pay per request, the wall at `max_connections`, the process per client, follows from that one fact. `max_connections` is not the number of callers you can serve, it's the number of backends you can afford to have forked at once, and a pooler is how you stop confusing the two. So when the connection count starts climbing, the reflex to crank `max_connections` is treating the symptom, it just lets more callers contend for the same slots at once. The pooler is what treats the cause, and it's a config file.

The harness, both docker-compose cases and the benchmark script, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/pgbouncer-connection-pool). These are laptop numbers, so read the ratios, not the absolutes. The 2.1x and the 75-to-0 are the point, not the milliseconds.
