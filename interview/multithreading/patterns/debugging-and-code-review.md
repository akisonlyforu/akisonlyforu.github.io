---
layout: post
title: Debugging & Code Review Playbook
date: 2026-07-19
description: >-
  Running the method backwards from a symptom: the five-sweep scan, a symptom-to-cause table, thread dumps and BLOCKED versus WAITING, deterministic reproduction, and how to prove a fix.
categories: interview multithreading patterns
---

Deep dive on the debugging family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). Every other family builds forward from a problem statement. This one starts from a symptom and someone else's code, which is a different skill and increasingly its own interview round.

Every other family in this bank hands you a blank editor and asks you to *build* something correct. This family hands you working-looking code that is already deployed, already passing its tests, and already wrong, and asks you to **find the bug, prove it's the bug, and fix it without breaking anything else**. Stripe runs this as an explicit round; Uber L5+, Coinbase, Airbnb, AWS L6 and Rubrik all fold it into system-design or "here's a PR" segments. The canonical Stripe artifact is a report that reads *"totals drift under high concurrency"* and resolves to **an unguarded read-modify-write**.

The skill being graded is not "can you spot a missing `synchronized`". It is:

1. **Scan discipline**: do you read concurrent code in a fixed order that *cannot* miss the classic bugs, or do you skim until something looks funny?
2. **Diagnosis under uncertainty**: can you go symptom → hypothesis → *deterministic reproduction* → confirmation, instead of guessing and patching?
3. **Proof**: can you state why the fix is correct as an argument, not as "the stress test went green"?
4. **Prioritisation**: given five real defects in one file, can you rank them by blast radius and say which one you'd ship tonight?

The inversion to internalize: in the build families the *correctness argument* is the deliverable. Here the **diagnosis** is the deliverable, the fix is usually three lines, and candidates who lead with the three lines fail the round.

---

## 1. The systematic scan order (five sweeps, always this order)

Read the code five times, each time looking for exactly one thing. Say the sweep names out loud as you go; the interviewer is grading the method as much as the find. The order is chosen so each sweep narrows the surface for the next.

**Sweep 1, Find the shared mutable state.** Enumerate every field a second thread can read or write: instance fields of an object handed to multiple threads, statics, anything reachable from a collection that escapes, anything captured by a lambda submitted to an executor. For each, write down *what guards it*, a lock, `volatile`, `final`+safe publication, confinement, or **nothing**. The "nothing" column is your bug list; everything after this is refinement. Immutable and confined fields drop out permanently, cross them off loudly so the interviewer sees you're pruning, not skimming.

**Sweep 2, Find every check-then-act.** Any place a decision is made on a read and acted on later: `if (x == null) x = ...`, `if (!map.containsKey(k)) map.put(k, v)`, `if (!q.isEmpty()) q.poll()`, `if (size < cap) add()`, and the miniature version that hides best, `count = count + 1`, `balance -= amount`, `list.set(i, list.get(i) + d)`. **Read-modify-write is check-then-act in miniature**, and it is the single most-reported production concurrency bug in the world. For each pair ask: *is the check and the act inside one critical section, or one atomic instruction?* If they're in two different critical sections, that is the same bug as no lock at all, the race lives in the gap.

**Sweep 3, Find every wait/signal pair.** For each `wait`/`await`/`park`: (a) is it inside a `while` loop re-testing the predicate, or an `if`? (b) is the predicate a *persisted state* check, or is it relying on a transient event? For each `notify`/`signal`: (c) does it happen *after* the state change and *while still holding the lock*? (d) does the wait set contain more than one kind of waiter, if so, `notify` can wake the wrong party and the signal is consumed. Every `notify` on a monitor with mixed waiters is a defect until proven otherwise. Also check: does every state change that could make *someone's* predicate true have a signal after it? Missing signals are lost wakeups too.

**Sweep 4, Find every lock acquisition order.** List, per code path, the sequence of locks held. Any operation holding two or more locks is a candidate. Build the pairs: path A takes (L1, L2), path B takes (L2, L1), that's a cycle, and it needs no third thread and no exotic timing. Also check the non-obvious multi-lock shapes: **calling alien code while holding a lock** (a listener, a callback, an overridable method, a lambda you didn't write), and **waiting on monitor M while holding lock N** (you release only M; N is held for the entire wait).

