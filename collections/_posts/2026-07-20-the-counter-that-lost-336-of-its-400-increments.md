---
layout:     post
title:      The Counter That Lost 336 Of Its 400 Increments
date:       2026-07-20
description:    Eight workers incremented a Postgres counter 400 times under READ COMMITTED. Every transaction committed, not one error was raised, and the final balance was 64. The same workload at REPEATABLE READ lost nothing silently, it just told me about all 319 conflicts instead. Then write skew broke an invariant in 200 out of 200 trials at REPEATABLE READ and 0 out of 200 at SERIALIZABLE.
categories: postgres transactions isolation databases
---

If you've ever had a counter, a balance, or an inventory quantity that drifted low over time, and every log line said the write succeeded, this is for you. There's no failed transaction to find. There's no error rate to graph. The application read a number, added to it, wrote it back, and Postgres said yes to every single one of those, and the total is still wrong at the end of the day.

I went looking for the exact size of that gap, so I put eight workers on one row and had each of them increment it fifty times. Four hundred increments, four hundred commits, zero errors. Final balance: 64.

## The problem

READ COMMITTED, the Postgres default, promises you'll never read uncommitted data. That is the entire promise. It does not promise that the value you read a microsecond ago is still the value in the table, and it does not promise that two transactions doing read-then-write on the same row will produce two increments instead of one. When your code does `SELECT balance`, computes `balance + 1` in Python or Java, and then does `UPDATE ... SET balance = 65`, the database has no idea that 65 was derived from a read. It sees a blind write of a constant. Two workers that both read 64 both write 65, and one increment is gone with no error anywhere, because nothing went wrong as far as the database is concerned. Both transactions were perfectly legal.

The stricter levels don't magically make both increments land. They make the conflict *visible*, by refusing to commit the second one and handing you a serialization failure to retry. That's the actual trade you're making, and I wanted to measure it: how much do you lose silently, what does it cost to stop losing it, and where does that cost actually come from. So I built a digest-pinned PostgreSQL 17.10 and started running the same broken pattern at each level.

## Four hundred increments, sixty four survivors

The first experiment is the naive read-modify-write, the one that lives in every codebase that has ever had an ORM in it. Eight threads, fifty iterations each, all on `accounts` row 1, no retry logic anywhere.

```python
# the naive pattern: read into the app, add in Python, write back.
bal = q1(conn, "SELECT balance FROM accounts WHERE id = 1")
exe(conn, "UPDATE accounts SET balance = %s WHERE id = 1", (bal + 1,))
conn.commit()
```

And as a control, the same 400 increments done the correct way at the same READ COMMITTED, where the read never leaves the database:

```python
# the correct RC pattern: one statement, the read happens inside
# the same UPDATE that takes the row lock.
exe(conn, "UPDATE accounts SET balance = balance + 1 WHERE id = 1")
conn.commit()
```

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

<figure class="cache-bench">
  <h3>400 intended increments, 8 workers on one row, no retries</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">final balance, of 400</p>
      <div class="cb-bar-row"><span>RC, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:16.0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">64</span></div>
      <div class="cb-bar-row"><span>RR, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:20.3%;--bar:var(--cb-blue)"></span></span><span class="cb-value">81</span></div>
      <div class="cb-bar-row"><span>SER, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:21.0%;--bar:var(--cb-blue)"></span></span><span class="cb-value">84</span></div>
      <div class="cb-bar-row"><span>RC, balance = balance + 1</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">400</span></div>
    </div>
    <div>
      <p class="cb-panel-title">increments lost with no error raised</p>
      <div class="cb-bar-row"><span>RC, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">336</span></div>
      <div class="cb-bar-row"><span>RR, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0</span></div>
      <div class="cb-bar-row"><span>SER, read-modify-write</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0</span></div>
      <div class="cb-bar-row"><span>RC, balance = balance + 1</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0</span></div>
    </div>
  </div>
  <figcaption>Left, how many of the 400 increments survived. Right, how many vanished without any error being raised. READ COMMITTED lost 336 in total silence, all 400 transactions committed clean. REPEATABLE READ and SERIALIZABLE landed a similar number, 81 and 84, but every one of their 319 and 316 missing increments came back as a SQLSTATE 40001 the application could have retried. The atomic UPDATE at the same READ COMMITTED landed all 400. Measured on PostgreSQL 17.10, results in benchmarks/postgres-isolation-levels/results/.</figcaption>
</figure>

