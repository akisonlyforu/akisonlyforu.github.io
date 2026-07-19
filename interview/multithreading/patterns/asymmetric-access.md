---
layout: post
title: Asymmetric Access & Readers-Writers Playbook
date: 2026-07-19
description: >-
  The lightswitch in full mechanical detail, the three priority policies and who starves under each, category generalisations, and when a plain mutex wins.
categories: interview multithreading patterns
---

Deep dive on the readers-writers family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). One mechanic solves the whole family; the interview is really about the follow-up question of who you let starve.

The problem family where threads are NOT interchangeable: some categories of thread may share the resource with each other, while other categories need it exclusively. "Many can read, one can write" is the canonical instance, but the real pattern is broader: **any grouping where compatibility is a property of the pair, not of the individual**.

One mechanic solves the whole family — the **lightswitch** — plus one recurring follow-up: which category do you allow to starve? This document gives you the mechanic in full detail, the three priority policies, the generalizations, the JDK mapping, a derivation recipe, and the failure-mode catalog. It is validated at the end against every problem in this category plus two transfer targets.

---

## 1. The lightswitch, mechanically

### The problem it solves

A mutex lets ONE thread hold a lock. Readers need a GROUP to hold a lock collectively: while *any* reader is inside, writers stay out; but readers must not block each other. No single reader can own the exclusion, because readers come and go while the exclusion must persist.

### The mechanic

The group holds the lock through two special members: **first-in and last-out**.

Picture a room whose door lock is wired to the light switch. The first person entering flips the light on — that locks the door against the janitor (writer). People stream in and out freely while the light is on. The last one out flips the light off — now the janitor may enter. Nobody "owns" the light for the whole duration; ownership is an emergent property of the count.

To implement "first" and "last" you need exactly two pieces of state:

1. **A counter** (`readers`) — how many of the group are inside.
2. **A mutex guarding the counter** — because `readers++` / `readers--` plus the check `== 1` / `== 0` is a compound read-modify-write. Without the mutex, two arriving readers can both observe `0 → 1` and both believe they are "first" (see failure catalog, F1).

Plus one shared primitive: **the room lock** (`roomEmpty = Semaphore(1)`), which represents exclusion itself. Writers acquire it directly, like ordinary tenants. Readers acquire it *once per group episode*, via first-in, and release it *once*, via last-out.

```
state: readers = 0
       mutex     = Semaphore(1)     # guards readers
       roomEmpty = Semaphore(1)     # the room lock; 1 = room is empty

enter():                            # readLock
    mutex.acquire()
    readers++
    if readers == 1:
        roomEmpty.acquire()         # first-in flips the switch — MAY BLOCK, holding mutex
    mutex.release()

exit():                             # readUnlock
    mutex.acquire()
    readers--
    if readers == 0:
        roomEmpty.release()         # last-out flips it off
    mutex.release()
```

Count the semaphore operations per group episode: `roomEmpty` is acquired exactly once (by whoever happened to be first) and released exactly once (by whoever happened to be last) — possibly different threads. That is the whole trick, and it's also why a `ReentrantLock` cannot play the role of `roomEmpty`: ReentrantLock is owner-checked (only the acquiring thread may unlock), while a semaphore permit is ownerless. **When a lock must be released by a different thread than acquired it, you need a semaphore.** Say this in the interview; it's a one-line senior point.

### Why blocking while holding the mutex is safe — the dependency argument

Look at `enter()` again: first-in calls `roomEmpty.acquire()` — a potentially long block (a writer may be mid-write) — **while still holding `mutex`**. Normally "block on lock B while holding lock A" is the textbook deadlock smell. Here it is deliberate and safe, and you must be able to argue why:

Deadlock needs a **cycle in the waits-for graph**. The blocked first-in holds `mutex` and waits for `roomEmpty`. Who holds `roomEmpty`? Only a writer can (if a reader group held it, first-in wouldn't be blocking — the count would already be ≥ 1 and this thread wouldn't be first). Does that writer's release path ever need `mutex`? **No — writers never touch the reader mutex.** So the chain is: first-in → waits for roomEmpty → held by writer → waits for nothing we hold. No cycle. Q.E.D.

