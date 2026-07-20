---
layout: post
title: Lock-Free Stack (Treiber) and the ABA Problem
date: 2026-07-19
description: >-
  It is the smallest complete lock-free algorithm, small enough to fit in a whiteboard, complete enough that every concept in non-blocking programming shows up: the CAS retry…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** The standard vehicle for "have you actually written a CAS loop?", usually asked as a small coding exercise followed by heavy conceptual probing on ABA and memory reclamation. Common in low-level/infra-flavoured senior loops; frequency claim is directional.

### Problem

Implement a thread-safe LIFO stack with `push` and `pop`, without using any lock. Then defend it: explain what makes the operation atomic, identify the linearization point of each operation, and analyse whether the ABA problem can occur, and if it can't, say precisely what is preventing it.

Follow-ups that reliably arrive: what changes if this were C++ instead of Java; and when would you *not* use this, preferring a plain mutex.

### Constraints

- No locks, no `synchronized`, no blocking. Only atomic compare-and-set on a single reference.
- `pop` on an empty stack must have a defined, non-blocking outcome (return an empty result, decide and state the policy).
- Correct under arbitrary interleavings and arbitrary preemption: a thread suspended mid-operation must not prevent any other thread from completing.

### Clarify before solving

- **Is the ask lock-free specifically, or just thread-safe?** If the latter, the correct opening move is a `synchronized` stack plus the sentence about why. Volunteering a CAS loop unasked is the over-engineering tell.
- **What should `pop` on empty do, return empty, throw, or block?** Blocking is a different problem entirely (bounded-resource family); a lock-free structure cannot block by construction.
- **Do we need a `size()`, and how exact?** Foreshadows that an exact count is a second, global invariant that a single CAS cannot cover alongside the head pointer.
- **Are nodes pooled or reused for allocation reasons?** This is the question that decides whether ABA is live or dormant. Ask it explicitly, it's the strongest signal in the problem.
- **Contention level?** High contention is the case where the lock-free version can lose to a mutex, and knowing that is the point of the last follow-up.

### Why this problem matters

It is the smallest complete lock-free algorithm, small enough to fit in a whiteboard, complete enough that every concept in non-blocking programming shows up: the CAS retry loop as a hardware-atomic check-then-act, the "compute the new state as a pure function of what you read" discipline, the linearization point being a single instruction you can point at, and the happens-before edge that a successful CAS carries.

More importantly, it is the standard test of whether a candidate understands ABA *mechanically* or has only heard of it. The graded answer walks a concrete interleaving where a CAS succeeds and the structure still breaks, and then, critically, explains why a JVM implementation of this exact algorithm is usually safe anyway, and what invisible service the runtime is performing. Candidates who can only recite "ABA is when a value changes to B and back to A" cannot answer the follow-up about C++, because they never understood that ABA and memory reclamation are two faces of the same missing guarantee.

Finally, it is a chance to demonstrate restraint. Lock-free is a claim about *non-blocking progress*, not a claim about speed. A candidate who says "lock-free is faster" without qualification has failed a judgment test embedded in a coding question.

---

## Strategy

### Classify

Concurrent data structure whose entire invariant fits in **one word**. That is the rare precondition under which lock-free is not an escalation but the natural design, and recognizing the precondition is more valuable than memorizing the algorithm.

### Invariant

The stack is exactly the chain reachable from the head pointer; every pushed element appears exactly once until popped, and never after; the order of removal is the reverse of the order of insertion (by linearization order, not by wall-clock arrival). No element is lost, duplicated, or resurrected.

The crucial structural fact: **the entire mutable state is one pointer.** Nodes, once linked, are never mutated (their `next` is set before publication and never changes while reachable). Everything else is derived from following that pointer. When the whole invariant fits in a single atomic variable, a single CAS can be the whole critical section.

### Mental model

A single hook on a wall with a chain of links hanging from it. To push, you build a new link, hook its top onto whatever is currently on the wall-hook, and then, in one indivisible motion, move the wall-hook to your new link, *but only if nobody moved it since you looked*. To pop, you look at what's on the hook, note what's below it, and move the hook down one, again only if nothing changed.

"Only if nothing changed" is what the hardware sells you. And the entire ABA discussion is the discovery that the hardware can only check *what is hanging there now*, not *whether anything happened in between*.

### Why the CAS loop is the cure for check-then-act

Read the head into a local, compute the new head as a pure function of what you read, then compare-and-set. The CAS is simultaneously:
- **the check**: "head is still the value I read," and
- **the act**: "install my new value,"

executed as one indivisible instruction. That's exactly the guarded-state cure (make the check and the act atomic) delivered by the CPU instead of by a monitor. When it fails, someone else changed the head, meaning **someone made progress**; you discard your speculative work and re-read. A failed CAS is at minimum a volatile read, so the retry always sees fresh state, you can never spin on a stale local.

