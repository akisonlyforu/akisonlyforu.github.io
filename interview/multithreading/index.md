---
layout: default
title: Multithreading
---

# Multithreading

Concurrency, parallelism, and thread-safe programming — as they actually get asked in senior interviews.

Everything here answers one question wearing different costumes: **who is allowed to proceed, and who tells them?** Start with the 101 to get the vocabulary, learn the framework to get a repeatable method, then work the seven pattern families. The problems under each pattern are there to be *solved*, not read.

## Start here

- [Multithreading 101: everything you must know](/interview/multithreading/multithreading-101) — the concepts, the JMM, the primitives, the failure catalog. One page, no problems, all vocabulary.
- [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework) — classify → invariant → pattern → template → verify, in 45 minutes.

## Patterns

Seven families cover every concurrency question I've seen asked. Each playbook has the mechanics, the derivation recipe, and the failure modes; each problem below it is a worked application.

- [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering) — the baton: who acts next, who hands it over
  - **Problems**
    - [Print in Order](/interview/multithreading/problems/print-in-order)
    - [Print FooBar Alternately](/interview/multithreading/problems/print-foobar-alternately)
    - [Print Zero Even Odd](/interview/multithreading/problems/print-zero-even-odd)
    - [Odd-Even Printer](/interview/multithreading/problems/odd-even-printer)
    - [FizzBuzz Multithreaded](/interview/multithreading/problems/fizzbuzz-multithreaded)
    - [N Threads Round-Robin](/interview/multithreading/problems/n-threads-round-robin)
- [Guarded State & Mutual Exclusion](/interview/multithreading/patterns/guarded-state) — one invariant, one lock, and the check-then-act disease
  - **Problems**
    - [Thread-Safe Singleton](/interview/multithreading/problems/thread-safe-singleton)
    - [Dining Philosophers](/interview/multithreading/problems/dining-philosophers)
    - [Make a Class Thread-Safe](/interview/multithreading/problems/make-a-class-thread-safe)
    - [Traffic Light Intersection](/interview/multithreading/problems/traffic-light-intersection)
- [Bounded Resource & Producer-Consumer](/interview/multithreading/patterns/bounded-resource) — two parties waiting on opposite predicates
  - **Problems**
    - [Bounded Blocking Queue](/interview/multithreading/problems/bounded-blocking-queue)
    - [Thread Pool from Scratch](/interview/multithreading/problems/thread-pool-from-scratch)
    - [Implement a Semaphore](/interview/multithreading/problems/implement-semaphore)
    - [Implement CountDownLatch or CyclicBarrier](/interview/multithreading/problems/implement-latch-or-barrier)
    - [Dining Savages](/interview/multithreading/problems/dining-savages)
    - [The Barbershop](/interview/multithreading/problems/barbershop)
- [Group Formation & Barriers](/interview/multithreading/patterns/group-formation) — admission and boundary, kept separate
  - **Problems**
    - [Reusable Barrier](/interview/multithreading/problems/reusable-barrier)
    - [Building H2O](/interview/multithreading/problems/building-h2o)
    - [The Uber Ride Problem](/interview/multithreading/problems/uber-ride-problem)
    - [River Crossing](/interview/multithreading/problems/river-crossing)
    - [Roller Coaster](/interview/multithreading/problems/roller-coaster)
- [Asymmetric Access & Readers-Writers](/interview/multithreading/patterns/asymmetric-access) — the lightswitch, and choosing who starves
  - **Problems**
    - [Reader-Writer Lock](/interview/multithreading/problems/reader-writer-lock)
- [Task Lifecycle, Async & Parallelism](/interview/multithreading/patterns/task-lifecycle) — where is the work, and who is counting it
  - **Problems**
    - [Multithreaded Web Crawler](/interview/multithreading/problems/web-crawler-multithreaded)
    - [Parallel API Aggregation](/interview/multithreading/problems/parallel-api-aggregation)
    - [Fork-Join Parallel Computation](/interview/multithreading/problems/fork-join-parallel-computation)
    - [Implement a Future/Promise](/interview/multithreading/problems/implement-a-future)
- [Time-Based State](/interview/multithreading/patterns/time-based) — state as a function of the clock
  - **Problems**
    - [Token Bucket Rate Limiter](/interview/multithreading/problems/rate-limiter-token-bucket)
    - [Delayed Task Scheduler](/interview/multithreading/problems/delayed-task-scheduler)
    - [Read-Heavy Cache with Expiry](/interview/multithreading/problems/read-heavy-cache-with-expiry)

## How to use this

Read the problem statement, close the page, and try it for 15–20 minutes in a blank editor. Only then read the strategy. The strategy pages deliberately contain **no full solutions** — they give you the invariant, the mental model, and the failure modes, so you reconstruct the code from understanding rather than recall. If you can't rederive it a week later, you memorized it.

[← Back to Interview Prep](/interview)
