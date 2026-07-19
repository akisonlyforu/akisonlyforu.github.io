---
layout: post
title: What do you actually do in a Multithreading interview?
date: 2026-07-19
description: A repeatable five-step method for concurrency rounds, classify the family, state the invariant, pick the pattern, code from a template, then verify out loud.
categories: interview multithreading framework
---

Concurrency rounds look chaotic from the outside, forty-five minutes to produce code that is correct under every possible interleaving. They are not chaotic. Almost every question belongs to one of seven families, and once you name the family the design is largely mechanical. What is left, and what actually separates candidates, is stating a precise invariant and checking the failure paths out loud.

This is the method. Concepts it assumes are on the [101 page](/interview/multithreading/multithreading-101/).

## Step 0: Your toolbox (keep it SMALL)

Do not learn 30 APIs. Start with this small toolbox:

| Tool | Use for | One-liner |
|------|---------|-----------|
| `synchronized` + `wait/notifyAll` | mutual exclusion + waiting for a condition | baseline when asked to implement coordination manually |
| `Semaphore` | signaling, ordering, permits/capacity | `acquire` waits, `release` signals |
| `ReentrantLock` + `Condition` | when you need 2+ separate wait-queues (e.g. notFull/notEmpty) | fancier wait/notify |
| `BlockingQueue` + `ExecutorService` | task handoff, bounded work, worker lifecycle | prefer this to writing a worker pool |
| Atomics / `ConcurrentHashMap` | atomic single-variable or per-key operations | compound operations still need an atomic API or lock |

Use `CountDownLatch` for a one-shot gate and `CyclicBarrier` for a fixed reusable meeting point when those semantics match exactly. You should recognize other APIs, but you do not need them in your default coding toolbox.

---

## Step 1: Classify the problem (30 seconds)

| Type | Tell-tale phrasing | Examples |
|------|-------------------|----------|
| A. **Ordering / turn-taking** | "print in order", "alternately", "sequence" | LC 1114/1115/1116/1195, odd-even |
| B. **Mutual exclusion / shared state** | "thread-safe", "counter", "singleton", "cache" | singleton, thread-safe LRU |
| C. **Producer–Consumer / bounded resource** | "queue", "buffer", "capacity", "block until" | LC 1188, blocking queue, thread pool |
| D. **Grouping / batching / barrier** | "wait until N arrive", "form a group", "boat holds 4" | H2O, Uber Ride, river crossing, roller coaster |
| E. **Readers–Writers / asymmetric access** | "many can read, one can write" | RW lock, unisex bathroom |
| F. **Task execution / lifecycle** | "process jobs", "crawl", "cancel", "shut down", "know when done" | thread pool, crawler, fan-out/fan-in |

Say your classification out loud: *"This is a grouping problem, like Building H2O: threads must wait until a valid group forms, then proceed together."* This lets the interviewer correct a misunderstanding before you code.

Time-based designs such as a token bucket or delayed scheduler combine Type B (guarded state) with a clock. A semaphore caps **concurrency**; it does not by itself enforce a rate per second.

---

## Step 2: State the invariant BEFORE coding

One sentence: what must always/never be true?

- Bounded queue: "size never exceeds capacity; consumers never take from empty."
- H2O: "a molecule releases exactly 2 H and 1 O; no thread from molecule k+1 proceeds before molecule k finishes."
- Uber Ride: "a car seats exactly 4: (4 Dem) or (4 Rep) or (2+2)."
- RW lock: "writers are exclusive against everyone; readers only against writers."
- Executor: "every accepted task runs at most once, shutdown rejects new work, and queued/running work follows the stated cancellation policy."

Also name the **linearization point**: the single locked or atomic action at which the operation logically takes effect (for example, queue insertion or token deduction).

## Step 3: Pick the pattern (the 7 patterns from the Little Book of Semaphores)

| # | Pattern | Mechanic | Solves |
|---|---------|----------|--------|
| 1 | **Signaling** | B waits on sem; A releases after its work | print-in-order, any "X before Y" |
| 2 | **Rendezvous** | two signalings crossed | "both must arrive before either continues" |
| 3 | **Mutex** | Semaphore(1) or synchronized | shared counters/state |
| 4 | **Multiplex** | Semaphore(n) | "at most N concurrent": connection pool, worker cap, bathroom capacity |
| 5 | **Barrier (+ turnstile for reuse)** | count arrivals under mutex; last one opens the gate | H2O, Uber Ride, roller coaster |
| 6 | **Lightswitch** | first reader locks the room, last reader unlocks | readers-writers and all variants |
| 7 | **Leader–Follower queue** | two sems, each side signals the other and waits | pairing problems (dance partners, matchmaking) |