**Sweep 5, Find every lifecycle path.** Follow work from submission to death: who accepts it, what happens when the queue is full (is it even bounded?), what happens when the task throws, what happens when the thread is interrupted, what happens at shutdown, what happens to `ThreadLocal` state after the task returns on a *pooled* thread. Then follow every `finally`: is every acquire matched by a release on the exception path? Missing `finally` around a release/decrement is a delayed-detonation bug, the system keeps running and fails minutes later, in a different place.

Two cross-cutting checks to run at the end of every sweep set:

- **Publication audit.** How does a second thread first obtain a reference to this object? If the answer isn't final-field / volatile / lock the reader also takes / concurrent handoff / class-init, the reader may see a half-built object. Check for `this` escaping the constructor (registering a listener, starting a thread, passing `this` to a collection).
- **Composition audit.** Every method may be individually thread-safe and the *caller's two-line sequence* still racy. Read the call sites, not just the class.

## 2. Symptom → cause lookup table

The interviewer usually opens with a symptom, not code. Map it before you read a line, it tells you which sweep to run first.

| Symptom (as reported) | First hypotheses | Sweep to run first |
|---|---|---|
| **Drift**, "totals are slightly low", counts don't match input, balances off by a little under load | Unguarded read-modify-write; lost update; two locks over one invariant; `volatile` mistaken for atomic | 1, then 2 |
| **Drift that's always low, never high** | Lost update specifically (increments overwrite each other; nothing invents extra) | 2 |
| **Hang, all threads idle, CPU ~0%** | Lost wakeup (`if` vs `while`, `notify` vs `notifyAll`, signal before state change, missing signal); or waiting on a future inside a full fixed pool | 3, then 5 |
| **Hang with a lock cycle in the dump** | Lock-order inversion / classic deadlock | 4 |
| **Hang, CPU pegged** | Livelock (retry without backoff), or busy-wait spin on a non-volatile flag | 4, then 1 |
| **Works in dev, hangs or NPEs in prod** | Visibility: non-volatile flag/field; unsafe publication. Dev is single-core-ish, lightly loaded, JIT cold | 1 |
| **Started failing after we upgraded the JVM / added -server / warmed up** | Same, JIT hoisted the non-volatile read out of the loop, or reordered constructor stores. The bug was always there | 1 |
| **NPE or half-initialized object on a field that's clearly assigned** | Unsafe publication; `this` escaped the constructor; non-volatile double-checked locking | 1 + publication audit |
| **Duplicate processing / two owners of one item** | Check-then-act on a claim (`contains` then `add`); claim-after-work | 2 |
| **Throughput decays to zero over hours; no errors in the log** | Silent worker death (no per-task catch), or pool leak | 5 |
| **Memory grows until OOM under load; latency fine until it isn't** | Unbounded queue hiding overload; `ThreadLocal` leak on pooled threads | 5 |
| **Task won't cancel; shutdown never completes; JVM won't exit** | Swallowed `InterruptedException`; flag-only shutdown; missing `shutdown()` with non-daemon threads | 5 |
| **Heisenbug, vanishes when you add logging** | Almost always a race: the log call is a synchronized I/O barrier that both slows the window and adds happens-before edges | 1, 2 |
| **Fails only on one machine / only in CI** | Core count, memory model strength (x86 is much more forgiving than ARM), thread count, or timing. ARM/Apple-silicon CI exposing an x86-passing bug is a classic | 1 |
| **Fails at a period, every Nth cycle, or after the first round** | Generation/reuse bug: reused one-shot latch, permit theft, lapping threads, stale state carried between cycles | 3, 5 |

The two entries worth memorizing verbatim because they are the two most common *reported* symptoms: **"totals drift under load" → unguarded read-modify-write** and **"works in dev, hangs in prod" → missing `volatile` on a stop flag**.

## 3. The diagnostic toolkit

### 3a. Thread dumps, and the three states you must distinguish

`jstack <pid>` (or `jcmd <pid> Thread.print`, or `kill -3`) is the first instrument for any hang. Take **three dumps, ten seconds apart**, a single dump can't distinguish "stuck" from "slow". If the stacks are identical across all three, it's stuck.

The state word next to each thread is the diagnosis:

- **BLOCKED**: waiting to *enter* a `synchronized` block, i.e. waiting for a monitor someone else owns. The dump shows `waiting to lock <0x...>` and, elsewhere, another thread with `locked <0x...>`. **BLOCKED means contention or deadlock.** Follow the addresses: if the "waiting to lock" edges form a cycle, you have a lock-order inversion. The JVM detects monitor cycles for you and prints a `Found one Java-level deadlock` section, but only for monitors and `ReentrantLock`s it can see, and **never** for a lost wakeup.
- **WAITING**: parked voluntarily inside `Object.wait()`, `Condition.await()`, `LockSupport.park()`, `Thread.join()`, `Future.get()`, `CountDownLatch.await()`. Nobody is holding anything against it; **it is waiting to be told**. A dump where *every* interesting thread is WAITING and no thread is BLOCKED, with no deadlock section, is the signature of a **lost wakeup**, the system is not deadlocked in the Coffman sense at all, it is simply asleep because the wakeup that would have started it was consumed, missed, or never sent.
- **TIMED_WAITING**: same, but with a deadline (`sleep`, `wait(ms)`, `await(timeout)`, `poll(timeout)`). Sees the same causes as WAITING but self-heals eventually, which is *worse* for diagnosis, because it turns a hang into intermittent latency spikes.
- **RUNNABLE** with the same stack across three dumps, either a genuine CPU loop, a livelock (stack cycles through a retry), or a spin on a stale non-volatile field. Note that a thread blocked in a socket read also shows RUNNABLE; the JVM can't tell.

**The one-sentence rule to say out loud:** *BLOCKED means someone is holding what I want; WAITING means nobody is holding anything and nobody told me to wake up, a hang made of WAITING threads with no lock cycle is a lost wakeup, not a deadlock.*

Also read from the dump: how many threads share a pool name prefix (has the pool shrunk? → silent worker death), and how many threads exist at all (thread leak).

### 3b. Deterministic reproduction: turning a heisenbug into a bug

A race you can't reproduce on demand is a race you can't prove fixed. Four techniques, cheapest first:

- **Start gate.** Have all N threads `await()` on one latch (or a barrier) before touching the target. Without it, thread 1 has usually finished before thread 8 is created and the window never opens. This one change converts most "1 in 10,000" races into "1 in 3".
- **Injected delay at the exact window.** Once you have a hypothesis, "the bug is between the read and the write", put a sleep, a `Thread.yield()`, or an `onSpinWait` *at that point* (via a test hook, a subclass, or a debug flag) and watch the failure become 100%. This is the strongest form of confirmation available without special tooling: it demonstrates you know *precisely* which interleaving is at fault, not just that something is racy.
- **Breakpoint interleaving.** In a debugger set to suspend a *single thread* rather than the VM, park thread A inside the window and let thread B run through. Slow, manual, but decisive for a two-thread bug and it is a legitimate answer to "how would you confirm it?".
- **Amplification.** Many iterations (10k+), more threads than cores (forces preemption), skewed thread speeds (some threads with an extra delay so they lap each other), and running on a weakly-ordered machine (ARM) rather than x86 for visibility bugs. Also: disable the JIT (`-Xint`) to see if the bug *disappears*, if it does, you're looking at a reordering/hoisting bug, which is a diagnosis in itself.

State the reproduction *before* the fix. "Here's how I'd make it fail every time" is the sentence that separates senior from mid in this round.

### 3c. Stress harness shape

```
harness(threadsN, itersN, op, invariantCheck):
    gate = latch(1); done = latch(threadsN)
    pool = fixed(threadsN)
    for t in 1..threadsN:
        submit:
            gate.await()                # start gate: maximize overlap
            for i in 1..itersN: op()
            done.countDown()
    gate.countDown()
    if not done.await(TIMEOUT): FAIL("hang")   # timeout so hangs fail fast
    assert invariantCheck()                    # e.g. counter == threadsN*itersN
```

Three properties that make it a real harness rather than theatre: the **start gate**, the **timeout** (a hang must fail the test, not hang the suite), and an **invariant assertion** with an exact expected value (not "roughly right").

### 3d. Tooling at awareness level