Read the two panels together, because the left panel alone tells you the wrong story. On the left, all three read-modify-write rows look about equally broken, 64 and 81 and 84, and you could squint at that and conclude the isolation level barely matters. It matters completely. The right panel is where it lives. READ COMMITTED's 336 lost increments came with zero exceptions, zero rollbacks, zero anything in your logs. REPEATABLE READ's 319 lost increments came with 319 explicit serialization failures, one per loss, and the numbers reconcile exactly: final balance plus 40001 errors plus silent losses equals 400 in every row.

That's the real difference between the levels. Both of them lost roughly the same number of increments to the same contention, and READ COMMITTED is the one that didn't mention it.

And the bottom row is the reminder that the isolation level was never the actual bug here. `UPDATE accounts SET balance = balance + 1` at plain READ COMMITTED landed all 400 increments, no errors, no retries, 3224 txn/s, because Postgres re-evaluates the row under the write lock and the read never gets a chance to go stale in your application's memory. If you can express the change as a single statement, do that and stop reading here.

## What REPEATABLE READ actually gives you

The next thing I wanted was the plain demonstration of what the snapshot buys, so: open a transaction, read a row and a count, let a completely separate connection commit an UPDATE and an INSERT in the middle, then read both again inside the same transaction.

```python
first_val   = q1(reader, "SELECT val FROM items WHERE id = 1")
first_count = q1(reader, "SELECT count(*) FROM items WHERE category = 'widgets'")

# a second connection commits an UPDATE and an INSERT in between
exe(writer, "UPDATE items SET val = 999 WHERE id = 1")
exe(writer, "INSERT INTO items VALUES (11, 'widgets', 500)")

second_val   = q1(reader, "SELECT val FROM items WHERE id = 1")
second_count = q1(reader, "SELECT count(*) FROM items WHERE category = 'widgets'")
```

```
READ COMMITTED   val 100 -> 999   count 10 -> 11   non_repeatable=True   phantom=True
REPEATABLE READ  val 100 -> 100   count 10 -> 10   non_repeatable=False  phantom=False
```

Under READ COMMITTED every statement gets a fresh snapshot, so the row changed under the reader and a new row appeared in the count. Under REPEATABLE READ the whole transaction sees one snapshot taken at the first statement, and both reads are stable.

Worth noting what happened to the phantom, because the SQL standard says REPEATABLE READ is allowed to permit phantom reads and a lot of documentation still describes it that way. Postgres doesn't implement REPEATABLE READ as the standard's lock-based definition, it implements it as snapshot isolation, and a snapshot doesn't know or care whether a row you can't see is a "new" row or an old one. The count stayed at 10. You get stronger behavior than the level is required to give you, which is nice right up until it makes you believe the level is stronger than it is, and then experiment C happens.

## Two doctors, both went off call

Here's the failure that snapshot isolation genuinely cannot catch, and it's the reason SERIALIZABLE exists as a separate level instead of being redundant with REPEATABLE READ.

The setup is the textbook one. A `doctors` table, two doctors, both on call. The invariant is that at least one has to stay on call. Each doctor's transaction checks the invariant before taking itself off, which is exactly what you would write:

```python
n  = q1(conn, "SELECT count(*) FROM doctors WHERE on_call = true")
ok = n >= 2
gate.wait()                       # both sides have taken their snapshot
if ok:
    exe(conn, "UPDATE doctors SET on_call = false WHERE id = %s", (doctor_id,))
conn.commit()
```

Both transactions read 2, both conclude it's safe, both go off call, and now nobody's on call. Nothing was overwritten. They wrote to *different rows*. There is no lost update here for anyone to detect, the two transactions simply read the same thing and each invalidated the other's read afterwards. That's write skew, and 200 trials of it, barrier-synced so the two transactions genuinely overlap:

<figure class="cache-bench">
  <h3>Write skew, 200 trials of two concurrent transactions each</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">trials ending with zero doctors on call</p>
      <div class="cb-bar-row"><span>REPEATABLE READ</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">200</span></div>
      <div class="cb-bar-row"><span>SERIALIZABLE</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0</span></div>
    </div>
    <div>
      <p class="cb-panel-title">40001 aborts, of 400 transactions</p>
      <div class="cb-bar-row"><span>REPEATABLE READ</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
      <div class="cb-bar-row"><span>SERIALIZABLE</span><span class="cb-track"><span class="cb-fill" style="--value:50%;--bar:var(--cb-green)"></span></span><span class="cb-value">200</span></div>
    </div>
  </div>
  <figcaption>REPEATABLE READ broke the invariant in 200 of 200 trials and raised not one error while doing it, both transactions committing every time. SERIALIZABLE broke it 0 times, aborting exactly one of the two transactions in each trial with SQLSTATE 40001, 200 aborts across 400 transactions. Measured on PostgreSQL 17.10, results in benchmarks/postgres-isolation-levels/results/.</figcaption>
