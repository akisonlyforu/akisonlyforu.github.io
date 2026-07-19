---
layout: post
title: The Unisex Bathroom
date: 2026-07-19
description: >-
  The reader-writer lock has an asymmetry that hides the general mechanic: writers are exclusive *and* internally exclusive, so they need no lightswitch of their own. Here both…
categories: interview multithreading problems
---

Part of the [Asymmetric Access](/interview/multithreading/patterns/asymmetric-access/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** The Little Book of Semaphores ch. 6; a standard readers-writers variant. Worth doing: it is the shortest problem that forces two lightswitches instead of one.

### Problem

An office has one unisex bathroom with these rules:

- Any number of men may be inside together.
- Any number of women may be inside together.
- Men and women must never be inside at the same time.

Threads call `manEnter()` / `manExit()` and `womanEnter()` / `womanExit()`. Design the entry protocol.

**Follow-up 1 (always asked):** add a capacity limit, at most 3 people inside at once, regardless of category.

**Follow-up 2 (the real question):** your solution lets a continuous stream of men keep women waiting forever. Fix it.

### Constraints

- No busy-waiting.
- A person already inside must never be blocked from leaving.
- Entry and exit may be executed by different threads of the same category, nobody "owns" the room.

### Clarify before solving

- Is the capacity limit per category or global? (Global, in the standard version, which is what makes it orthogonal to the exclusion rule.)
- Is fairness required, or just freedom from starvation? (Know the difference; the standard fix gives the latter, not the former.)
- Must the *first* arrival of a category be the one that acquires the room? (No, and noticing that "first in" and "last out" may be different threads is the point.)

### Why this problem matters

The reader-writer lock has an asymmetry that hides the general mechanic: writers are exclusive *and* internally exclusive, so they need no lightswitch of their own. Here both categories are internally compatible, so you need **two** lightswitches wired to **one** room, and suddenly the pattern is visible as what it really is: a compatibility relation between categories, not a story about reads and writes. Getting this to click makes search-insert-delete and every other multi-category variant mechanical.

It is also the cleanest demonstration that capacity and exclusion are independent concerns that compose, which candidates routinely tangle into one over-complicated counter.

---

## Strategy

### Classify

Asymmetric access with **two symmetric categories**. Compatibility matrix: man/man ✓, woman/woman ✓, man/woman ✗. Two internally-compatible categories that exclude each other ⇒ two lightswitches, one room lock.

### Invariant

The room's occupants are all of one category; occupancy never exceeds capacity (follow-up 1); a person inside can always leave.

### Mental model

The room has one key, hanging on a hook. **The first person of a category to arrive takes the key; the last one of that category to leave hangs it back.** While the key is off the hook, the other category queues at the door, not because anyone is holding a lock for the whole duration, but because the key is gone. Everyone in between walks in and out freely.

The key is why the room lock must be a **semaphore, not a `ReentrantLock` or `synchronized`**: the person who takes it is almost never the person who returns it, and owner-checked locks forbid that. State this out loud, it's a one-line senior point and the single most common implementation mistake here.

### Design

Per category: a counter (`menInside`, `womenInside`) and a mutex guarding that counter. Shared: `roomEmpty = Semaphore(1)`.

Entry for a category: take that category's mutex; increment; **if the count just became 1, acquire `roomEmpty`** (this may block, deliberately, see below); release the mutex. Exit: take the mutex; decrement; if it hit 0, release `roomEmpty`; release the mutex.

Two subtleties worth narrating:

1. **The first-in blocks while holding the category mutex.** Textbook says never block while holding a lock. Here it is the mechanism: the blocked first-man freezes every other man at the men's mutex, which is exactly "men wait while women are inside". It is safe because of the dependency argument, the only thread that will release `roomEmpty` is a *woman* (the last one out), and women never touch the men's mutex. No cycle, no deadlock. Verify that chain explicitly; the same argument licenses the reader-writer lock's first-in and dining savages' empty-finder.
2. **Symmetry.** Both categories run identical code with their own counter and mutex. If your two branches look different, one of them is wrong.

### Follow-up 1: capacity

Add `slots = Semaphore(3)` and have **each person** acquire one slot on entry and release it on exit. That's it.

The insight to voice: capacity is a **multiplex**, exclusion is a **lightswitch**, and they are orthogonal, one counts people, the other decides which category may be inside at all. Candidates who try to fold the capacity into the category counters end up with a tangled predicate that breaks the moment the last person of a category leaves while others queue.

Ordering matters, though: acquire the room (via the lightswitch) *before* the capacity slot, and release in the reverse order. If a person takes a slot first and then blocks on the room, they are holding a scarce resource while waiting for a condition that the people who could free the room may need slots to satisfy, the same acquire-ordering hazard as taking a mutex before a counting semaphore in the bounded-queue family.

### Follow-up 2: starvation

The base design starves. A woman arriving while women are already inside sees `womenInside ≥ 1`, increments, and walks in without ever touching `roomEmpty`. Under continuous female traffic the count never reaches zero and the men queue forever. Safety is intact; liveness for men is not. **Say this before you're asked**, noticing it is worth more than the fix.

The fix is the same **turnstile** as in the reader-writer lock: one `Semaphore(1)` that everyone must pass through on entry, normally acquire-then-immediately-release, so it costs nothing when uncontended. To let the waiting category in, an arriving member of the starved category holds the turnstile while waiting for the room, which dams all new arrivals of *both* categories behind it; the current occupants drain, the room empties, and the dammed group enters.

Be precise about what this buys: it prevents indefinite starvation, it does **not** make the system fair. Arrival order is still not respected, and a stream of alternating first-arrivals can still make individuals wait a long time. Calling the turnstile version "fair" is a known trap.

### Pitfalls

1. **Owner-checked lock as the room lock.** First-in and last-out are different threads; `synchronized`/`ReentrantLock` cannot express this. Semaphore.
2. **Unguarded counter.** `count++` plus the `== 1` test is a compound action. Two arrivals both reading 0 both believe they are first; both acquire `roomEmpty`; the second blocks forever with the room full of its own category, and the accounting is permanently broken.
3. **Missing last-out release on an exception path.** If a person's time inside can throw, the release must be in a `finally` or the room is locked against everyone, silently, forever.
4. **Capacity folded into the exclusion counters.** Two concerns, two primitives.
5. **Slot acquired before the room.** See the ordering note above.
6. **Asymmetric code between the two categories.** Copy-paste divergence is the usual cause; they must mirror exactly.

### Check your understanding

1. Why can't the room lock be a `ReentrantLock`? Answer in one sentence about ownership.
2. Give the exact interleaving in which an unguarded counter produces two "first" arrivals, and describe the end state.
3. Walk the dependency argument that makes blocking-while-holding-the-category-mutex safe. Which thread releases `roomEmpty`, and what does it need?
4. Construct the starvation timeline for men with three overlapping women. Then walk the turnstile fix through the same timeline.
5. Why must the room be acquired before the capacity slot? Construct the failure in the other order.
6. Generalize: three mutually exclusive categories. How many lightswitches, how many room locks, and what changes structurally? (Nothing structural, which is the point.)

### Transfers to

Search-insert-delete (the same machinery with an asymmetric matrix), the parallel-crossing variant of the traffic-light problem (two roads are two categories), any "these two workload types must not run at the same time" scheduling constraint, and maintenance-window designs where a background job must exclude live traffic.
