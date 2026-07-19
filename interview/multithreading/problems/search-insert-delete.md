---
layout: post
title: Search-Insert-Delete
date: 2026-07-19
description: >-
  Readers-writers has two roles and a symmetric rule, so it can be memorized without being understood. This one has three roles and an asymmetric rule — insert excludes insert…
categories: interview multithreading problems
---

Part of the [Asymmetric Access](/interview/multithreading/patterns/asymmetric-access/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** The Little Book of Semaphores ch. 6; the classic asymmetric-compatibility problem. Worth doing: it is the hardest instance of the family and the one that proves you can derive a design from a matrix rather than recall a shape.

### Problem

Three kinds of thread operate on a shared singly-linked list:

- **Searchers** examine the list without modifying it. Any number may search concurrently.
- **Inserters** append to the end. Insertion must be mutually exclusive with *other insertions*, but an insert may proceed concurrently with any number of searches.
- **Deleters** remove an item from anywhere. A delete must be exclusive against everything — no other deleters, no inserters, no searchers.

Design the entry and exit protocol for each of the three roles.

### Constraints

- No busy-waiting.
- Concurrency actually allowed by the rules must actually be achieved: a design that serializes searchers, or that blocks an inserter behind a searcher, has failed even though it is "correct".
- Assume the list operations themselves are given; you are designing only the access protocol.

### Clarify before solving

- Confirm the matrix out loud before coding, cell by cell — search/search, search/insert, search/delete, insert/insert, insert/delete, delete/delete. Misreading one cell produces a design that looks right and is wrong.
- Is starvation prevention required? (Base version: no. Know which role starves under your design and say so.)
- Why can two inserters not run together if they only touch the tail? (Because both read and write the tail pointer — a compound update. Worth stating; it's the justification for the insert/insert cell.)

### Why this problem matters

Readers-writers has two roles and a symmetric rule, so it can be memorized without being understood. This one has three roles and an **asymmetric** rule — insert excludes insert but not search — and cannot be pattern-matched. You have to write the compatibility matrix down and let the primitives fall out of it, which is exactly the derivation skill the family is teaching.

It is also the problem where the deleter must hold two exclusion resources at once, so the family's machinery finally collides with multi-lock deadlock: you need an acquisition order argument on top of the lightswitch mechanics.

---

## Strategy

### Classify

Asymmetric access, three categories, non-symmetric compatibility. This is the general case of the family; readers-writers and the unisex bathroom are both simplifications of it.

### Step 1 — Write the matrix (do this before anything else)

| | Search | Insert | Delete |
|---|---|---|---|
| **Search** | ✓ share | ✓ share | ✗ |
| **Insert** | ✓ share | ✗ | ✗ |
| **Delete** | ✗ | ✗ | ✗ |

Read the design straight off it:

- Searchers are internally compatible ⇒ they need a **lightswitch**.
- Inserters are internally *in*compatible ⇒ among themselves a plain **mutex**. But they are compatible with searchers, so they must not touch anything searchers hold.
- Deleters exclude everyone ⇒ they must hold **both** exclusion resources.

The general rule this instantiates: **one exclusion resource per exclusion relationship; internally-shareable categories hold theirs through a lightswitch; internally-exclusive categories hold theirs directly.** A mutex is just a lightswitch whose maximum count is one — that observation is why the three roles need three different-looking mechanisms that are really one mechanism.

### Invariant

No deleter overlaps any other thread; at most one inserter at a time; searchers overlap freely with each other and with a single inserter.

### Design

Three resources:

- `noSearcher = Semaphore(1)` — held while searchers are present, taken by the first searcher via a lightswitch (counter + its own mutex) and released by the last.
- `noInserter = Semaphore(1)` — held for the duration of an insert, taken directly by the inserter (no lightswitch: inserters exclude each other, so the mutual exclusion *is* the requirement).
- The deleter acquires **both**, in a fixed global order, and releases both when done.

Per role: a searcher passes through its lightswitch and searches. An inserter takes `noInserter`, inserts, releases it. A deleter takes `noSearcher` then `noInserter` — **always that order** — deletes, then releases.

### Why this achieves the allowed concurrency

Check each cell against the design, out loud — this is the correctness argument:

- Search/search: the lightswitch means only the first and last searcher touch `noSearcher`; the rest just increment and go. Full parallelism. ✓
- Search/insert: the inserter touches only `noInserter`; searchers touch only `noSearcher`. They never contend. ✓ (This is the cell candidates most often break, by giving inserters a share in the searchers' resource.)
- Insert/insert: both need `noInserter`; the second waits. ✓
- Delete/anything: the deleter needs both, so it waits for the searchers to drain and for any insert to finish, and while it holds both nobody else can acquire either. ✓

### The deadlock hazard — this is what makes the problem hard

The deleter holds two resources. That is hold-and-wait, and it is only safe because of an **acquisition-order** argument: the deleter is the *only* thread that ever wants both, so no other thread can hold one and request the other. There is no second acquisition order in the system, therefore no cycle.

But the moment the problem grows a second two-resource role — say a "compact" operation that also needs both — you must fix a global order across both roles or you get the classic inversion. Say this explicitly: *"only one role takes two resources today, which is why any order works; if a second appears, I'd impose a global ordering."* That sentence shows you know why it's safe rather than that it happens to work.

Second hazard, subtler: the deleter blocks on `noInserter` while already holding `noSearcher`. Run the dependency argument — the releaser of `noInserter` is an inserter, and inserters never need `noSearcher`, so no cycle. Same argument shape as the reader-writer lock's first-in and the bathroom's first-arrival.

### Starvation

Under this design deleters starve first: continuous searcher traffic keeps `noSearcher` permanently held (the lightswitch count never reaches zero), exactly as readers starve writers in the base reader-writer lock. Inserters can also starve deleters. The fix is again a **turnstile** all three roles pass on entry, held by a waiting deleter to dam new arrivals while the current occupants drain. As always: this prevents starvation, it does not make the system fair.

### Pitfalls

1. **Giving inserters a lightswitch on `noSearcher`.** It looks symmetric and it destroys the search/insert concurrency the problem exists to permit. Re-check every matrix cell against the design before you finish.
2. **One global lock "to be safe".** Correct, and it fails the question — it serializes searchers, which the matrix explicitly allows to overlap. If you offer it as a baseline, name the cost immediately.
3. **Unguarded searcher counter.** Two "first" searchers both acquiring `noSearcher`; one blocks forever and the release accounting is broken.
4. **Deleter acquiring in a different order in some code path.** Today there is only one path; the discipline matters the moment there are two.
5. **Missing releases on exception paths.** Every acquire needs its release in a `finally`, and the deleter must release both.
6. **Owner-checked lock for `noSearcher`.** First-in and last-out are different threads. Semaphore.

### Check your understanding

1. Reproduce the matrix from the problem statement without looking, then derive the three mechanisms from it.
2. Why do inserters get a bare mutex rather than a lightswitch, when searchers get a lightswitch? Answer in terms of the matrix, not the code.
3. Prove the deleter cannot deadlock. What property of the current design is doing the work, and what would break it?
4. Which cell does the "give inserters a lightswitch too" mistake break, and how would you notice it in a test?
5. Add a fourth role: a **compactor** that may run with searchers but excludes inserters and deleters. Extend the matrix and the design.
6. Which role starves in the base design, and why is the turnstile fix not fairness?

### Transfers to

Any multi-category compatibility problem: maintenance windows versus live traffic, schema migrations versus reads and writes, index rebuilds, backup versus restore versus serve in a storage system, and the general shape of lock modes in real databases (shared, update, exclusive) — which is exactly this matrix under different names.