</figure>

200 out of 200, and 0 out of 200. That's the sharpest result in the whole set and it took the fewest lines of code to produce.

The mechanism is that REPEATABLE READ in Postgres only checks for write conflicts, first-updater-wins on a row. These two transactions never touched the same row, so there is no conflict to find, and both commit happily. SERIALIZABLE adds SSI, which tracks the *reads* too, notices that each transaction read data the other one then wrote, spots the dangerous structure in the dependency graph and kills one of them. Same two transactions, same two writes, and the only difference is whether the database was watching what you read.

The thing I'd underline is that experiment B and experiment C are about the same level. REPEATABLE READ was strong enough to block a phantom that the SQL standard says it's allowed to let through, and then it broke a real business invariant in every single trial. Both of those come out of the same mechanism, it hands every statement one consistent snapshot and then checks nothing else, so a phantom can't get in and two transactions reading that snapshot can still write themselves into a state neither of them would have allowed.

## So what does SERIALIZABLE cost

This is where most of the argument actually happens, so I ran a sweep. Each worker does 100 small read-modify-write transactions over a keyspace, retrying on 40001 up to five times, at all three levels, at 2 / 4 / 8 / 16 workers, over a hot 8-row keyspace and a wider 128-row one. 24 configurations.

The number everyone quotes is throughput, so here it is at the worst config in the sweep, 16 workers on 8 rows:

<figure class="cache-bench">
  <h3>16 workers, 100 txns each, over a hot 8-row keyspace</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">throughput, txn/s</p>
      <div class="cb-bar-row"><span>READ COMMITTED</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">5654</span></div>
      <div class="cb-bar-row"><span>REPEATABLE READ</span><span class="cb-track"><span class="cb-fill" style="--value:49.3%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2785</span></div>
      <div class="cb-bar-row"><span>SERIALIZABLE</span><span class="cb-track"><span class="cb-fill" style="--value:51.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2910</span></div>
    </div>
    <div>
      <p class="cb-panel-title">increments that landed, of 1600</p>
      <div class="cb-bar-row"><span>READ COMMITTED</span><span class="cb-track"><span class="cb-fill" style="--value:51.4%;--bar:var(--cb-orange)"></span></span><span class="cb-value">823</span></div>
      <div class="cb-bar-row"><span>REPEATABLE READ</span><span class="cb-track"><span class="cb-fill" style="--value:94.5%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1512</span></div>
      <div class="cb-bar-row"><span>SERIALIZABLE</span><span class="cb-track"><span class="cb-fill" style="--value:94.8%;--bar:var(--cb-green)"></span></span><span class="cb-value">1517</span></div>
    </div>
  </div>
  <figcaption>Left, READ COMMITTED is roughly 2x the throughput of either stricter level. Right, the work those transactions actually accomplished: RC committed all 1600 transactions and produced 823 increments, RR and SER produced 1512 and 1517 with retries. RC's extra speed is entirely the speed of doing half the work. Measured on PostgreSQL 17.10, results in benchmarks/postgres-isolation-levels/results/.</figcaption>
</figure>

READ COMMITTED is about twice as fast, and it is twice as fast because it committed 1600 transactions that between them managed 823 increments. It didn't process more work per second, it processed the same work and dropped half of it on the floor. SERIALIZABLE was slower and landed 1517. If you put those two side by side as a throughput comparison and pick the taller bar, you have chosen the configuration that loses data, and the benchmark told you it was winning.

That comparison held in all 16 of the non-READ-COMMITTED configurations, by the way. Wherever RR and SER both finished their retries, they produced identical increment counts. The safety isn't approximate.

The other half of the answer is that the cost isn't really a property of the level at all, it's a property of how much your transactions fight:

<figure class="cache-bench">
  <h3>SERIALIZABLE retry rate as concurrency climbs, hot keyspace vs wide</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">8-row keyspace, retry %</p>
      <div class="cb-bar-row"><span>2 workers</span><span class="cb-track"><span class="cb-fill" style="--value:10.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">5.2</span></div>
      <div class="cb-bar-row"><span>4 workers</span><span class="cb-track"><span class="cb-fill" style="--value:37.1%;--bar:var(--cb-orange)"></span></span><span class="cb-value">18.4</span></div>
      <div class="cb-bar-row"><span>8 workers</span><span class="cb-track"><span class="cb-fill" style="--value:68.5%;--bar:var(--cb-orange)"></span></span><span class="cb-value">34.0</span></div>
      <div class="cb-bar-row"><span>16 workers</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">49.6</span></div>
    </div>
    <div>
      <p class="cb-panel-title">128-row keyspace, retry %</p>
      <div class="cb-bar-row"><span>2 workers</span><span class="cb-track"><span class="cb-fill" style="--value:3.0%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.5</span></div>
      <div class="cb-bar-row"><span>4 workers</span><span class="cb-track"><span class="cb-fill" style="--value:2.0%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.0</span></div>
      <div class="cb-bar-row"><span>8 workers</span><span class="cb-track"><span class="cb-fill" style="--value:5.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">2.6</span></div>
      <div class="cb-bar-row"><span>16 workers</span><span class="cb-track"><span class="cb-fill" style="--value:11.9%;--bar:var(--cb-green)"></span></span><span class="cb-value">5.9</span></div>
    </div>
  </div>
  <figcaption>Same level, same worker counts, only the keyspace changed. On 8 rows the retry rate climbs to 49.6% at 16 workers and p99 latency goes to 14.2ms. Spread the same workload over 128 rows and 16 workers retries 5.9% at 6.4ms p99, and throughput lands at 5476 txn/s against READ COMMITTED's 6116, within 10%. Measured on PostgreSQL 17.10, results in benchmarks/postgres-isolation-levels/results/.</figcaption>
</figure>

Sixteen times the keyspace took the retry rate from 49.6% to 5.9% and the p99 from 14.2ms to 6.4ms without changing the isolation level at all. When SERIALIZABLE is expensive on your system, that's usually a measurement of your contention, not a verdict on SSI. Widening the hot set is often the cheaper fix than dropping down a level, and it's the one that doesn't cost you correctness.

One caveat on these throughput numbers: they moved 10 to 20% between runs on my laptop, and one earlier READ COMMITTED run threw an 81ms p99 that was pure scheduler noise. The correctness columns, the increment counts and the retry counts, were stable run to run. Trust the shape of the throughput, not the third digit.

## The retry loop is not optional

The one operational thing worth saying plainly, because it's the part people skip: the moment you use REPEATABLE READ or SERIALIZABLE, your application owes the database a retry loop. Both levels handle conflict by aborting a transaction with SQLSTATE 40001 and expecting you to run it again. Without that loop you haven't bought safety, you've traded silent data loss for loud user-facing 500s, which is arguably worse because now it's a support ticket instead of a slow drift.

And the retry has to re-run the whole transaction from the top, including the reads. Retrying just the failed statement on the same connection gets you nothing, the snapshot is what was wrong. In my sweep the retries were bounded at five attempts and at the hottest config 88 transactions still gave up entirely, which is the other thing to know: under enough contention a bounded retry loop is itself a failure mode you have to handle.

## The takeaway

READ COMMITTED doesn't protect a read-modify-write. That's not a bug in it, it's the level doing exactly what it says, and the cost of forgetting that was 336 increments out of 400 with a clean log and zero errors. If the change fits in one statement, `UPDATE ... SET balance = balance + 1` at the default level lands all 400 and you never think about this again. If it doesn't fit in one statement, `SELECT ... FOR UPDATE` the row before you read it, or go up a level and write the retry loop.

REPEATABLE READ gives you a stable snapshot, blocks non-repeatable reads and phantoms, and will still let two transactions read the same rows and write different ones into an invariant violation, 200 times out of 200 in my run. If your correctness argument is "I checked a condition, then wrote," that's write skew and only SERIALIZABLE catches it, at the price of a 40001 you have to retry.

As for what SERIALIZABLE costs, the honest answer from 24 configurations is: mostly it costs whatever your contention already costs. Half the throughput and a 49.6% retry rate on 8 hot rows, within 10% of READ COMMITTED on 128 rows at the same concurrency, identical correct results in both. And when you see READ COMMITTED win a throughput benchmark against it, check what the run actually accomplished before you believe the bar, because in mine the fast one finished 1600 transactions and produced 823 increments. The harness is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-isolation-levels) if you want to reproduce your own lost updates. These are laptop numbers with fsync off, the shape of the thing, not a capacity statement about your database.
