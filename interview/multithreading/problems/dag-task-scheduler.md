---
layout: post
title: DAG Task Scheduler (Dependency-Ordered Execution)
date: 2026-07-19
description: >-
  It is the cleanest test of whether you understand that a topological order does not need to be computed, it can be discovered. Candidates who reach for "sort topologically…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Rubrik's System Coding round names this one explicitly; the same shape turns up at Stripe/Airbnb/Coinbase as "build a job runner with dependencies" in the production-code round. Frequency claim, hedged: **common wherever a round is explicitly about writing runnable production code, rare in LeetCode-style rounds**, calibrate by round format, not by company logo.

### Problem

`DagScheduler` is given a set of tasks; each task has an id, a body to run, and a set of ids it depends on. Execute all of them on a fixed thread pool so that a task starts only after **every** dependency has completed, and tasks with no outstanding dependencies run **concurrently**. `execute()` returns when every task has reached a terminal state (completed, failed, or skipped) and reports the outcome of each.

### Constraints

- Fixed-size `ExecutorService`. No thread-per-task; no unbounded thread creation.
- Maximum parallelism: the moment a task's last dependency finishes, that task should be eligible to run. Level-by-level execution is allowed only as a stated trade-off, not as an accident.
- The dependency graph is supplied upfront (tasks do not discover new dependencies at runtime) but may be malformed, cycles, dangling ids, duplicates.
- Every task's outcome must be observable by the caller; a task that never ran must be distinguishable from one that ran and failed.

### Clarify before solving

- **Cycle policy**: validate upfront and reject the whole DAG, or run what's runnable and report the unreachable residue? (Upfront validation is cheaper to explain and fails fast, say so and pick it.)
- **Failure policy**: a task fails → do its transitive dependents get *skipped* (rest of the DAG proceeds) or does the whole run *fail fast* (cancel unstarted, interrupt running)? This is the single biggest branch; ask before designing.
- **Diamonds and duplicates**: A→C and B→C means C waits for both, runs once. Confirm "runs once", because the naive completion-callback design can submit C twice.
- **Results passing**: do dependents consume dependency outputs, or is the edge pure ordering? (Ordering-only is the usual ask; results = add a concurrent result map, one sentence.)
- **Per-task concurrency caps** (e.g. at most 4 database tasks at once)? That's a bulkhead bolted on, not a change to the scheduler.
- **Retry on failure**? If yes, note that a retried task re-enters the accounting, the pending counter must be re-incremented before re-submission.

### Why this problem matters

It is the cleanest test of whether you understand that **a topological order does not need to be computed, it can be discovered**. Candidates who reach for "sort topologically, then execute in order" have produced a correct program that serializes an embarrassingly parallel workload. Candidates who reach for "each task waits on its dependencies' futures" have produced a program that deadlocks a fixed pool with no lock anywhere in it.

The correct design is a direct application of two mechanics you already own, in-degree counting where the decrement-to-zero is an atomic *claim*, and the pending-counter discipline for termination, which is exactly why this problem is a good measure of transfer rather than recall. It also forces failure semantics into the open: a skipped task still has to participate in the accounting, and the candidate who forgets that ships a scheduler that hangs on the first failure.

---

## Strategy

### Classify

Task lifecycle (Type F), the self-generating branch, with one twist worth saying out loud in the first thirty seconds: **the work is not discovered by running tasks, it is *released* by finishing tasks.** The crawler's workers find new URLs; here the graph is fully known, but a task only becomes *submittable* at the instant its last dependency completes. Structurally that is the same thing: work enters the executor from inside other tasks, so the queue can be empty while the run is far from over, and therefore **the pending-counter discipline applies verbatim**.

Three sub-problems, name them upfront: (1) deciding when a task is ready, (2) termination detection, (3) failure and cycle semantics. Only (2) and (3) carry real risk; (1) is one counter per node.

### Invariant

A task starts only after every one of its dependencies has reached a terminal state and all of them succeeded; every task is submitted **at most once**; `execute()` returns only when no task is queued, none is in flight, and no task can become ready.

The linearization point is per node: the atomic `decrementAndGet()` on that task's remaining-dependency counter that returns **0**. That single instruction is simultaneously "I am ready" and "I am the unique thread allowed to submit me", the same claim-before-work shape as the crawler's `visited.add(url)` boolean, with a counter instead of a set.

### Mental model