- **jcstress**: the JDK's concurrency stress harness. It runs tiny two-thread test snippets millions of times under aggressive JIT settings and *enumerates the observed result set*, so it can catch reordering and visibility bugs a normal test never sees. The right name to drop for "how would you test that this `volatile` is actually required?"
- **Java Flight Recorder / async-profiler in lock mode**: for *contention* rather than correctness: which monitor is hot, how long threads park. Answers "the fix is correct but now it's slow."
- **ThreadSanitizer (TSAN)**: the C/C++/Go dynamic race detector; it instruments memory accesses and reports two conflicting accesses with no happens-before edge between them. Java's nearest analogue is the (largely historical) FindBugs/SpotBugs `MT_CORRECTNESS` static checks plus ErrorProne's `GuardedBy` checking, annotate fields `@GuardedBy("lock")` and the compiler will flag unguarded accesses. Mentioning `@GuardedBy` as *documentation that is also enforced* is a strong review-round remark.
- **Model checking / JPF**: exhaustive interleaving exploration. Awareness only; say the name, say it doesn't scale past small models, move on.

## 4. Testing concurrent code (and why a green test proves nothing)

A passing concurrency test proves that *the schedules that happened to run* did not expose the bug. The schedule space is astronomically larger and the scheduler is adversarial exactly when you're on call. So:

**The correctness argument is the deliverable; the test is a smoke check.** For every fix, produce one of: a lock-coverage argument ("every read and write of `balance` happens under `this` monitor, so check and act are atomic"), a happens-before chain ("constructor writes HB volatile write HB volatile read HB reader's field reads"), a permit-conservation count, or the two-sentence lock-ordering impossibility proof ("every waiter waits for a lock higher than any it holds; a cycle would require some thread to wait for a lower one").

**Then stress properly anyway.** The checklist:

- **Start gate** so threads actually overlap (a `CountDownLatch(1)` all workers await).
- **Timeouts on every join/await** so a hang fails the test in seconds instead of wedging CI. A test that can hang forever is worse than no test.
- **Invariant assertions with exact expected values**: final counter equals `threads × iters`, sum of all balances equals the initial sum, no element dequeued twice, no element lost.
- **Many iterations, and more threads than cores**: forces real preemption inside your windows.
- **Skewed thread speeds**: give some threads an extra delay so fast threads lap slow ones. Generation/reuse bugs (stale barrier, recycled latch, permit theft) *only* appear under lapping.
- **Both parities and both extremes of N**: 1 thread, 2 threads, N = capacity, N = capacity ± 1. Off-by-one boundary bugs hide at the edges.
- **Run it many times, on more than one machine**: and prefer an ARM box for visibility bugs; x86's stronger memory model hides them.
- **Never use `sleep` as proof of coordination**: a test that passes because a sleep was long enough is a test that will flake.

**And know what stress cannot do:** it cannot prove absence of a race, it cannot reliably reach rare interleavings, and it degrades as an oracle when the invariant is only checkable at quiescence. That's why the injected-delay reproduction (§3b) matters, it converts a probabilistic test into a deterministic one *for the specific interleaving you claim is the bug*, and re-running that same deterministic test after the fix is the closest thing to proof a stress harness can offer.

## 5. Running the review round out loud

The ceremony, roughly 20–30 minutes:

1. **Restate the symptom precisely** and ask the questions that partition the hypothesis space: *Is it wrong data or no progress?* (drift vs hang, completely different sweeps.) *Does it reproduce under load only?* *Is it always in the same direction, always low, never high?* *What changed before it started?* (deploy, JVM, machine class, traffic.) *Does a thread dump show anything BLOCKED?*
2. **Announce the scan order** and run the sweeps (§1) on the code, narrating what you cross off. Crossing things off is signal: "these three fields are final and set in the constructor, so they're out."
3. **State the hypothesis as an interleaving**, not as a label. Not "there's a race on `balance`" but "thread A reads 100, thread B reads 100, A writes 110, B writes 105, B's write is computed from a stale read and A's deposit is lost."
4. **Say how you'd reproduce it deterministically** (start gate + injected delay at the named window).
5. **Say how you'd confirm** (thread dump states, `@GuardedBy`, jcstress, the exact assertion that fails).
6. **Offer the fix ladder, not one fix**: usually three: the atomic primitive, the lock, and the redesign that removes the shared state. Say which you'd ship and why. Naming the trade-off (contention, throughput, blast radius of the change) is where the senior points are.
7. **Prove the fix** with the correctness argument, then note what the fix does *not* fix (usually: the composition at the call sites is still racy).
8. **Sweep for the rest.** Real code has more than one bug; the round often has a planted second defect. Finish with "I'd also flag X and Y, and I'd rank them: X first because it silently loses money, Y is a latency risk only."