Type A → pattern 1/2. Type B → 3. Type C → 4 + condition waiting. Type D → 5 (+3 for the counter). Type E → 6.

Type F usually starts with a bounded `BlockingQueue` and `ExecutorService`; explicitly define completion, rejection, cancellation, and shutdown.

## Step 4: Code from a template

### Template 1: wait/notify condition loop (memorize this shape)
```java
private final Object lock = new Object;
private int state;                      // whatever the invariant tracks

void doWhenAllowed throws InterruptedException {
    synchronized (lock) {
        while (!conditionAllowsMe) {  // ALWAYS while, NEVER if
            lock.wait;
        }
        mutateState;
        lock.notifyAll;               // default to notifyAll
    }
}
```

### Template 2: semaphore signaling (ordering problems)
```java
Semaphore second = new Semaphore(0);    // 0 = "not yet allowed"
// thread A:  doFirst;  second.release;
// thread B:  second.acquire;  doSecond;
```

### Template 3: bounded blocking queue (Lock + 2 Conditions)
```java
final Lock lock = new ReentrantLock;
final Condition notFull  = lock.newCondition;
final Condition notEmpty = lock.newCondition;
final Queue<T> q = new ArrayDeque<>;

void put(T x) throws InterruptedException {
    lock.lock;
    try {
        while (q.size == capacity) notFull.await;
        q.add(x);
        notEmpty.signal;
    } finally { lock.unlock; }
}
// take is the mirror image
```

### Template 4: reusable fixed-composition group (H2O shape)
```java
final Semaphore hSlots = new Semaphore(2);
final Semaphore oSlots = new Semaphore(1);
final CyclicBarrier group = new CyclicBarrier(3,  -> {
    hSlots.release(2);
    oSlots.release(1);
});

void hydrogen(Runnable emitH) throws Exception {
    hSlots.acquire;
    emitH.run;
    group.await;
}
// oxygen mirrors this with oSlots.
```

The semaphores admit the required composition; the barrier prevents the next group from starting before this one completes. For Uber Ride, choose a valid composition while holding one lock, release exactly those rider permits, then rendezvous at a barrier. A bare `count + gate.release(N)` barrier is one-shot and is unsafe to reuse because a later arrival can steal a permit from the current generation.

This compact template assumes participants are not cancelled after taking a slot. Production code must define how a broken/interrupted barrier restores permits or cancels the whole group.

## Step 5: Verify out loud (2 minutes)

Run this checklist verbally:

1. **Race**: is every read-modify-write of shared state inside the lock?
2. **Deadlock**: can two threads hold locks and wait on each other? Do I ever `wait` while holding a second lock?
3. **Lost wakeup**: `while` around every wait? `notifyAll` (or the right Condition) after every state change?
4. **Starvation**: can one thread type wait forever? (writers behind endless readers?)
5. **Cancellation/failure**: what if a wait is interrupted or user code throws? Are locks and permits released in `finally`? Can peers wait forever?
6. **Lifecycle**: who owns the executor, how is it shut down, and how do callers learn about task failure?
7. **Walk one interleaving**: narrate one happy path, one contention path, and the linearization point.

---

## How to run the 45 minutes

- **0–5 min**: restate the problem, ask: How many threads? Called once or repeatedly (reusable barrier?)? Must waiting block or fail fast? What are timeout, interruption, fairness, and shutdown semantics?
- **5–10 min**: classify (Step 1), state invariant (Step 2), name pattern (Step 3). Get interviewer nod BEFORE coding.
- **10–35 min**: code from the template. Narrate. Start with the simplest correct tool (`synchronized`/Semaphore) and optimize only if the requirements demand it.
- **35–43 min**: verification checklist out loud.
- **Follow-ups they'll ask** (prepare answers): "make it fair", "make readers not starve writers", "what if N producers", "how does cancellation work", "how would you test it" (answer: a `CountDownLatch` start gate, timeouts, repeated contention, invariant assertions; jcstress is optional extra credit).

## The anti-over-engineering rules

1. Never introduce a primitive you can't fully explain.
2. `notifyAll` > `notify` unless you can prove notify is safe.
3. One lock is better than two until proven otherwise.
4. Don't optimize for throughput unless asked: correctness first, say so explicitly.
5. If stuck: go back to the invariant and ask "who is allowed to proceed, and who tells them?" Every concurrency problem is just that question.
6. Never use `Thread.sleep` to coordinate threads or to prove a test is correct.
7. Do not hand-write a queue, pool, latch, or barrier in a design answer when a standard Java utility already matches, unless implementation is the question.