A kitchen where every dish has a card pinned to the rail listing how many ingredients it is still waiting on. Nobody walks the rail looking for ready dishes. When a prep station finishes an ingredient, it walks to each card that wanted it and crosses one line off; whoever crosses off the *last* line on a card takes that card to a free cook. The topological order is never written down anywhere, it **emerges** from the crossing-off. That is the whole design.

### Design

**Precompute, once, single-threaded**: for each task, `remaining` = number of dependencies (an `AtomicInteger`), and `dependents` = the reverse adjacency list (immutable after construction, so it is safely published to every worker with zero synchronisation, final-field publication, the cheapest concurrency there is).

**Seed**: submit every task whose `remaining` is already 0. If that set is empty and the task set is not, you have a cycle, see below.

**On completion** of task T, on the worker thread that ran it, for each dependent D: `D.remaining.decrementAndGet()`. If the result is 0, that thread submits D. If the result is > 0, that thread does nothing. Two dependencies of D finishing simultaneously cannot both submit D, because exactly one of the two atomic decrements returns 0, this is the double-submission race the diamond case exists to probe, and the atomic counter closes it without a lock.

Free bonus worth naming: the atomic decrement is also the **visibility handoff**. The submitter's write-then-decrement and the future runner's decrement-then-read are ordered by the counter's volatile semantics, so D sees everything its dependencies wrote (their entries in the result map) with no extra barrier.

**Termination, the pending counter, unchanged.** `pending` = tasks committed-to but not fully finished. The three rules from the crawler are the same three rules here:

1. Increment **before** submit, including for the seed tasks, before any of them can start.
2. Decrement **after** complete, in a `finally`, after the completion callback has released dependents.
3. Dependents are counted (by their own increment inside the submit path) **before** the parent's decrement runs.

Rule 3 is what makes the counter sound in a DAG: at the moment a parent decrements, every dependent it just unblocked has already been counted, so `pending` cannot dip to 0 while a released-but-uncounted task exists. The unique last decrementer *pushes* the done signal, never poll `pending` from the caller.

**Skipping must still decrement.** This is the mistake that separates a working scheduler from one that hangs. When a task fails and policy is skip-dependents, its dependents are not "left alone", they are *resolved as skipped*, which means they must still run the completion callback that releases *their* dependents and still participate in the pending accounting. Give every task exactly one terminal path: run-or-skip, then the identical `onComplete`. A skipped task is a task whose body was a no-op, not a task that vanished from the books. If you model it any other way, the counter never reaches zero on the first failure.

### Why the obvious designs fail

**"Topologically sort, then execute in order."** Correct output, zero parallelism, you turned a DAG into a list. If asked for the batched compromise, offer **level-order (Kahn by layers)**: run all in-degree-zero tasks, wait for the whole level, recompute. Simple, easy to test, and it loses exactly one thing, a straggler in level k blocks fast tasks in level k+1 that only depended on the *quick* members of level k. That is the identical trade-off as the crawler's level-BFS design, and offering it *with the straggler cost named* is a passing answer when time is short.

**"One thread per task; each task blocks until its dependencies' futures complete."** This is the design that gets people. In a fixed pool of N, take a wide DAG whose bottom layer has more than N dependents: all N workers get taken by tasks that are blocking on futures whose tasks are still sitting in the queue with no free worker to run them. Nothing is deadlocked in the lock sense, there is not a single lock in the program, and yet nothing will ever run again. **Pool-exhaustion starvation**, catalog entry #4/#17. Name it by that name. The counter design dodges it structurally: **no worker ever waits**; a worker either runs a task or returns to the pool. Mitigations if pushed: `ForkJoinPool` (a joining worker helps by running pending subtasks), or virtual threads, where a parked thread costs almost nothing and the wait-for-my-dependencies design becomes the natural one rather than the dangerous one.

### Cycles

Two honest options; pick one and say why.

- **Validate upfront** (preferred): run Kahn's algorithm or a DFS colouring over the graph before submitting anything, O(V+E), single-threaded, no concurrency in it at all. A cycle → reject the whole DAG with the offending ids. The runtime then never has to reason about cycles, which keeps the concurrent part as small as possible. "Push the hard reasoning into the single-threaded phase" is a general senior instinct and this is a clean place to state it.
- **Detect at quiescence**: a cycle manifests as tasks whose `remaining` never reaches 0. So when `pending` hits 0, compare `resolved` (completed + failed + skipped) against the total. If `resolved < total`, the residue is exactly the tasks on or downstream of a cycle. Cheap, requires no pre-pass, but reports the problem only after doing work.