And the *consequence* of blocking-while-holding is exactly the semantics you want: every subsequent reader queues on `mutex` behind the blocked first-in — i.e., readers wait while a writer writes, and the moment the writer leaves, first-in gets the room and the queue drains through. The smell is the feature.

(This is the same argument as Dining Savages' "block on the pot while holding the counter mutex": safe because the releaser — the cook / the writer — never needs the mutex you hold. If you can transfer the argument between the two problems, you own it.)

One more hygiene point: the only two locks are always taken in the order `mutex → roomEmpty`, never reversed, anywhere in the design. Consistent hold order is the second reason there's no cycle.

---

## 2. The three priority policies

Safety (the invariant) is identical in all three. What differs is **liveness**: who is allowed to starve. This choice IS the interview follow-up; know all three and the cost of each.

### Policy 1 — readers preference (the v1 above)

The skeleton in section 1, plus trivial writer methods:

```
writeLock():   roomEmpty.acquire()
writeUnlock(): roomEmpty.release()
```

**Starvation analysis: writers starve.** A reader arriving while a writer *waits* still gets in: it sees `readers ≥ 1`, increments, and never touches `roomEmpty`. So under continuous overlapping read traffic the count never reaches 0 and the room never empties. Concrete timeline: R1 enters (flips switch); W arrives, blocks on roomEmpty; R2 enters (count 2); R1 leaves (count 1); R3 enters (count 2); R2 leaves … the count oscillates 1↔2 forever, W waits forever. Safety intact; liveness broken for writers. **State this unprompted** — noticing it is worth more than the fix.

### Policy 2 — writers preference, via a turnstile

Add one semaphore that *everyone* must pass on the way in:

```
add: turnstile = Semaphore(1)

writeLock():
    turnstile.acquire()             # HOLD it — bars all NEW arrivals
    roomEmpty.acquire()             # wait for current readers to drain
writeUnlock():
    roomEmpty.release()
    turnstile.release()             # reopen; piled-up readers flood in

readLock():
    turnstile.acquire()             # walk through: blocks only if a writer
    turnstile.release()             #   is waiting or writing
    ... lightswitch enter() as before ...

readUnlock(): unchanged
```

Mechanics of the fix: readers merely *walk through* the turnstile (acquire, immediately release), so with no writer around, nothing changes — reads still run in parallel. A writer, though, grabs the turnstile and **holds it through its entire write**. Effect the moment a writer arrives: new readers pile up *outside* the turnstile; existing readers drain out; count hits 0; roomEmpty opens; the writer proceeds. On `writeUnlock` the turnstile reopens and the pile floods in as one big reader group.

**Starvation analysis: readers can now starve.** The bias is inverted, not removed — a continuous stream of writers can hog the turnstile (each new writer grabs it as the previous releases), and with unfair semaphores even the pile-up readers may keep losing the race. Also don't call this "fair": a reader that arrived before a writer may still cross after it. It is *writer-preferring*, full stop. Naming the residual imbalance yourself is the senior move (failure catalog, F5).

### Policy 3 — fair / FIFO

True fairness means arrival-order queuing: a queue of waiting threads where contiguous *runs* of readers are admitted as a batch, and a queued writer bounds every reader batch. Building this by hand means a FIFO of condition-waiters — **do not hand-roll it in an interview**; name it and reach for `ReentrantReadWriteLock(true)` (fair mode), which queues exactly this way.

**Starvation analysis: nobody starves.** The cost is throughput: fairness limits reader batching (readers behind a queued writer must wait even though the room is only holding readers), so read parallelism — the entire reason you built this — drops under mixed load. Fairness is a tax; pay it only when asked.

Summary table:

| Policy | Writers starve? | Readers starve? | Cost |
|---|---|---|---|
| Readers preference | YES | no | writer liveness |
| Writers preference (turnstile) | no | YES (under writer streams) | reader liveness; +1 semaphore hop per read |
| Fair / FIFO | no | no | reader batching → read throughput |

---

## 3. Generalizations

### Categories as reader groups

"Readers" and "writers" are roles, not identities. The general shape: **N categories; members of the same category may share; some pairs of categories exclude each other.** Each self-compatible category gets its *own* lightswitch (own counter + mutex); categories that must exclude each other flip switches wired to the *same* room lock.

- **Unisex bathroom**: two categories (M, F), each internally compatible, mutually exclusive. Two lightswitches, one `roomEmpty`. First man in locks the room against women; last man out releases; symmetrically for women. Add capacity ("at most 3 inside") as a plain multiplex `Semaphore(3)` acquired *inside* the room, per person — orthogonal to the switches. Starvation follow-up is the same as v1, squared: each side can starve the other; the fix is again a shared turnstile everyone passes.
- **Search–insert–delete** (the classic asymmetric matrix): searchers share with everyone except deleters; inserters share with searchers but exclude *each other*; deleters exclude everyone. Read the matrix off: searchers get a lightswitch on `noSearcher`; inserters get a lightswitch on `noInserter` PLUS a plain mutex among themselves; a deleter acquires `noSearcher` AND `noInserter` (both rooms, fixed order). Categories that are internally exclusive don't need a switch — a mutex is a degenerate lightswitch with max count 1.

### Multiple lightswitches — the general rule

Draw the **compatibility matrix** first (see recipe, step R1). Then: one exclusion semaphore per exclusion relationship; every internally-shareable category holds each of its exclusion semaphores *via a lightswitch*; internally-exclusive categories hold them directly. If any thread must hold two or more rooms, fix a global acquisition order.

### When the RW structure LOSES to a plain mutex

RW locks exist for exactly one workload: **long/frequent reads, rare writes**. Two ways it goes wrong:

1. **Short critical sections.** The lightswitch costs two extra lock operations per read (mutex in, mutex out) and puts every reader through the *same* counter — a contended cache line. If the protected read is a few nanoseconds (read a field, check a flag), the bookkeeping costs more than the exclusion it optimizes. A plain mutex — or a `volatile` read, or an immutable snapshot swap — wins.
2. **Write-heavy load.** If writes are frequent, readers rarely get to overlap anyway; you pay the machinery for parallelism you never collect, plus starvation policy headaches for free.

Rule of thumb to say out loud: *"RW pays when reads dominate and hold the lock long enough to overlap; otherwise I'd ship a plain mutex and measure."* Interviewers reward the refusal more than the pattern.

---

## 4. JDK mapping

Hand-roll when implementation is the question; name the JDK in design answers.

**`ReentrantReadWriteLock`** — the direct production counterpart.
- **Nonfair (default)**: barging allowed; throughput-oriented. Worth knowing: even in nonfair mode, an arriving reader that finds a *writer at the head of the wait queue* will queue behind it — a built-in mitigation of exactly the v1 writer-starvation problem. The JDK made the same policy journey you just did.
- **Fair (`new ReentrantReadWriteLock(true)`)**: FIFO arrival order = Policy 3.
- **No upgrade (read → write).** Naive upgrade deadlocks, and you must be able to say why: a thread holding the read lock that calls `writeLock().lock()` waits for `readers == 0` — which includes *its own* read hold → self-deadlock. Even without self-counting: two readers both trying to upgrade each wait for the *other* to release its read; neither will → mutual deadlock. RRWL sidesteps the trap by simply blocking such an attempt forever. The correct idiom: release read, acquire write, **re-validate the state you read** (the world may have changed between the two).
- **Downgrade (write → read) IS supported**: acquire write → acquire read → release write. Safe because it only *relaxes* exclusivity; nobody can sneak in between the two steps since you hold write during the read-acquire. Useful for "modify, then keep reading a consistent view".

**`StampedLock`** — awareness level only: `tryOptimisticRead()` returns a stamp with NO lock at all; you read, then `validate(stamp)` — if a writer intervened, retry or fall back to a real read lock. Removes even the reader-counter contention from the hot path. Caveats to name: not reentrant, no Conditions, easy to misuse. Mention it as "the next step if the reader counter itself becomes the bottleneck"; do not code it.

---

## 5. Derivation recipe

Given a new problem that smells like Type E ("many may X, one may Y", "group A shares, group B exclusive"):

- **R0 — Try to make reads lock-free first.** If the read path can be a single read of an immutable or concurrent structure — a `volatile` reference to an immutable snapshot, a `ConcurrentHashMap` entry that carries its own validity — you may not need an RW lock *at all*. The best readers–writers solution often has no room lock on the read path. Only proceed to R1 if reads genuinely need to exclude in-progress writes over a multi-step section.
- **R1 — Draw the compatibility matrix.** List the thread categories. For each pair (including a category with itself): may they overlap? This matrix IS the spec; everything else is mechanical.
- **R2 — State the invariant and the linearization point.** One sentence read off the matrix (e.g., "active readers > 0 ⇒ no active writer; at most one writer"). The linearization point of "the group takes the room" is first-in's `roomEmpty.acquire()`; of "the group leaves", last-out's release.
- **R3 — Allocate primitives from the matrix.** One exclusion semaphore per exclusion relationship (the "rooms"). Each internally-compatible category holds its rooms via a lightswitch (counter + mutex, first-in acquires, last-out releases). Internally-exclusive categories acquire rooms directly. Multiple rooms per thread ⇒ fix a global acquisition order.
- **R4 — Choose the starvation policy — ask, don't assume.** Default readers-preference and *say who starves*; add a turnstile if the excluded category must make progress; name fair/FIFO (JDK fair mode) if strict fairness is demanded. Never claim the turnstile version is fair.
- **R5 — Add capacity if present.** "At most N inside" is an orthogonal multiplex `Semaphore(N)` acquired per-member inside the room.
- **R6 — Verify against the failure-mode catalog (section 6)** plus a starvation timeline for the chosen policy; confirm the dependency argument for the block-while-holding-mutex point and consistent lock order.
- **R7 — Sanity-check against a plain mutex.** Short critical sections or write-heavy load ⇒ state that RW loses and ship the mutex.

---

## 6. Failure-mode catalog

- **F1 — Torn counter → two "first" readers → deadlock.** `readers++` outside the mutex: two threads both read 0, both write 1, both take the `if readers == 1` branch and call `roomEmpty.acquire()`. The second blocks forever *while the room is full of readers* — and the accounting is now broken: last-out will release one permit, but two were (attempted) taken; writers are locked out permanently. Root cause: the increment-and-test is a compound action; the mutex is not optional.
- **F2 — Missing last-out release on an exception path.** If user code between `readLock` and `readUnlock` throws (or early-returns) and `readUnlock` is skipped, the count never reaches 0 and `roomEmpty` is never released: writers are locked out forever, silently. Discipline: `lock(); try { … } finally { unlock(); }` — same for the writer path, and same inside your own lock implementation if a semaphore acquire can be interrupted after the counter was bumped.
- **F3 — Upgrade deadlock.** Offering read→write upgrade casually. Trace: two readers, both decide to upgrade; each waits for readers == 0; each is *itself* one of the readers the other waits for. Correct answer: no in-place upgrade — release, re-acquire as writer, re-validate. Downgrade is fine (section 4).
- **F4 — Writer starvation shipped without comment.** Presenting v1 as done. Safety is intact, so tests pass; liveness for writers is broken under load. Run the timeline in section 2 unprompted.
- **F5 — Calling the turnstile version "fair".** It's writer-preferring; a writer stream starves readers, and semaphore unfairness means no ordering guarantees at all. Name the residual bias.
- **F6 — Wrong primitive for the room lock.** Using an owner-checked lock (`ReentrantLock`, `synchronized`) as `roomEmpty`: last-out is usually a *different thread* than first-in, and owner-checked unlock throws (or is simply impossible for `synchronized`). The room lock must be a semaphore.

---

## 7. Validation

The recipe applied to every problem in this category, then to two transfer targets that reference this pattern. (Recipe note: the first draft of the recipe began at R1; validating against the read-heavy cache showed it over-applied the lightswitch to a problem whose best read path has no lock at all. R0 was added and all three validations re-run — they pass as written above.)

### 7.1 reader-writer-lock (primary — the one problem in 05)

R0: doesn't fire — the lock itself is the deliverable, so a lock-free bypass is out of scope (say so). R1: matrix — reader/reader ✓ share, reader/writer ✗, writer/writer ✗. R2: invariant "(activeReaders > 0 ⇒ no active writer); at most one writer" — matches the strategy section verbatim; linearization = first-in's acquire. R3: one room (`roomEmpty`); readers are internally compatible ⇒ lightswitch; writers internally exclusive ⇒ acquire directly. Produces exactly the v1 in the strategy section. R4: the follow-up ("your version starves writers — fix it") is precisely the readers-pref → turnstile transition; F4/F5 supply the narration. R6: F1–F3 are the strategy section's pitfalls 1, 2, 4. R7: the "when to refuse an RW lock" line reproduces the strategy's production note. **Verdict: recipe reproduces the existing strategy end-to-end with nothing left over. PASS.**

### 7.2 Transfer — traffic-light intersection, parallel-crossing variant

The base problem is guarded state (one mutex, hold it across the crossing). The optimization — same-road cars cross concurrently — is this pattern via the *categories* generalization: R0 doesn't fire (crossing is an activity, not a data read; it can't be made "lock-free"). R1: matrix — roadA/roadA ✓, roadB/roadB ✓, roadA/roadB ✗. No thread is a "writer"; both categories are reader groups — confirming R1's category framing over role names. R3: two lightswitches (one counter+mutex per road) wired to one `intersectionEmpty` room; first car of a road flips the light and takes the room, last car out releases. R4: readers-preference per road ⇒ a road with endless traffic never yields — the strategy section's "writer-starvation in disguise" — fixed with a shared turnstile (or bounded batches per green). R6: F1/F2 apply unchanged (per-road counters, exception on crossing). **Verdict: fits cleanly through the categories-as-reader-groups generalization; the starvation analysis transfers verbatim. PASS.**