**Prioritisation rubric** for multi-bug reviews, rank by, in order: (a) silent data corruption, (b) unbounded resource growth / liveness loss in production, (c) cancellability and operability, (d) latency and contention, (e) style and documentation. A lost update outranks a missing `shutdown()` outranks an over-coarse lock.

Anti-patterns that fail this round: jumping to a fix in the first 60 seconds; sprinkling `synchronized` without naming the invariant; saying "add `volatile`" for a compound update (freshness ≠ atomicity); claiming a deadlock without constructing the cycle; declaring victory because a stress run went green.

## 6. The fix ladder (and choosing among fixes)

Once diagnosed, there are usually exactly four kinds of fix. Present them in this order and pick with a reason.

- **F0, Remove the shared mutable state.** Make it immutable, confine it to one thread, or accumulate per-thread and merge at the end. No lock can be wrong if there's nothing to guard. Always ask this first; it is the fix a senior reviewer proposes and a mid-level one doesn't.
- **F1, Use an atomic primitive.** `AtomicLong.incrementAndGet`, `LongAdder` (hot contended counters, trades a cheap exact `sum()` at quiescence for much better write throughput), `compareAndSet` retry loop, `ConcurrentHashMap.compute`/`merge`/`putIfAbsent`. Correct when the compound operation is *exactly one* atomic step and no other invariant spans it.
- **F2, Widen the critical section.** Put the check and the act under one lock, or make the monitor cover the whole invariant. Correct when the invariant spans more than one field, which no single atomic can cover. The cost is contention; name it.
- **F3, Change the protocol.** Global lock ordering, `tryLock` + release + backoff, two `Condition`s instead of one wait set, `notifyAll` instead of `notify`, bounded queue with an explicit rejection policy, poison pills for shutdown. Correct when the bug is structural rather than local.

The trap in this ladder: **F1 applied to an invariant that spans two fields is a fake fix.** Making each of two counters atomic does not make "the two counters always sum to N" hold. Say this whenever you reach for an atomic.

## 7. Failure-mode catalog (keyed to the refresher's 21 bugs)

The refresher's catalog is the universal list; this table is it, re-indexed by *how the bug shows up in a review* rather than by how you'd avoid it while building. Sweep the numbers; the sweep column tells you which read finds it.