Also validate dangling dependency ids in the same pre-pass, a task depending on an id that isn't in the set has `remaining` that can never reach zero, which is indistinguishable from a cycle at runtime and trivially distinguishable upfront.

### Failure propagation: the policy axis again

The same knob as block/balk/timeout/reject in the bounded-resource family: one decision point, several one-line variants. Ask which one the interviewer wants; do not assume.

- **Skip dependents** (usual default): T fails → mark T failed, mark its transitive dependents skipped via the same release-and-decrement path, everything not downstream of T runs to completion. Maximises useful work; the caller gets a per-task outcome map.
- **Fail fast**: T fails → stop submitting anything new (an `AtomicBoolean` checked in the submit path), let in-flight tasks finish or interrupt them, resolve everything unstarted as cancelled. Termination still comes from the pending counter, you are not changing the accounting, only the bodies.
- **Continue regardless**: dependents run even if a dependency failed. Only sane when edges are pure ordering, not data flow. Say that qualification out loud.

Either way, **exceptions never escape a worker unrecorded**. The task body is wrapped in catch-`Throwable`; the outcome is recorded; the release-dependents and pending-decrement happen in `finally`. An uncaught throw kills the worker and, worse here than in a plain pool, freezes termination forever, the counter never reaches zero and the caller waits eternally.

### Trade-offs to have a sentence for

- **Per-node `AtomicInteger` vs one global lock over a ready-set.** The atomics have no serialisation point at all; a global lock turns every completion into a contended critical section on the hot path. Start with the atomics, here, unusually, the lock-free version is also the *simpler* one, so the escalation-ladder caution doesn't apply.
- **Results map**: `ConcurrentHashMap<Id, Result>`; the atomic decrement supplies the happens-before edge, so no extra synchronisation on reads by dependents.
- **Priorities / critical path**: if tasks have durations, submitting long-pole tasks first shortens makespan. Mention as a scheduling refinement, don't build it.
- **Production equivalents**: Airflow/Dagster/Temporal at the orchestration layer; within a JVM, `CompletableFuture.allOf` per node composes the same DAG declaratively, and Spring Batch / Gradle's task graph do it in-process. In a design round, say "I'd express this as a future graph or use a workflow engine"; hand-build the counter only when implementation is the question.

### Pitfalls

1. **Precomputed linear order**: correct, and it throws away the parallelism the question is measuring.
2. **Waiting on dependency futures inside pool workers**: pool-exhaustion starvation on any DAG wider than the pool. The signature failure of this problem.
3. **Skipped tasks that don't decrement**: the run hangs the first time anything fails. The most common *working-then-broken* bug here.
4. **Double submission on a diamond**: releasing D when *any* dependency completes instead of when the counter hits zero; or checking `remaining == 0` and then submitting as two separate steps (check-then-act, with a second decrementer sliding into the gap).
5. **Pending counter incremented inside the task instead of before submit**: a window where the task is submitted, `pending` is still 0, and the caller declares victory.
6. **No per-task catch-Throwable**: silent worker death plus permanent hang, since the decrement lives in the same method that died.
7. **Mutating the dependents adjacency map at runtime**: build it once, freeze it, publish via final fields. A concurrently-mutated graph invalidates every argument above.

### Check your understanding

1. Why is `decrementAndGet() == 0` a *claim* and not merely a test? Give the two-thread interleaving on a diamond that a `remaining == 0` check followed by a submit would get wrong.
2. State the three pending-counter rules as they apply here, and explain precisely which one guarantees the counter cannot hit zero while a just-released task exists.
3. Construct a concrete DAG and pool size where "each task waits on its dependencies' futures" hangs. Name the failure mode, and say what changes under virtual threads.
4. A task fails and policy is skip-dependents. Walk the bookkeeping for a dependent three hops downstream, what does it do to `pending`, to its own dependents, and to the outcome map?
5. Cycle detection upfront versus at quiescence: what does each cost, what does each report, and which would you ship?

### Transfers to

Build systems (Gradle/Bazel task graphs), data pipelines and workflow engines, service-startup ordering, incremental compilation, and any "run these steps in dependency order, as fast as the graph allows" LLD. The in-degree-decrement-as-claim mechanic is the general tool for *event-driven topological execution*; the pending counter remains the general termination tool whenever work enters the executor from inside the executor.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/task-lifecycle/dag-task-scheduler).
