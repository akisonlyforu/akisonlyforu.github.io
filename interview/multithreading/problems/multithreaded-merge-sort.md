---
layout: post
title: Multithreaded Merge Sort
date: 2026-07-19
description: >-
  The canonical fork-join problem stripped to its bones: recurse, fork one half, sort the other in-caller, and JOIN both before you dare merge. The join IS the barrier, and merging early corrupts the output…
categories: interview multithreading problems
---

Part of the [Task Lifecycle, Async & Parallelism](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [Design and implement multithreaded merge sort](https://enginebogie.com/public/question/design-and-implement-multithreaded-merge-sort/1796). The concrete instance of the [fork-join / data-parallelism](/interview/multithreading/problems/fork-join-parallel-computation/) family, so treat that page as the general theory and this as the worked case.

### Problem

Sort a large array using all cores. Merge sort is already divide-and-conquer: split the array in half, sort each half, merge the two sorted halves. Parallelize it, sort the two halves concurrently, and defend where the speedup comes from and where it stops.

### Constraints

- Recursive split; sort halves in parallel; a node may merge only after BOTH its children are fully sorted.
- Below some size, sort in-thread, don't spawn a task per element.
- No `new Thread` per split; a bounded pool with work-stealing.
- Correct sorted output for any input; the merge needs somewhere to put results.

### Clarify before solving

- CPU-bound in-memory sort? (Yes, this is data parallelism's home turf; an external/on-disk sort is a different problem.)
- Stable sort required? (Affects the merge's tie-breaking, not the parallel structure.)
- Can I allocate scratch space, or must it be in-place? (In-place parallel merge is the hard extension, flag it, don't lead with it.)

### Why this problem matters

It is the cleanest possible test of the fork/join dependency: the merge at a node is *strictly ordered after* both children finish, and nothing else. Get the join placement wrong and you merge half-sorted data, corrupt output with zero exceptions thrown. It also forces the two judgment answers seniors are graded on: the sequential threshold, and why this needs no lock at all.

---

## Strategy

### Classify

Type F, data parallelism: recursive fan-out over a partitioned array, results combined upward by the merge. Each task owns a disjoint sub-range; the only coordination is the parent waiting on its two children. Where there's no sharing, there's no locking, the concurrency content here is entirely in the *dependency*, not in mutual exclusion.

### Invariant

Every index belongs to exactly one leaf task; a node's merge reads only its two children's ranges and runs only after both have returned; the parallel result equals the sequential merge sort of the same input.

### Mental model

A relay bracket run backwards. You hand the top half and the bottom half to two runners; each recursively hands off smaller and smaller stretches until a stretch is short enough to sort by hand. Then results flow *up*: a runner may only combine his two children's finished, sorted stretches, never a stretch still being sorted. The whole race is that upward handoff, and the one rule is: don't combine until both hands are full.

### Design (fork right or left, compute the other in-caller)

The [RecursiveAction/RecursiveTask shape](/interview/multithreading/problems/fork-join-parallel-computation/) applies verbatim. At each node: if the range is at or below the threshold, sort it sequentially and return. Otherwise split, `fork()` one half (async submit to the pool), `compute()` the other half yourself on the current thread, then `join()` the forked half, and only then merge. Three choices to narrate:

1. **Join is the barrier, and it gates the merge.** The merge is not part of a child; it is the parent's own work, and it depends on BOTH children's joins completing. `fork; compute; join; merge`, in that order. Merge before the join returns and you're reading a range that's still being permuted, silent corruption. This single ordering is the entire synchronization content of the problem; say it in one sentence and the interviewer knows you understand it.
2. **Sequential threshold cutoff (say ~1k to 8k, then "I'd measure").** Below it, sort in-thread (insertion or `Arrays.sort` on the slice). Without a cutoff, recursion runs to single elements: thousands of tiny task objects whose creation and scheduling overhead dwarfs the comparisons, and a recursion depth that threatens stack exhaustion and thread explosion. The threshold is the granularity dial, same idea as batch sizing everywhere.
3. **fork one, compute the other in-caller** (not fork both + join both): the current thread does half the work in-line instead of parking at a join, halving task count and keeping the worker productive.

### Why this needs NO mutex (say it explicitly)

The partition removes the sharing. At every level the two children own strictly disjoint index ranges, so two tasks running concurrently never touch the same element, there is nothing to guard, so a lock would only add contention for no correctness gain. The *only* dependency is temporal: the merge must not start before its inputs are done, and `join()` expresses exactly that, no more. Contrast the tempting-but-wrong `total += x` shared accumulator of parallel sum; merge sort doesn't even have that temptation, because results flow back through the array ranges the caller already owns. Partitioning, not locking, is what makes it safe.

### The scratch-space problem and the speedup ceiling

Merging two sorted runs of combined length *n* is O(*n*) and needs somewhere to write the interleaved output, a scratch buffer (allocate once, reuse, or ping-pong between two arrays). Now the ceiling: the *top-level* merge is a single sequential O(*n*) pass over the whole array, and it can't start until everything below is done. That serial tail is exactly what **Amdahl's law** taxes, one big sequential merge caps your speedup no matter how many cores sort the halves. The recovery is a **parallel merge** (binary-search a split point in the larger run, recurse the merge itself in parallel), which is the genuinely hard extension and the thing that separates "I parallelized the sorts" from "I parallelized the whole algorithm". Lead with the simple version; offer parallel merge as the ceiling-buster when asked.

### Why ForkJoinPool, not new Thread per split

A thread per split spawns O(*n*) OS threads: each costs ~1MB of stack, the scheduler thrashes, and you exhaust memory long before you sort anything. A recursive task that `join()`s its child on a *fixed* pool instead risks starvation-deadlock, every worker blocked at a join waiting on a child that has no free worker to run it. ForkJoinPool dissolves both: bounded worker count (≈ cores), work-stealing deques so idle workers pull pending subtasks, and *helping*, a worker blocked at `join()` runs pending tasks (often the very child it awaits) instead of parking. That's why recursive decomposition is safe here and lethal on a naive fixed executor.

### Pitfalls

1. Merge before both joins return, half-sorted input, corrupt output, no exception. The signature bug.
2. No threshold (recurse to size 1): task-creation overhead swamps the work, and deep recursion risks stack exhaustion / thread explosion.
3. `new Thread` per split instead of a bounded pool, O(*n*) threads, OOM.
4. Recursive tasks on a fixed `ExecutorService`, starvation-deadlock at the joins (be able to narrate it).
5. Adding a lock around the array "to be safe", pure contention; disjoint ranges need none. Not seeing that is the tell you missed the partition argument.
6. `join()` before the sibling's in-caller `compute()`, accidental serialization; order is fork, compute, join, merge.
7. Forgetting the top-level merge is serial and promising linear speedup, Amdahl caps you until you parallelize the merge.

### Check your understanding

1. State the exact dependency the join enforces, and construct the corrupt output that results from merging one instruction too early.
2. Why does this problem need no mutex when parallel-sum's accumulator does? Name the structural difference in one sentence.
3. Your parallel sort of 4k elements is slower than `Arrays.sort`. Give the two most likely reasons in order.
4. Where is the serial fraction, and what concretely do you change to shrink it?
5. Narrate the fixed-pool starvation deadlock at depth 3 with two workers, then say what ForkJoinPool does differently.

### Transfers to

This IS [fork-join parallel computation](/interview/multithreading/problems/fork-join-parallel-computation/) (parallel sum/reduce is the cheap-combine sibling; merge sort is the expensive-combine one). The join-as-dependency generalizes to the [DAG task scheduler](/interview/multithreading/patterns/task-lifecycle/) (join = a dependency edge), to [parallel API aggregation](/interview/multithreading/problems/parallel-api-aggregation/) (fan-out then join = fan-in), and to [implementing a future](/interview/multithreading/problems/implement-a-future/) (join is `future.get()`). Learn the ordering once and you have all four.