| # (refresher) | Bug | Review signature, what you see reading the code | Symptom in production | Sweep |
|---|---|---|---|---|
| 1 | Check-then-act outside the lock (incl. `count++`, contains+put, derive-then-consume) | A read and a dependent write not in one critical section; `x = x + 1` on a shared field; `containsKey` then `put` | Drift, lost updates, duplicates | 2 |
| 2 | Lost wakeup, transient signal, or missing notify after a state change | A state mutation with no signal after it; a signal sent before the mutation or outside the lock; a "wake me" that carries no persisted state | Intermittent freeze; all threads WAITING | 3 |
| 3 | Wrong-party wakeup, `notify` with mixed waiters | One monitor, two kinds of waiter, `notify()` | Freeze under mixed load only | 3 |
| 4 | `if` instead of `while` around a wait | Literally `if (!condition) wait();` | Rare corruption or freeze; worse after JVM upgrades | 3 |
| 5 | Unsafe publication, non-volatile DCL, `this` escape, unlocked reads of guarded state | A field written under a lock and read without it; a constructor that registers/starts/publishes `this`; DCL on a non-volatile field | NPE or half-object in prod only; "works in dev" | 1 + publication audit |
| 6 | Lock-order inversion; alien call under lock; `wait()` holding a second lock | Two paths taking the same two locks in opposite order; a callback/listener/lambda invoked inside a critical section | Hang with a lock cycle; JVM prints a deadlock section | 4 |
| 7 | Fake `tryLock` fix (retry without releasing); lockstep livelock (no backoff) | A retry loop that never releases the first lock; a retry with a fixed (non-random) delay | Hang, or hang with CPU pegged | 4 |
| 8 | Compound-operation race, safe methods, racy caller sequences | Two thread-safe calls in a row at a call site; concurrent-collection methods composed | Drift, duplicates | 2 + composition audit |
| 9 | Permit theft / generation mixing, reused one-shot barrier | A `CountDownLatch` reused for round 2; a permit released by a thread that didn't acquire it | Fails on the second cycle | 3, 5 |
| 10 | Double-selection, decide and decrement not atomic | Selecting a waiter and decrementing a count in two steps | Two dispatchers serve the same waiter | 2 |
| 11 | Boundary leak, per-thread permit re-issue instead of centralized | Each thread releasing its own permit at a boundary | Slow permit inflation | 3 |
| 12 | Torn counter, two "first" readers; unguarded "last one" counts | `if (++count == 1)` outside the lock; the last-arriver branch unguarded | Delayed detonation: the *next* cycle corrupts | 2 |
| 13 | Queue-empty false termination, in-flight work invisible | Termination decided by `queue.isEmpty()` | Silent partial results | 5 |
| 14 | Claim-after-work, duplicate processing in the window | Mark-visited *after* the fetch/work | Duplicates under load | 2 |
| 15 | Silent worker death, no per-task catch; missing release on exception paths | A worker loop with no `try/catch(Throwable)`; an unlock or decrement not in `finally` | Throughput decays to zero, no errors logged | 5 |
| 16 | Flag-only shutdown, parked workers never re-read your flag | `running = false` with workers blocked in `take()` | Shutdown never completes | 5 |
| 17 | Pool-exhaustion starvation, waiting on futures inside a fixed pool | `future.get()` called from inside a task running on the same pool | Hang, all WAITING, no lock cycle | 5 |
| 18 | `sleep()` for coordination; busy-waiting; unlocked flag polled forever | `while (!flag) {}`, `sleep(100)` to "let it finish" | Hang with CPU pegged; or works in dev, hangs in prod | 1, 3 |
| 19 | Wall-clock arithmetic; trusting a timed wakeup without re-deriving | `currentTimeMillis()` in interval math; using elapsed time without re-checking the predicate | Bursts/stalls after NTP steps | 3 |
| 20 | Starvation shipped without comment | An unfair lock or reader-preference policy with no note | Tail latency; writer never runs | 4 |
| 21 | Over-engineering | Lock-free/striped/fine-grained code with no measurement in the commit message | Unmaintainable; often subtly wrong | all |

Two review-specific additions not on the refresher list, both real production killers: **unbounded queue hiding overload** (a `LinkedBlockingQueue` with no capacity in a pool, latency inflates then OOM), and **`ThreadLocal` leak on a pooled thread** (set and never removed; the value survives into the next unrelated task and pins its object graph). Both surface only in sweep 5.

---

## 8. Validation against all problems

Running §1's sweep order and §2's table against each problem in this family, to check that the method actually produces the known answer.

### lost-update-hunt
Symptom "totals drift under load, always low" → §2 row 1/2 → sweep 1 finds `balance`/`count` with an empty guard column; sweep 2 finds `x = x + 1`. Hypothesis stated as an interleaving (two reads of 100, two writes, one lost). Reproduction: start gate + N×iters + exact-value assertion; deterministic version = injected delay between read and write. Confirmation: the deficit scales with thread count, never overshoots. Fix ladder §6 yields exactly three: `AtomicLong`/`LongAdder` (F1), `synchronized` over the whole method (F2), per-thread accumulation merged at the end (F0). Catalog #1. **Verdict: fits; the method produces all three fixes and their trade-offs without memorization.** ✓

### the-hang-that-isnt-a-deadlock
Symptom "intermittent freeze, CPU 0, no deadlock section in the dump" → §2 row 3, and §3a's rule fires immediately: all WAITING, nothing BLOCKED ⇒ lost wakeup, not deadlock. Sweep 3 finds `if` instead of `while` and `notify` on a monitor with two kinds of waiter. Reproduction: mixed producers/consumers, skewed speeds, timeout so the hang *fails fast* rather than wedging. Fixes: `while` (catalog #4), `notifyAll` (#3), or two `Condition`s (F3). **Verdict: fits, and it is the problem that forces §3a's BLOCKED/WAITING distinction to be load-bearing rather than decorative.** ✓

### lock-order-inversion-review
Pure code-review: no symptom needed first, though "hang under concurrent transfers between the same two accounts" is given. Sweep 4 is the whole problem, list locks per path, find (A,B) vs (B,A), construct the cycle, check Coffman, apply global ordering with the two-sentence impossibility proof. Confirmation via the JVM's own deadlock detection in `jstack`. Catalog #6, with #7 (fake `tryLock` fix) as the planted wrong answer. **Verdict: fits; sweep 4 exists for this shape.** ✓

