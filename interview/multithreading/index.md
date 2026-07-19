---
layout: default
title: Multithreading
---

# Multithreading

Concurrency, parallelism, and thread-safe programming, as they actually get asked in senior interviews.

Everything here answers one question wearing different costumes: **who is allowed to proceed, and who tells them?** Start with the 101 to get the vocabulary, learn the framework to get a repeatable method, then work the ten pattern families. The problems under each pattern are there to be *solved*, not read.

## Start here

- [Multithreading 101: everything you must know](/interview/multithreading/multithreading-101). The concepts, the JMM, the primitives, the failure catalog. One page, no problems, all vocabulary.
- [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework). Classify → invariant → pattern → template → verify, in 45 minutes.

## Patterns

Ten families cover every concurrency question I've seen asked. Each playbook has the mechanics, the derivation recipe, and the failure modes; each problem below it is a worked application.

Families 1 to 7 are the coordination core, learn these first and in order. Families 8 to 10 are the senior material: what correctness *costs* at scale, whether you can find someone else's bug, and whether your instincts survive the process boundary.

### The coordination core

- [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering). The baton: who acts next, who hands it over
  - **Problems**
    - [Print in Order](/interview/multithreading/problems/print-in-order)
    - [Print FooBar Alternately](/interview/multithreading/problems/print-foobar-alternately)
    - [Print Zero Even Odd](/interview/multithreading/problems/print-zero-even-odd)
    - [Odd-Even Printer](/interview/multithreading/problems/odd-even-printer)
    - [FizzBuzz Multithreaded](/interview/multithreading/problems/fizzbuzz-multithreaded)
    - [N Threads Round-Robin](/interview/multithreading/problems/n-threads-round-robin)
- [Guarded State & Mutual Exclusion](/interview/multithreading/patterns/guarded-state). One invariant, one lock, and the check-then-act disease
  - **Problems**
    - [Thread-Safe Singleton](/interview/multithreading/problems/thread-safe-singleton)
    - [Dining Philosophers](/interview/multithreading/problems/dining-philosophers)
    - [Make a Class Thread-Safe](/interview/multithreading/problems/make-a-class-thread-safe)
    - [Traffic Light Intersection](/interview/multithreading/problems/traffic-light-intersection)
    - [Circuit Breaker](/interview/multithreading/problems/circuit-breaker)
- [Bounded Resource & Producer-Consumer](/interview/multithreading/patterns/bounded-resource). Two parties waiting on opposite predicates
  - **Problems**
    - [Bounded Blocking Queue](/interview/multithreading/problems/bounded-blocking-queue)
    - [Thread Pool from Scratch](/interview/multithreading/problems/thread-pool-from-scratch)
    - [Connection Pool](/interview/multithreading/problems/connection-pool)
    - [Bulkhead Isolation](/interview/multithreading/problems/bulkhead-isolation)
    - [Implement a Semaphore](/interview/multithreading/problems/implement-semaphore)
    - [Implement CountDownLatch or CyclicBarrier](/interview/multithreading/problems/implement-latch-or-barrier)
    - [Dining Savages](/interview/multithreading/problems/dining-savages)
    - [The Barbershop](/interview/multithreading/problems/barbershop)
- [Group Formation & Barriers](/interview/multithreading/patterns/group-formation). Admission and boundary, kept separate
  - **Problems**
    - [Reusable Barrier](/interview/multithreading/problems/reusable-barrier)
    - [Building H2O](/interview/multithreading/problems/building-h2o)
    - [The Uber Ride Problem](/interview/multithreading/problems/uber-ride-problem)
    - [River Crossing](/interview/multithreading/problems/river-crossing)
    - [Roller Coaster](/interview/multithreading/problems/roller-coaster)
- [Asymmetric Access & Readers-Writers](/interview/multithreading/patterns/asymmetric-access). The lightswitch, and choosing who starves
  - **Problems**
    - [Reader-Writer Lock](/interview/multithreading/problems/reader-writer-lock)
