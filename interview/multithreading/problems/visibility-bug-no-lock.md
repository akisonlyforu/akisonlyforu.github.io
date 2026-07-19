---
layout: post
title: Visibility Bug, No Lock In Sight, "works in dev, hangs in prod"
date: 2026-07-19
description: >-
  Every other bug in this folder is a *race on state*. This one is a race on visibility: the code is logically correct, there is exactly one writer and one reader per field…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** AWS L6 and Rubrik debugging rounds; the archetype of the class of bug that a code reviewer cannot find by looking for missing locks, because **there is no shared *mutation* to serialize, only a missing happens-before edge.**

### The code under review (described, not shown)

Two related pieces of a background indexing service.

**Piece 1, the poller.** An `IndexWorker` implements `Runnable`. It has:

- A plain `boolean` field `running`, initialized to `true`.
- A `shutdown()` method, called by the main thread, which sets `running = false`.
- A `run()` method whose body is a loop: `while (running)` { pull the next batch from an in-memory array, compute a checksum over it (pure CPU, no I/O, no synchronization, no logging), accumulate the result }. When the loop exits, it writes a summary and returns.

There is no `volatile`, no lock, and no interruption anywhere in `IndexWorker`.

**Piece 2, the published config.** A `ConfigHolder` with:

- A plain (non-`volatile`) `IndexConfig` field `current`, initially `null`.
- `reload()`, called by a config-watcher thread: constructs a new `IndexConfig` (whose constructor reads a file and sets several non-final fields, `shardCount`, `analyzerName`, a `Map<String,String>` of options) and assigns it to `current`.
- `get()`, called by every worker thread on every batch: returns `current`, and callers immediately dereference `shardCount` and the options map.

Additionally, `IndexConfig`'s constructor registers itself with a global `ConfigRegistry` on its last line, so the registry can enumerate live configs.

### The observed symptom

Two separate incident reports, filed months apart, that the team never connected:

**Report A, the worker won't stop.**
- In local development and in the integration test suite, `shutdown()` works every time; the worker exits within milliseconds.
- In production, `shutdown()` sometimes never takes effect. The worker keeps spinning. CPU for that thread sits at **100%** indefinitely.
- `jstack` shows the thread in state **RUNNABLE**, in the checksum loop, in all three dumps ten seconds apart. No `BLOCKED`, no `WAITING`, no deadlock section.
- It started happening after a deploy that changed nothing in this class. The deploy note says: *"moved to larger instances, enabled tiered compilation defaults."* Someone else observed that it also began appearing in CI after CI moved to ARM build agents.
- Running the same binary with `-Xint` makes the problem vanish entirely.

**Report B, impossible NPEs.**
- Rare `NullPointerException`s dereferencing the options map inside `IndexConfig`, on a config object that was definitely fully constructed, the constructor cannot complete without setting it, and the stack trace shows a reader, not the constructor.
- Even rarer: a reader saw a `shardCount` of `0` on a config whose file specified 8.
- Both only in production, never reproducible, roughly one in ten million reads.

### Your task

1. For each report, state the defect in terms of **happens-before**, not in terms of "missing `synchronized`".
2. Explain every environmental clue: why dev and CI-on-x86 pass, why bigger instances and JIT warmup matter, why ARM exposes it, and why `-Xint` makes it disappear.
3. Say precisely what the JIT is permitted to do to the polling loop, and why that is legal.
4. Identify the third defect in `IndexConfig` that is a bug even if you fix the field's visibility.
5. Give the fixes and prove them with a happens-before chain.
6. Say what tool you would use to demonstrate that the fix is *required*, not merely sufficient.

### Clarify before diagnosing

- Does the loop body ever perform I/O, logging, or take any lock? (No, and that is load-bearing. Any of those would incidentally introduce barriers and mask the bug, which is exactly why dev passes.)
- How long does the loop run between iterations? (A short pure-CPU body is the worst case: nothing forces a re-read.)
- Is `IndexConfig` deeply immutable, or does anyone mutate it after publication? (Decides whether `volatile` alone is enough or the object needs redesigning.)
- What are the actual production CPU architectures? (x86 has a much stronger memory model than ARM; a fleet spanning both will produce exactly this "one machine class only" report.)