### visibility-bug-no-lock
Symptom "works in dev, hangs in prod / started after we added -server" → §2 rows 6 and 7 → sweep 1's guard column shows a flag with *no* guard, read in a loop. Diagnosis is happens-before: no edge between the writer's store and the reader's loads, so the JIT may hoist the read out of the loop entirely. Reproduction: long-running loop, `-XX:+PrintCompilation`, or ARM; confirmation: bug disappears under `-Xint`, which is itself the proof it's a reordering/hoisting bug. Fixes: `volatile` (F1), lock both sides (F2), interrupt instead of a flag (F3). jcstress is the tool answer. Catalog #5, #18. **Verdict: fits; this problem is why §3b lists `-Xint` and ARM as diagnostic instruments rather than trivia.** ✓

### check-then-act-on-concurrent-map
Symptom "duplicate work / lost entries even though we used ConcurrentHashMap" → sweep 2 plus the composition audit: each method is atomic, the *sequence* isn't. Adds the `computeIfAbsent` misuse axis, which sweep 4 catches as *alien code under a lock*, the mapping function runs while the bin is locked, so blocking work inside it stalls unrelated keys and a re-entrant map call on the same map can deadlock or corrupt. Fixes: `putIfAbsent`/`merge`/`compute` (F1), compute a value cheaply outside and only publish inside (F3). Catalog #1, #8, and #6's alien-call clause. **Verdict: fits, and it validates that the alien-call rule generalizes beyond user-written locks to library-internal ones.** ✓

### executor-misuse-review
Sweep 5 is the entire problem, run five times over one file: unbounded queue, swallowed `InterruptedException`, no per-task catch, `ThreadLocal` leak, missing `shutdown()`. Then §5's prioritisation rubric does the real grading work, the answer is not the list but the *ranking* (silent worker death and unbounded growth above cancellability above JVM-exit hygiene). Catalog #15, #16, #17, plus the two review-specific additions. **Verdict: fits; and it is the problem that justified adding the prioritisation rubric to §5, since finding five bugs unranked is a mid-level answer.** ✓

**Method fix applied during validation:** the first draft had four sweeps (state, check-then-act, wait/signal, lock order). `executor-misuse-review` and the `ThreadLocal`/`finally` bugs fell outside all four, nothing in the scan asked "what happens to this work when it throws, is cancelled, or the process is shutting down". Sweep 5 (lifecycle) was added, and catalog rows 13–17 re-keyed to it. With that, all six validate cleanly.

---

## 9. What the general framework leaves out

The 5-step framework (classify → invariant → pattern → template → verify) is a **construction** procedure. This family runs it backwards, and four things are simply absent:

1. **There is no "read someone else's code" step at all.** The framework starts from a problem statement; here you start from a symptom and an artifact. Sections 1 and 2 supply the missing front end, the scan order and the symptom→cause map, and neither has any counterpart in the framework.
2. **No diagnostic instrumentation.** The framework's verify step is a mental checklist. It never mentions thread dumps, the BLOCKED/WAITING distinction, deterministic reproduction by injected delay, `-Xint` as a reordering probe, jcstress, or `@GuardedBy`. A candidate who can only reason on paper stalls the moment the interviewer says "here's the jstack output."
3. **No prioritisation or blast-radius reasoning.** Building has one right answer; reviewing has a *ranked list*. The framework has no notion that a lost update outranks a missing `shutdown()`. §5's rubric supplies it.
4. **No theory of proof under uncertainty.** The framework says "verify aloud"; it doesn't say that a green stress test is not evidence, that the correctness argument is the deliverable, or how to convert a probabilistic failure into a deterministic one. §4 supplies it, and it is the single most transferable idea in this folder, it is what makes the other eight families' correctness arguments *matter* rather than being ceremony.

Minor: the framework's classification step assumes the code has one family. Real code under review is a *mixture*, the executor problem is Type F lifecycle, the map problem is Type B guarded state, the hang is Type C wait/signal, so the review skill is classifying **each defect** into its family and then reusing that family's catalog. That's a genuinely different mental motion from classifying a problem, and it is the reason this family sits last: it is the index over all the others.
