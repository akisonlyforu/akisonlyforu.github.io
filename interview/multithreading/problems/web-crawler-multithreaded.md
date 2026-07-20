---
layout: post
title: Web Crawler Multithreaded (LC 1242)
date: 2026-07-19
description: >-
  First problem where the thread count is dynamic-ish and the hard question isn't mutual exclusion but termination detection: workers both produce and consume work, so "queue…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1242](https://leetcode.com/problems/web-crawler-multithreaded/), and a very common real interview shape ("parallelize this graph traversal").

### Problem

Given a start URL and an `HtmlParser.getUrls(url)` (blocking network-ish call), crawl every URL reachable from the start that shares its hostname. Use multiple threads. Return all crawled URLs, each fetched exactly once.

### Constraints

- `getUrls` is slow (I/O), that's WHY threading helps here (and a sizing discussion: I/O-bound → threads ≫ cores).
- No URL fetched twice, concurrent dedup is a core requirement.
- The crawl must terminate: you must KNOW when there's no more work, the genuinely hard part.

### Clarify before solving

- Same-hostname filter only? Max depth? (LC: hostname only, no depth.)
- Roughly how many URLs? (Bounds your pool size / memory.)
- Failure policy if getUrls throws? (LC ignores; production: say retry/skip policy.)

### Why this problem matters

First problem where the thread count is dynamic-ish and the hard question isn't mutual exclusion but **termination detection**: workers both produce and consume work, so "queue empty" does NOT mean "done", a busy worker may be about to add more. Getting termination right, and choosing the right dedup idiom, is what separates this from every previous problem. This is Type F thinking: completion, lifecycle, in-flight accounting.

---

## Strategy

### Classify

Task lifecycle: parallel graph traversal where workers GENERATE work. Three sub-problems, name them upfront: (1) parallel fetching, (2) concurrent dedup, (3) termination detection. Only (3) is hard.

### Invariant

Each URL fetched at most once (dedup is the linearization point: winning the "claim this URL" race); the algorithm returns only when every reachable URL has been fetched AND no work is in flight.

### Sub-problem 1: parallelism

Fixed `ExecutorService`. I/O-bound → pool larger than cores (conceptual #11: cores × (1 + wait/compute), then say "heuristic, would measure"). Don't spawn a raw Thread per URL, unbounded thread creation is the anti-pattern this question fishes for.

### Sub-problem 2: dedup (small but classic)

`visited.contains(url)` then `visited.add(url)` = check-then-act race: two workers both pass contains, fetch twice. The idiom: **claim atomically**, `ConcurrentHashMap.newKeySet().add(url)` returns false if already present; the winner fetches. One atomic op is both check and claim. Claim BEFORE fetching, not after (claiming after = duplicate fetches in the race window). This tiny decision is a favorite probing point.

### Sub-problem 3: termination (the heart)

Why "queue empty → done" is wrong: worker A holds the last queued URL, queue is empty, worker B sees empty and declares victory, while A's fetch is about to enqueue 50 new URLs. **Empty queue ≠ no work; work in flight counts.**

Three sound designs, in order of interview practicality:

1. **Pending-task counter.** Atomic `pending`. Increment BEFORE submitting each task; decrement when a task fully finishes (after enqueueing its children, children were already counted by their own increments). Done when pending hits 0; last decrementer signals the waiting main thread (or CountDownLatch-like). The discipline: increment-before-submit, decrement-after-complete, children counted before parent's decrement, say those three rules; they're the whole correctness.
2. **Future-tree / structured recursion.** Each task submits children and waits on their Futures (or uses fork/join). Termination = root future completes. Clean logic; caution: waiting inside pool threads can thread-starve a small fixed pool (say this trade-off; it's a strong senior signal, and virtual threads dissolve exactly this concern, a natural #18 mention).
3. **Batch/level BFS.** Main thread submits level k, waits for all (invokeAll), collects level k+1. Simplest to reason about, slight parallelism loss at level boundaries. A perfectly good interview answer, offer it if time is short.

### Correctness sketch (design 1)

pending > 0 while any task is submitted-but-unfinished (invariant by the increment/decrement discipline). pending == 0 → every submitted task finished and enqueued nothing uncounted → no reachable-but-unfetched URL remains (every fetched page's links were claimed-or-skipped and counted). Exactly-once from the atomic claim.

### Pitfalls

1. Termination by queue-empty polling, the bug this problem exists to catch.
2. Dedup claim after fetch, or contains+add non-atomically.
3. Waiting on child futures in a fixed pool sized too small → all threads waiting, none working: deadlock-by-starvation. Know it, name it, size or restructure.
4. Forgetting hostname filter before claiming (wasted claims are harmless; wasted FETCHES aren't).
5. No shutdown of the executor at the end (resource leak; also `awaitTermination` semantics, conceptual #26).

### Check your understanding

1. Give the exact interleaving where queue-empty termination returns early.
2. Why must pending be incremented BEFORE submit, not inside the task? (Window where pending==0 while a submitted task hasn't started.)
3. Why is `newKeySet().add()`'s boolean the linearization point of "this URL is mine"?
4. How do virtual threads change design 2's main risk?
5. Production follow-ups to have one sentence for: per-host rate limiting (semaphore per host, multiplex!), retries (bounded, with the pending counter unchanged? No: re-submission re-increments), max depth (carry depth in the task).

### Transfers to

Any parallel traversal (file tree walker, dependency resolver), fan-out/fan-in service calls, "process a work graph" LLD. The pending-counter discipline is the general termination tool for self-generating work.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/task-lifecycle/web-crawler-multithreaded).