### Why this problem matters

Every other bug in this folder is a *race on state*. This one is a race on **visibility**: the code is logically correct, there is exactly one writer and one reader per field, and no interleaving of the *statements* produces a wrong answer. It is broken purely because the Java Memory Model gives you no guarantees at all between two threads with no happens-before edge, so the compiler may hoist the read out of the loop, the CPU may serve a stale cache line indefinitely, and the constructor's stores may become visible after the reference does. It is the bug that teaches why "I don't need a lock, only one thread writes it" is wrong, and it is the one that reliably separates candidates who have memorized `volatile` from candidates who can derive it.

---

## Strategy

### Classify

Guarded state (Type B), publication/JMM section. Two defects from catalog #5 (unsafe publication; unlocked reads of guarded state) and #18 (unlocked flag polled forever). Sweep 1 finds both: enumerate every field a second thread touches and write down **what guards it**. `running`, nothing. `current`, nothing. Two entries in the "nothing" column, two incident reports. The sweep-1 guard table is not a formality; on this problem it *is* the diagnosis.

### The invariant being broken

Not a data invariant, a **memory-visibility** one: *every write performed by one thread is visible to a thread that later reads it.* Java grants this only across a happens-before edge. With no edge, the JMM's answer to "will the reader see the write?" is not "eventually", it is **no guarantee whatsoever**, forever, plus permission to see writes out of order. Two conflicting accesses (at least one a write) with no HB path between them is the definition of a **data race**, and a racy program gets nothing.

### Report A: the stop flag

### Symptom → hypothesis

The dump does the work. `RUNNABLE`, same stack, three dumps, CPU 100%. That eliminates the entire hang-by-waiting hypothesis space in one look, nothing is BLOCKED (no contention, no lock cycle) and nothing is WAITING (no lost wakeup). A thread burning CPU in a loop that should have exited is either (a) the exit condition is genuinely still false, or (b) **the thread cannot see that it became true**. Since `shutdown()` demonstrably ran, it's (b).

### What is actually permitted to happen

Two mechanisms, and you should name both because interviewers probe for whether you think this is "just caching":

1. **Compiler hoisting.** The field is non-volatile and the loop body contains no synchronization, no volatile access, no I/O and no call the JIT can't inline and prove effect-free. Therefore, *as far as the JIT's single-thread-correctness obligation is concerned*, `running` cannot change during the loop, nothing in this thread writes it. So the JIT is fully entitled to hoist the load out of the loop, read it once into a register, and compile the whole thing into either an infinite loop or a no-op. This is not a bug in the JIT: within a single thread's semantics, the transformation is invisible, and the JMM only forbids transformations that are visible across a *properly synchronized* boundary. There is no such boundary here.
2. **Store buffering / cache coherence delay at the hardware level.** The writer's store may sit in a store buffer; the reader's core may keep serving a cached line. On x86's strong (TSO) model this typically resolves in nanoseconds, which is why x86 dev boxes appear to work. On ARM's weaker model the store may remain unobserved far longer.

The compiler mechanism is the important one, because it explains **permanence**: cache lines eventually reconcile, but a value hoisted into a register is never re-read. A bug that lasts forever is a compiler bug, not a cache bug.

### Every environmental clue explained

- **Passes in dev / integration tests.** Short runs; the loop may exit before the JIT ever promotes the method to compiled code. In the interpreter, every iteration re-loads the field from memory.
- **`-Xint` makes it vanish.** Interpreter only, no hoisting. **This is not just a workaround, it is a diagnosis:** if a bug disappears under `-Xint` you are looking at a JIT-visible reordering or hoisting problem, i.e. a missing happens-before edge. Say that sentence; it is the sharpest single confirmation technique in the problem.
- **Started after moving to larger instances / JIT changes.** More cores means the writer and reader genuinely run on different physical cores (on a small box they may share one, and a context switch is an implicit barrier). Longer-lived, hotter loops reach C2 compilation and get optimized properly. The bug did not appear with the deploy, **it was always there and the deploy merely stopped hiding it.** Deliver that line explicitly; blaming the deploy is the wrong instinct this problem is built to catch.
- **Appeared in CI after ARM agents.** Weaker memory model surfaces visibility bugs that x86 hides. This is now a common real-world report as fleets move to Graviton and Apple silicon.
- **CPU at 100%.** Distinguishes this from every wait-based hang, and also from a livelock (whose stack would cycle rather than sit still).

