---
layout: post
title: Fork-Join Parallel Computation (data parallelism)
date: 2026-07-19
description: >-
  The one question that tests data parallelism (same op, partitioned data) as opposed to everything else in this bank (task parallelism / coordination). It's where…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Classic "parallelize this computation" round: parallel sum / max / merge sort / word count over a large array.

### Problem

Given a huge array (100M longs), compute its sum (or sort it, or count matches) using all cores. Then defend: when does this parallelize well, when doesn't it, and what does the framework do for you?

### Constraints

- Divide-and-conquer: split until a threshold, solve sequentially below it, combine results upward.
- No shared mutable accumulator (the tempting `total += x` across threads is the racy-counter bug from Week 1 at scale).
- Know why a fixed thread pool + recursive tasks deadlocks-by-starvation, and why ForkJoinPool doesn't.

### Clarify before solving

- CPU-bound, right? (Data parallelism's home turf, if the per-element work were I/O, this is the wrong tool entirely.)
- Is the combine step cheap (sum) or real work (merge in merge sort)? Affects the threshold math.
- Result exact/deterministic required? (Floating-point sums reorder, associativity matters; longs don't care.)

### Why this problem matters

The one question that tests **data parallelism** (same op, partitioned data) as opposed to everything else in this bank (task parallelism / coordination). It's where work-stealing, sequential thresholds, Amdahl's law, and "why parallel streams exist and when they lie to you" all get examined. Senior candidates fail it not on code but on the WHEN-NOT answers.

---

## Strategy

### Classify

Type F, data parallelism: recursive fan-out over partitioned data, results combined upward. No shared mutable state AT ALL is the design goal, each subtask owns its range, results merge by return value. Where there's no sharing, there's no locking: the best concurrency is none.

### Invariant

Every array index is covered by exactly one leaf task; the combine tree computes the same result as the sequential fold (watch associativity for floats); no task touches another's range.

### Mental model

Counting a stadium crowd: split the stadium in half, hand each half to a counter, who splits again... until a section is small enough to count by eye (the threshold); totals add upward through the hierarchy. Nobody shares a tally sheet, that's the whole trick. The framework's job is keeping all counters busy even though the tree unfolds unevenly.

### Design (RecursiveTask shape)

```
compute():
    if (range <= THRESHOLD) return sequentialSolve(range)
    left  = new Task(firstHalf);  left.fork()      // async submit
    right = new Task(secondHalf)
    rightResult = right.compute()                  // do half YOURSELF
    leftResult  = left.join()                      // then wait for the fork
    return combine(leftResult, rightResult)
```

Three deliberate choices to narrate:

1. **Threshold** (say ~1k–10k elements, then "I'd measure"): below it, task-creation overhead exceeds the work. Threshold too low → millions of tiny tasks, overhead dominates; too high → too few tasks to keep cores busy. This IS the granularity trade-off, the same idea as batch sizing everywhere.
2. **fork right, compute left yourself** (not fork both + join both): the current thread does half the work in-line instead of parking, halves task count and keeps the worker productive. Standard idiom; explaining WHY is the depth signal.
3. **Results flow via return values, not a shared accumulator.** A shared `AtomicLong total` works but serializes every leaf on one contended cache line (and a plain long is the Week-1 lost-update bug). Combine-upward is contention-free by construction.

### Why ForkJoinPool and not a fixed executor (the core concept)

In a fixed pool, a recursive task that `join()`s its child blocks a worker; the child sits queued, needing a free worker; recurse deep enough and ALL workers are blocked joiners, **deadlock-by-starvation with zero locks** (failure #17 in the refresher). ForkJoinPool fixes it two ways: **work-stealing**, each worker has its own deque, idle workers steal from the tail of others' (locality for the owner, low contention for thieves); and **helping**, a worker blocked at `join()` doesn't park, it runs pending subtasks (often the very child it's waiting for). That's why recursive decomposition is safe here and dangerous on a fixed pool.

### Parallel streams (the 10-second production answer)

`Arrays.stream(a).parallel().sum()`, parallel streams ARE fork/join with automatic splitting (Spliterator). Offer it first, then the hand-built version as the exercise. The WHEN-NOT list is the real exam: (1) I/O or blocking in the lambda, commonPool is shared and CPU-sized, you starve the whole JVM's parallel work; (2) small data, splitting overhead loses; sequential wins below ~10k trivial ops; (3) side-effecting lambdas / shared mutable state, races the framework can't see; (4) order-dependent or non-associative reductions.

### Amdahl's law (attach it here)

Speedup ≤ 1 / (serial fraction + parallel fraction / cores). The sequential threshold work parallelizes; the final combines and the setup don't. 10% serial → ceiling of 10× on infinite cores. One sentence: "before adding threads, I'd ask what fraction is unavoidably serial, that's the ceiling."

### Pitfalls

1. Shared accumulator (racy or atomic-contended) instead of combine-upward.
2. fork both + join both, doubles task count, current thread idles at the join.
3. Recursive tasks on a fixed ExecutorService, starvation deadlock (be able to narrate it).
4. Threshold = 1, overhead swamps work; no threshold, stack of task objects for nothing.
5. Parallel stream with blocking lambda, JVM-wide collateral damage.
6. join() before fork()'s sibling compute, accidental serialization (order: fork, compute, join).

### Check your understanding

1. Narrate the fixed-pool starvation deadlock with pool size 2 and a depth-3 recursion.
2. What TWO mechanisms let ForkJoinPool survive blocking joins? One sentence each.
3. Why is combine-upward contention-free while AtomicLong-accumulation isn't, if both are "correct"?
4. Your parallel sum of 5k elements is SLOWER than sequential. Three likely reasons, in order.
5. Merge sort vs sum: how does an expensive combine change the threshold and the speedup ceiling?

### Transfers to

Parallel streams judgment calls, map-reduce thinking, "why is my parallel code slower" debugging, and the Amdahl conversation attached to any scaling question.
