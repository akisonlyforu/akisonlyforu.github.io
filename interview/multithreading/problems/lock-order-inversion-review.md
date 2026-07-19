---
layout: post
title: Lock-Order Inversion Review — spot it by reading
date: 2026-07-19
description: >-
  Lock-order inversion is the one concurrency bug that is genuinely findable by reading, with no reproduction, no dump, and no load test — you list the lock sequences per path…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Stripe and Coinbase PR-review rounds (account transfer is the archetype); AWS L6 "review this service" segments. Distinguished from other problems in this family by having **no symptom to start from** — you are asked to find it before it ships.

### The code under review (described, not shown)

A `TransferService` over an in-memory ledger.

State:

- `Account` objects, each with a `long balance` field and its own intrinsic monitor. Every method on `Account` (`debit`, `credit`, `getBalance`) is `synchronized` on that account.
- A `ConcurrentHashMap<AccountId, Account>` registry.

Methods on `TransferService`:

- `transfer(from, to, amount)` — synchronizes on `from`, and inside that block synchronizes on `to`; checks that `from.balance >= amount`; if so, debits `from` and credits `to`; otherwise throws `InsufficientFunds`.
- `reverse(original)` — a compensating operation used by the refund path. It looks up the original transfer's two accounts and calls the same two-account critical section, but because it was written to express "money flows back", it synchronizes on the **destination** account first and the **source** account second.
- `totalBalance(a, b)` — a reporting helper that synchronizes on `a`, then on `b`, and returns the sum. Callers pass accounts in whatever order they happen to have them.
- `auditAll()` — iterates the registry and, for each account, synchronizes on it and calls a caller-supplied `AuditListener` callback with the balance, while still holding the account's monitor.

Everything is individually `synchronized`. Every field is guarded. A reviewer looking for missing locks will find none.

### The observed symptom

There isn't one yet — this is a pre-merge review. But the on-call runbook from a *previous* incident in a sibling service records:

- Under load, two threads hung permanently. CPU zero.
- `jstack` showed exactly two threads in state **BLOCKED**, each `waiting to lock` a monitor address that the other thread's frame reported as `locked`.
- The JVM printed a **`Found one Java-level deadlock`** section naming both threads.

Assume the same thing is about to happen here.

### Your task

1. Find the inversion by reading. Say which two code paths form it and what the two locks are.
2. Construct the cycle concretely — two threads, two accounts, the exact sequence.
3. Recite the four Coffman conditions and check each one off against the cycle.
4. Give the fixes, and for each **name the Coffman condition it breaks**.
5. Identify the second, non-obvious multi-lock defect in this class, and the third one that isn't a deadlock at all but will hang you anyway.
6. Say how you would prevent the whole class of bug institutionally, not just this instance.

### Clarify before diagnosing