### Reproduce deterministically

A tight loop over a non-volatile boolean, started with a **start gate**; main thread sleeps a second, sets the flag, then joins **with a timeout** so the hang fails the test in seconds. Run with `-server` and enough iterations to guarantee C2 compilation (add `-XX:+PrintCompilation` and confirm the method compiled before the flag flip). On most JVMs this reproduces on the first attempt. On x86, if it doesn't, extend the run so the JIT recompiles, and try an ARM machine. The negative control is the confirmation: the identical program with `-Xint` always terminates, and the identical program with `volatile` always terminates.

### The fixes

**Fix A, make `running` `volatile`.** Adds happens-before edge: the volatile write happens-before every subsequent volatile read. The JIT can no longer hoist the load (a volatile read must be re-executed), and the hardware barrier forces the store to be visible. One keyword, correct, essentially free here, a volatile read of an uncontended field is a plain load plus a compiler barrier on x86.

**Fix B, read and write it under a lock.** The monitor edge (unlock of M happens-before subsequent lock of M) supplies the same guarantee. Correct but heavier and, in a hot loop, worse, and it only works if **both** sides lock. "The writer takes the lock, the reader doesn't" is the same bug: a one-sided lock creates no edge.

**Fix C, don't use a flag at all: use interruption (the redesign, F3).** `Thread.interrupt()` from the shutdown path, and the loop polls `Thread.currentThread().isInterrupted()`. Interruption's flag is maintained by the JVM with the right semantics, it also unblocks the thread if it ever starts blocking (a plain flag cannot, parked threads never re-read your flag, catalog #16), and it is the standard cooperative-cancellation vocabulary. **This is what to ship for a worker's stop signal**; the `volatile` flag is the correct minimal patch, interruption is the correct design.

### Prove the fix

The happens-before chain, stated explicitly: *the main thread's write to the volatile `running` happens-before the worker's subsequent read of `running`; the read occurs on every loop iteration because a volatile read may not be hoisted; therefore the worker observes `false` on some iteration and exits.* That is the proof. The stress test terminating is the smoke check.

### Report B: the published config

### Symptom → hypothesis

An NPE on a field the constructor cannot leave unset, seen by a reader, one in ten million, prod only. There is only one mechanism that produces that: **the reference became visible before the object's contents did.** Two distinct defects cause it here, and you must separate them.

**Defect 1, unsafe publication of `current`.** `reload()` performs constructor field writes and then a reference assignment. Nothing orders those two for a reader that takes no lock and reads no volatile. The mental model: publishing is mailing an envelope, without a barrier the envelope (the reference) can be mailed before the letter (the field stores) is inside. The reader opens it and finds `null` options and `shardCount` of `0` (the default field values, which is exactly what the incident reports describe, note that the *specific* observed values, null and zero, confirm the diagnosis: they are default values, not garbage). Fix: make `current` **`volatile`**, which orders the constructor's writes before the reference publication via the piggyback rule, everything the writer did before the volatile write is visible to anyone who reads the volatile.

**Defect 2, `this` escapes the constructor.** `IndexConfig`'s constructor registers itself with a global `ConfigRegistry` **on its last line**. "Last line" feels safe and is not: the reference is published to a globally reachable structure from inside the constructor, so another thread can obtain and dereference the object at a point where the JMM offers no guarantee about its fields at all, and, if the class were ever subclassed, at a point where the subclass constructor has not run. Making `current` volatile does *not* fix this path, because this path doesn't go through `current`. Fix: remove the self-registration from the constructor; construct fully, then register from the factory/caller after the constructor returns.

**Defect 3, the object is mutable and shallowly published.** The fields are non-final and it holds a `Map`. Fix properly by making `IndexConfig` **deeply immutable**: all fields `final`, the map wrapped unmodifiable (better, copied then wrapped), no setters. Then the final-field guarantee applies, an object constructed without leaking `this` has its final fields, and everything transitively reachable from them at freeze time, visible fully-initialized to any thread that obtains the reference, *by any route, with no synchronization at all*. Note the precondition it depends on: **no `this` escape**, which is precisely why defect 2 must be fixed for defect 3's guarantee to hold. The two are coupled and saying so is the depth marker.

### Prove the fix

Two chains, and say which one you're relying on:

- **Via volatile:** constructor field writes happen-before the volatile write of `current`, which happens-before a reader's volatile read of `current`, which happens-before that reader's dereferences. Transitivity closes it.
- **Via final fields:** the object never leaked during construction; its final fields are frozen at the end of the constructor; any thread obtaining the reference sees them fully initialized. This holds even for a plain field handoff, which is what makes immutable objects publishable by any means and is the reason it's the preferred fix.

Belt and braces (volatile field *and* deep immutability) is the right production answer here: immutability protects readers who get the reference by other routes, volatile guarantees they see the *newest* config rather than an arbitrarily stale one.

### Confirm, and the tool that proves the fix is *required*

- `-Xint` making report A vanish confirms it is a JIT-visible reordering/hoisting bug.
- `-XX:+PrintCompilation` shows the method reaching C2 right before the hang begins.
- Reproducing on ARM but not x86 confirms a memory-model dependency.
- **jcstress** is the answer to part 6. It runs tiny two-thread snippets millions of times under aggressive JIT settings and enumerates the **observed result set**, labelling outcomes as acceptable, interesting or forbidden. Point it at "writer sets fields then publishes; reader reads reference then fields" and it will report the `(null, 0)` outcome as actually observed on the plain-field version and absent on the volatile/final version. That is machine-generated evidence that the barrier is load-bearing, far stronger than "it stopped failing".
- Institutionally: `@GuardedBy` annotations plus ErrorProne, and a review rule that any field read by a thread other than its writer must appear in the guard table with a non-empty entry.

### Pitfalls

1. **"It's a caching problem, it'll resolve eventually."** No. Compiler hoisting makes it permanent, and the JMM promises nothing even in the limit. Eventual visibility is a folk belief, not a specification.
2. **Blaming the deploy.** The bug predates it. The deploy removed the accident that was hiding it.
3. **Synchronizing only the writer.** A lock creates an edge only between threads that *both* take it.
4. **`volatile` on the reference and stopping there.** It does nothing for the `this`-escape path or for later mutation of the config object.
5. **Thinking `volatile` gives atomicity.** It gives freshness and ordering only. It would not have fixed `lost-update-hunt`, and this problem exists partly to make you feel that boundary from the other side.
6. **`sleep()` in the poll loop as "the fix."** It reduces CPU and hides the bug behind a barrier-ish scheduling artifact; the field is still unsynchronized and there is still no edge. Also, a sleeping thread is a slow-to-stop thread.
7. **Ignoring that a plain flag can't stop a *blocked* worker.** Even correctly `volatile`, a flag never wakes a thread parked in a blocking call. That's catalog #16 and the reason fix C exists.
8. **Missing the final-field precondition.** The guarantee requires no `this` escape; quoting the guarantee without the precondition is a half-answer.

### Check your understanding

1. Why is `RUNNABLE` in three consecutive dumps a *complete* refutation of both deadlock and lost wakeup?
2. Explain precisely why the JIT is allowed to hoist the flag read out of the loop, in terms of what the JMM obliges it to preserve.
3. Why does `-Xint` fix it, and what does that tell you as a diagnosis rather than a workaround?
4. Why is a fleet spanning x86 and ARM likely to produce a "only on some hosts" report for the same binary?
5. The reader saw `null` and `0`. Why are those exact values a confirmation rather than a coincidence?
6. Write the happens-before chain that makes the volatile publication fix correct.
7. State the final-field guarantee including its precondition. Which defect in this code violates the precondition?
8. Why is interruption a better stop signal than even a correct `volatile` flag?
9. What would jcstress tell you that your stress test cannot?

### Transfers to

`thread-safe-singleton` (double-checked locking without `volatile` is the same publication bug in its most famous costume), every "config hot-reload", "feature flag", "cached immutable snapshot" and "stop the worker" pattern in production code, and the `task-lifecycle` family's shutdown discussion, where catalog #16 (a flag can't wake a parked thread) picks up exactly where this problem's fix C leaves off.