**Discipline that makes it correct:** the new state must be a pure function of the values you read in *this* iteration. Any state carried across iterations, or any side effect performed before the CAS succeeds, is a bug, the iteration may be discarded. Allocate the node before the loop if you like (allocation is idempotent), but set its `next` inside the loop, from the freshly-read head.

**Linearization points:** for `push`, the successful CAS that installs the new node. For `pop`, the successful CAS that advances the head, the element is removed at that instant, and the read of the item can happen after, because the node is already unreachable to everyone else. For a `pop` that observes an empty stack, the read of the null head. Being able to name all three is the compact proof of atomicity.

### Publication and the memory model

A pushed node's fields must be written **before** the CAS that makes it reachable. The successful CAS acts as a volatile write; a subsequent reader of the head performs a volatile read; the happens-before edge carries everything the pusher did before the CAS, including the node's field writes (the piggyback rule). Reverse the order, publish then initialize, and you have the same disease as double-checked locking without volatile: another thread reaches a node whose payload isn't there yet.

Making the item and `next` fields **final** where the algorithm allows it upgrades this from "correct by argument" to "correct by the final-field guarantee," which survives even sloppy publication. Where `next` must be assignable, it must be volatile or written through an atomic.

### ABA, mechanically

The CAS compares a *value*. You wanted it to compare *history*. Those coincide only when a value cannot be removed and later restored.

Walk the interleaving concretely, this narration is the deliverable:

Start with head → A → B → C.
1. Thread 1 begins a `pop`: reads head = A, reads A's next = B. It is now holding the pair (A, B) and is about to CAS head from A to B. It gets preempted here.
2. Thread 2 pops A. Head → B → C.
3. Thread 2 pops B. Head → C. Node B is now detached and, in a system that reuses nodes, returned to a pool or freed.
4. Thread 2 pushes A back (or pushes a new element that reuses A's memory). Head → A → C. Note A's next is now C.
5. Thread 1 resumes and executes CAS(head, expected = A, new = B). **Head is A. The CAS succeeds.**
6. Head → B. But B was popped in step 3. It is detached, possibly recycled, possibly holding an unrelated element, and its next pointer still points at the old C, or at garbage.

The stack is now corrupt: a removed node is back at the top, the element C may be duplicated or lost, and Thread 1 returned A's item which Thread 2 already returned. **The CAS reported success and the invariant broke.** That is ABA in one sentence: *a successful CAS proves the value is unchanged, not that the world is unchanged.*

### The three cures, in order of practical relevance

1. **Don't reuse nodes.** Allocate a fresh node per push. If every node object is used exactly once, "the same value reappears" cannot happen, a new node is a new identity. This is the default in a garbage-collected runtime and it is the reason the JVM version of this algorithm is usually safe.
2. **Version stamps.** CAS the pair (pointer, counter) as one unit, bumping the counter on every modification (`AtomicStampedReference` in Java; a double-width CAS or a pointer with tag bits packed into a 64-bit word in native code). Since the counter never repeats in practice, the CAS now compares history rather than value. Use this whenever nodes *are* pooled, or whenever you are CASing a value from a small domain (a state enum, a small integer) where recurrence is likely.
3. **CAS a whole immutable snapshot.** If each update installs a freshly allocated immutable object representing the entire state, there is no reused identity to confuse. Costs allocation per update; buys multi-field atomicity as a bonus.

### Why the JVM hides this, and why C++ can't

The deeper point, and the one the follow-up is fishing for. Thread 1 in step 1 held a reference to node A. On the JVM, that reference is a **GC root**: A cannot be collected, cannot be reallocated, and therefore cannot come back as a *different object at the same address*. The garbage collector is performing safe memory reclamation on your behalf, and safe memory reclamation is precisely what a lock-free algorithm needs and cannot easily get.

In C++ the same code has a worse problem than ABA. In step 3, thread 2 pops B and `delete`s it. Thread 1 still holds a raw pointer to A and will dereference A's next, but in the mirror scenario, thread 1's own held node is freed under it, and dereferencing it is a **use-after-free**, undefined behaviour, not merely a logical corruption. You cannot even safely *read* a node you found in a lock-free structure unless something guarantees it stays alive.

Hence the reclamation schemes, which are worth naming at awareness level:
- **Hazard pointers**: each thread publishes, in a globally visible slot, the nodes it is currently dereferencing. A thread that unlinks a node puts it on a retired list and only frees it once no hazard pointer references it. Precise, bounded memory, per-access cost.
- **Epoch-based reclamation**: threads announce the epoch they're operating in; a retired node is freed only once every thread has advanced past the epoch in which it was retired. Cheaper per access, but a stalled thread pins memory indefinitely.
- **Reference counting**: simple, correct, and usually too slow on the hot path because the count itself becomes contended.

The one-line takeaway to deliver: *garbage collection is a memory reclamation scheme, and every lock-free structure needs one; the JVM gives it to you for free, which is why lock-free algorithms are far easier to get right in Java than in C++.* A candidate who says this has demonstrably understood ABA rather than memorized it.

**Residual JVM hazard, so you don't overclaim:** GC protects you from *address* reuse, not from *value* reuse. CAS on a small-domain value, a state field cycling through a handful of constants, a monotonically-nothing integer, can still hit genuine ABA on the JVM. The rule is not "Java has no ABA"; it is "Java has no ABA when you CAS references to freshly allocated objects."

### When lock-free actually wins: and when it loses

Be precise about the claim. Lock-free means **non-blocking progress**: the system as a whole always advances, because a failed CAS implies someone else succeeded. A suspended, descheduled, page-faulted, or crashed thread cannot freeze the structure. That is a *robustness* property, and it's the honest reason to want it.

The progress ladder, worth naming: **wait-free** (every thread finishes in bounded steps) > **lock-free** (some thread always progresses) > **obstruction-free** (progress if eventually running alone) > **blocking** (any mutex, one suspended holder stops everyone).

Wins when:
- A thread could be preempted or delayed inside the critical section and that is intolerable, latency tails, signal handlers, real-time paths, code that may run at odd priorities.
- The operation genuinely is a single pointer swing, so retries are cheap.
- Contention is low to moderate: uncontended CAS is very fast, and retries are rare.

Loses when:
- Contention is high. Every loser re-reads and recomputes and retries, burning CPU on work that will be thrown away. A mutex, by contrast, parks the losers, they consume nothing while waiting. Under heavy contention a `synchronized` stack can beat a Treiber stack outright.
- The operation is long or has side effects. You cannot roll back an I/O call when the CAS fails.
- The invariant doesn't fit in one word. Then you're either CASing a whole snapshot (allocation per update) or you're not really doing this.
- Anyone has to maintain it. The verification burden is the real cost, and it is not visible in a benchmark.

**And note the shared bottleneck:** a Treiber stack has *one* head pointer, so every operation from every thread contends on the same cache line. It is non-blocking, but it does not scale, throughput can flatten or fall with thread count. If scaling is the goal rather than non-blocking progress, the answer is elimination backoff (pair a blocked pusher with a blocked popper and let them exchange directly, bypassing the stack entirely) or a different structure altogether. Naming this distinction, non-blocking ≠ scalable, is a strong senior signal.

### Pitfalls

1. Producing a CAS loop unprompted for a "make this thread-safe" question. Start with a lock; escalate on a stated reason.
2. Reading the head once outside the loop and reusing it across retries. The re-read must be inside.
3. Setting the new node's `next` before the loop and not refreshing it after a failed CAS, the classic silent corruption.
4. Publishing the node before writing its fields.
5. Performing a side effect (logging, counting, handing the value to a consumer) before the CAS succeeds.
6. Adding an `AtomicInteger size` alongside the head. Two atomics, one invariant, they can be observed inconsistently, and there is no CAS covering both. If a count is required, either fold it into a CASed immutable snapshot or accept that it's an estimate.
7. Claiming ABA is impossible in Java, full stop. True only for freshly allocated reference CAS.
8. Claiming lock-free is faster. It is non-blocking; speed depends on contention and is frequently worse.
9. Ignoring the empty case, or returning a sentinel that a valid element could equal.
10. Unbounded retry with no backoff on a hot structure, livelock-adjacent CPU burn even though the algorithm is technically progressing.

### Check your understanding

1. Why is a CAS a legitimate cure for check-then-act when a volatile read is not?
2. Name the linearization point of `push`, of a successful `pop`, and of a `pop` that finds the stack empty.
3. Narrate the ABA interleaving from memory, and state exactly which invariant clause breaks at the end.
4. Your `pop` succeeds via CAS. Which happens-before edge lets you safely read the popped node's item? Which edge made the item visible in the first place?
5. Someone adds node pooling to reduce allocation pressure. What breaks, and what are your two options?
6. Explain to a C++ engineer why their port of your Java stack has a bug that yours doesn't. Then explain how hazard pointers fix it.
7. Under what measured conditions would you replace this lock-free stack with a `synchronized` one? Give the reasoning, not just the threshold.
8. The stack is non-blocking but throughput drops as you add threads. Explain why, and name one design that addresses it.

### Transfers to

`lock-free-or-bounded-queue` (the same CAS discipline, but with two ends and therefore a genuinely harder multi-step update); `striped-counter-longadder` (which is what you do when the single CAS target becomes the bottleneck, this problem's scaling limitation is that problem's premise); the CAS-on-immutable-snapshot idiom used for multi-field state anywhere; the lazy-derivation designs in the time-based family, where a compare-and-set on a (value, timestamp) pair replaces a lock; and any interview conversation about optimistic concurrency control, of which this is the in-memory instance.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/concurrent-data-structures/lock-free-stack-treiber).