- [Task Lifecycle, Async & Parallelism](/interview/multithreading/patterns/task-lifecycle). Where is the work, and who is counting it
  - **Problems**
    - [Multithreaded Web Crawler](/interview/multithreading/problems/web-crawler-multithreaded)
    - [DAG Task Scheduler](/interview/multithreading/problems/dag-task-scheduler)
    - [Event Bus with Per-Key Ordering](/interview/multithreading/problems/event-bus-with-per-key-ordering)
    - [Parallel API Aggregation](/interview/multithreading/problems/parallel-api-aggregation)
    - [Fork-Join Parallel Computation](/interview/multithreading/problems/fork-join-parallel-computation)
    - [Implement a Future/Promise](/interview/multithreading/problems/implement-a-future)
- [Time-Based State](/interview/multithreading/patterns/time-based). State as a function of the clock
  - **Problems**
    - [Token Bucket Rate Limiter](/interview/multithreading/problems/rate-limiter-token-bucket)
    - [Delayed Task Scheduler](/interview/multithreading/problems/delayed-task-scheduler)
    - [Read-Heavy Cache with Expiry](/interview/multithreading/problems/read-heavy-cache-with-expiry)
    - [Retry with Backoff and Jitter](/interview/multithreading/problems/retry-with-backoff-and-jitter)
    - [Batching Aggregator](/interview/multithreading/problems/batching-aggregator)

### The senior material

- [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures). Guarded state with a throughput requirement: does the invariant decompose?
  - **Problems**
    - [Thread-Safe LRU Cache](/interview/multithreading/problems/thread-safe-lru-cache)
    - [Lock Striping & ConcurrentHashMap](/interview/multithreading/problems/lock-striping-and-concurrent-hashmap)
    - [Lock-Free Stack (Treiber)](/interview/multithreading/problems/lock-free-stack-treiber)
    - [Lock-Free & Bounded Queues](/interview/multithreading/problems/lock-free-or-bounded-queue)
    - [Striped Counter / LongAdder](/interview/multithreading/problems/striped-counter-longadder)
    - [Copy-on-Write Snapshot Registry](/interview/multithreading/problems/copy-on-write-snapshot-registry)
- [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review). Run the method backwards: symptom → cause → proof
  - **Problems**
    - [The Lost Update Hunt](/interview/multithreading/problems/lost-update-hunt)
    - [The Hang That Isn't a Deadlock](/interview/multithreading/problems/the-hang-that-isnt-a-deadlock)
    - [Lock-Order Inversion Review](/interview/multithreading/problems/lock-order-inversion-review)
    - [The Visibility Bug](/interview/multithreading/problems/visibility-bug-no-lock)
    - [Check-Then-Act on a Concurrent Map](/interview/multithreading/problems/check-then-act-on-concurrent-map)
    - [Executor Misuse Review](/interview/multithreading/problems/executor-misuse-review)
- [Distributed Concurrency & Idempotency](/interview/multithreading/patterns/distributed-concurrency). The same problems with no shared memory and partial failure
  - **Problems**
    - [Idempotency Keys](/interview/multithreading/problems/idempotency-keys)
    - [Optimistic Concurrency Control](/interview/multithreading/problems/optimistic-concurrency-control)
    - [Pessimistic Locking & Isolation Levels](/interview/multithreading/problems/pessimistic-locking-and-isolation)
    - [Distributed Locks & Leases](/interview/multithreading/problems/distributed-lock-and-lease)
    - [Exactly-Once Processing](/interview/multithreading/problems/exactly-once-processing)
    - [Flash-Sale Inventory](/interview/multithreading/problems/flash-sale-inventory)
    - [Double-Booking Prevention](/interview/multithreading/problems/double-booking-prevention)

## How to use this

Read the problem statement, close the page, and try it for 15–20 minutes in a blank editor. Only then read the strategy. The strategy pages deliberately contain **no full solutions**. They give you the invariant, the mental model, and the failure modes, so you reconstruct the code from understanding rather than recall. If you can't rederive it a week later, you memorized it.

Two families break that loop on purpose. In **Debugging & Code Review** there is nothing to code first: the diagnosis is the deliverable, so work aloud from symptom to hypothesis to reproduction to fix before opening the strategy. In **Distributed Concurrency** these are spoken design answers: always give the single-JVM answer first, in code-level detail, and name its linearization point before you name any datastore. That ordering is itself part of what gets graded.

[← Back to Interview Prep](/interview)