- Do accounts have a stable, unique, totally-ordered identifier? (If yes, global ordering is trivial. If not — say, they're keyed by object identity only — the fix needs an identity-hash ordering with a tiebreak, which is a different conversation.)
- Can `transfer` be called with `from == to`? (Self-transfer under a naive ordering fix is where people write a self-deadlock or a double-lock bug; intrinsic monitors are reentrant so it won't deadlock, but the ordering code must not "lock both" and the balance check must not double-count.)
- Is the `AuditListener` implemented by us or by a caller? (Alien code under a lock is the third defect and the answer decides how bad it is.)
- Is a partial failure acceptable, or must a transfer be all-or-nothing? (Decides whether `tryLock` + backoff is a legal fix here.)

### Why this problem matters

Lock-order inversion is the one concurrency bug that is genuinely **findable by reading**, with no reproduction, no dump, and no load test — you list the lock sequences per path and look for a pair in opposite order. It is therefore the bug interviewers use to test whether you have a *review method* rather than a debugging reflex. It is also the bug with the most crisply provable fix in all of concurrency: global lock ordering makes a cycle mathematically impossible, and the proof is two sentences. Being able to deliver those two sentences is the whole point of the problem.

---

## Strategy

### Classify

Guarded state (Type B), multi-lock section. Catalog #6 — lock-order inversion producing a deadlock cycle, plus its two siblings in the same catalog entry: **alien call under lock** and **wait while holding a second lock**. Sweep 4 (find every lock acquisition order) is the entire diagnosis, and it is mechanical: for each code path, write the ordered list of locks it holds simultaneously; look for two paths whose lists are the same pair in opposite order.

### The invariant being broken

*The waits-for graph over threads is acyclic.* Note what this is **not**: there is no data-race invariant in trouble here. Every field is correctly guarded, every critical section is sound, and the balances would always be right — if the program ever got to the end. This family of bug attacks **liveness**, not safety, and saying that distinction out loud early is a strong opening.

### Diagnosis by reading (the sweep-4 table)

Tabulate the paths:

| Path | Lock 1 | Lock 2 |
|---|---|---|
| `transfer(A, B, x)` | A | B |
| `reverse` of that transfer | B | A |
| `totalBalance(a, b)` | whatever the caller passed first | the other |
| `auditAll` | one account | *(none — but calls alien code while holding it)* |

`transfer` and `reverse` are the planted pair: **the same two locks in opposite order**. `totalBalance` is worse in one respect — it doesn't even have a fixed order, so it inverts against *both* of the others depending on argument order, and reviewers often miss it because there is no "opposite" written down anywhere. A path with an *unspecified* lock order is an inversion against every other path by default; flag it as such.

### Construct the cycle

Two accounts A and B, two threads:

1. Thread 1 begins `transfer(A → B)`. It acquires A's monitor.
2. Thread 2 begins `reverse` of an earlier A→B transfer, which flows B → A, so by its own convention it acquires **B**'s monitor first. It succeeds — B is free.
3. Thread 1 now asks for B. B is held by thread 2. Thread 1 goes **BLOCKED**.
4. Thread 2 now asks for A. A is held by thread 1. Thread 2 goes **BLOCKED**.
5. Waits-for graph: T1 → T2 → T1. A cycle of length two. Neither thread will ever release, because releasing requires exiting a `synchronized` block that neither can reach.

Nothing exotic is required: two threads, two accounts, one unlucky microsecond. It does not need high load — high load only makes it certain rather than occasional.

### Check the four Coffman conditions

All four must hold simultaneously for a deadlock; walk them and tick each:

1. **Mutual exclusion** — a monitor is held by at most one thread. ✓ (Inherent to `synchronized`.)
2. **Hold-and-wait** — T1 holds A while waiting for B. ✓
3. **No preemption** — nothing can take a monitor away from a thread; there is no way to force-release an intrinsic lock. ✓ (This is why intrinsic monitors are less flexible than `ReentrantLock`, which at least offers `tryLock`.)
4. **Circular wait** — T1 → T2 → T1. ✓

All four hold, so the deadlock is not merely possible, it is guaranteed under the right schedule. **Every fix works by breaking exactly one of these four — always name which one.** That sentence is the framing that earns the round.

### The fixes

**Fix 1 — Global lock ordering (breaks circular wait). The production answer.** Impose a total order on accounts — account id is the natural key; if none exists, `System.identityHashCode` with a tiebreak lock for the (rare) hash collision case. Every operation that needs two accounts acquires them in ascending order regardless of the business direction of the money. `transfer`, `reverse` and `totalBalance` all call one shared helper that orders first and acts second; the business semantics (who is debited) are decided *inside*, independent of lock order.

The impossibility proof, in two sentences: *every thread that is waiting is waiting for a lock strictly higher in the order than every lock it currently holds; a cycle would require some thread in the cycle to be waiting for a lock lower than one it holds, which the protocol forbids.* Deliver this verbatim — it is the crispest correctness proof available in concurrency and interviewers listen for it.

Handle the equal case explicitly: if `from == to`, don't acquire twice (intrinsic monitors are reentrant so it won't hang, but the code should short-circuit) and make sure the balance arithmetic doesn't double-apply.

**Fix 2 — `tryLock` with release and randomized backoff (breaks hold-and-wait).** Requires migrating from intrinsic monitors to `ReentrantLock`. Acquire the first, `tryLock` the second; **on failure release the first**, back off a randomized interval, retry. Legal here because a transfer that hasn't started can be retried freely. Two things to say about it: the **fake fix** is retrying *without* releasing the first lock — that is literally hold-and-wait and fixes nothing (catalog #7); and without *randomized* backoff, all contenders can cycle grab-fail-release in lockstep forever, which is livelock — a hang with CPU pegged instead of CPU at zero. Prefer fix 1; offer fix 2 for cases where a global order genuinely can't be defined (locks acquired across subsystems you don't control).

**Fix 3 — Coarsen: one ledger-wide lock (breaks the multi-lock situation entirely).** Every transfer takes a single global lock. Trivially deadlock-free because there is only one lock, so there is nothing to order. Real cost: all transfers serialize, including ones on disjoint accounts. Worth naming as the correct-but-conservative baseline you'd ship if the throughput requirement allows — "simple and correct beats clever and wrong" — while noting exactly what it gives up.

**Fix 4 — `tryLock(timeout)` as defense-in-depth (turns a hang into a failure).** Doesn't prevent the inversion; converts an eternal hang into a timeout exception you can alert on and retry. Use *alongside* fix 1 in code paths that cross ownership boundaries, never instead of it.

### The other two defects

**Defect 2 — alien call under lock (`auditAll`).** It invokes a caller-supplied `AuditListener` while holding an account monitor. You do not control that code. It may take its own locks — introducing a second lock ordering you cannot see, against which your account monitor inverts. It may call back into `TransferService` and re-enter. It may block on I/O, holding the account hostage for the duration. The rule: **never call code you don't control while holding a lock.** Fix: copy the balance out inside the critical section, release, then call the listener with the snapshot — and document that the value is a snapshot, not a live read.

**Defect 3 — the shape that isn't a deadlock but hangs anyway.** If any of these paths ever waits (on a condition, a future, or an I/O call) while holding a second lock, it releases only the monitor it waits on and holds the other for the whole wait. `jstack` will show a thread `WAITING` — not `BLOCKED` — and the JVM's deadlock detector will stay silent, because the detector finds monitor cycles only. Flag this preemptively in review even when it isn't present yet: it is the shape the *next* change introduces.

### Confirm — and what the tooling gives you

This bug's confirmation story is unusually strong: **the JVM detects it for you.** `jstack` (or `jcmd Thread.print`) prints a `Found one Java-level deadlock` section, names the threads, and shows each thread's `waiting to lock <address>` against another's `locked <address>`. `ThreadMXBean.findDeadlockedThreads()` gives the same programmatically, which is worth wiring into a health check.

Limits to state: the detector covers intrinsic monitors and `ReentrantLock` (via `findDeadlockedThreads`), and covers **nothing else** — no semaphore cycles, no lost wakeups, no pool starvation. So a clean deadlock section means "not this bug", never "no hang bug".

To reproduce deliberately: two threads, a start gate, one calling `transfer(A,B)` in a loop and the other calling `reverse` on the same pair. It deadlocks within a handful of iterations. Give the harness a **timeout** so the hang fails the test rather than wedging the suite — and then use that same test as the regression test after the fix.

### Prove the fix

The two-sentence ordering proof above *is* the deliverable. Supplement it with:

- A review-level check: every multi-lock path in the class now routes through the one ordering helper, so no path can define its own order. (This is the real fix — not "we ordered these three call sites" but "there is exactly one place that can acquire two account locks".)
- The regression test: the two-thread inversion harness, which previously deadlocked in a few iterations, now completes millions of iterations under its timeout.
- The negative check: a fresh `jstack` under load shows no deadlock section and no persistently BLOCKED threads across three dumps.

### Prevent the class, not the instance

The senior answer to part 6 of the task:

- **One helper, one order.** Make it impossible to acquire two account locks except through a single ordered entry point. Structure beats vigilance.
- **`@GuardedBy` annotations plus ErrorProne**, so unguarded or ad-hoc access fails the build rather than the review.
- **A lock-ordering policy documented as a lock hierarchy** — assign every lock in the system a level, forbid acquiring downward. Enforceable at runtime in debug builds by a wrapper that asserts the current thread's held-lock levels are ascending.
- **`ThreadMXBean.findDeadlockedThreads()` on a health endpoint**, so the next occurrence is detected in seconds rather than by a user complaint.
- And the design-level move: ask whether the ledger should hold locks at all, or whether transfers should be serialized per-account through single-threaded ownership (an actor or a partitioned queue keyed by account), which removes multi-lock acquisition from the system by construction.

### Pitfalls

1. **Claiming deadlock without constructing the cycle.** Name the two threads, the two locks, the order. A label is not a diagnosis.
2. **Missing `totalBalance`** because it has no *written* opposite order. An unspecified order inverts against everything.
3. **Ordering by the wrong key** — using the business direction, or an id that isn't unique/stable. Ordering by a mutable field is a bug generator.
4. **The fake `tryLock` fix**: retrying without releasing the first lock. That *is* hold-and-wait.
5. **`tryLock` without randomized backoff** — livelock: CPU pegged, no progress, and much harder to diagnose than the deadlock you replaced.
6. **Forgetting `from == to`.**
7. **Believing the deadlock detector's silence** in general. It finds monitor cycles only.
8. **Leaving the alien callback under the lock** because "it's only for auditing". Auditing code is exactly the code most likely to call back into the service.

### Check your understanding

1. List, for each of the four methods, the ordered sequence of locks it holds simultaneously. Which pairs invert?
2. Construct the cycle with two threads and two accounts, step by step.
3. State the four Coffman conditions and check each against your cycle.
4. For each of the four fixes, name the Coffman condition it breaks.
5. Deliver the global-ordering impossibility proof in two sentences, from memory.
6. Why does `tryLock` without releasing the first lock fix nothing?
7. What does the dump look like for this bug versus for a lost wakeup? Which one does the JVM detect, and why can't it detect the other?
8. `auditAll` calls a listener under the lock. Give three distinct ways that hangs you.

### Transfers to

`dining-philosophers` (the same cycle with five threads and the same three fixes — build side), any two-resource operation (seat + payment, inventory + reservation, parent + child node in a tree with hand-over-hand locking), and every distributed-systems analogue where two services take each other's locks in opposite order. The Coffman-plus-named-fix structure is reusable verbatim for any deadlock question in any round.