### 7.3 Transfer — read-heavy cache with expiry, read path

R1 alone would suggest: readers share, refresh/writes exclusive ⇒ lightswitch around the map. That is exactly the over-application R0 exists to catch: the cache's read path is a `ConcurrentHashMap` get of an *immutable* entry carrying its own expiry — a single safe read, no multi-step section to protect. R0 fires: no room lock on reads at all; exclusion is needed only per-key for loads, delivered by `computeIfAbsent` + cached futures, not by a global writer lock. The cache strategy's pitfall 1 ("global lock around get → serialized reads") is this validation stated as a bug. **Verdict: the recipe correctly routes AWAY from the lightswitch — R0 is the load-bearing step; without it the recipe would have endorsed the anti-pattern. PASS (and this validation is what forced the R0 fix).**

---

## What the general framework leaves out

Does the 5-step framework ([the five-step framework](/interview/multithreading/mt-framework/)) suffice for this category? Mostly — Step 1 Type E, Step 3 pattern 6, and Step 5's starvation check (item 4, which even names "writers behind endless readers") carry the core. Three gaps:

1. **No compatibility-matrix step.** Step 2 asks for a one-sentence invariant, which works for 2 symmetric categories but gets cramped for asymmetric multi-category problems (search–insert–delete, bathroom). The matrix (recipe R1) is the missing intermediate artifact between "classify" and "invariant" — for Type E it should be drawn explicitly before Step 2.
2. **Type E maps unconditionally to the lightswitch.** Step 3's table sends Type E straight to pattern 6, but the modern best answer for read-heavy *data* (the cache) skips the lock entirely via immutable entries + CHM. The framework's anti-over-engineering rules gesture at this ("simplest correct tool") but never say "try lock-free reads before building an RW lock" — recipe R0 fills that hole.
3. **Minor: no Type E template.** Step 4 gives memorizable skeletons for Types A–D; Type E has none. The two skeletons in section 2 of this playbook are the missing Template 5.

No other gaps: verification (Step 5), timeboxing, and the follow-up list ("make readers not starve writers") already anticipate this category's interview arc.
