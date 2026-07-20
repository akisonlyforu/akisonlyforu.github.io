---
layout: post
title: Thread-Safe Leaderboard
date: 2026-07-19
description: >-
  Two structures, a score store and a rank order, that must stay mutually consistent under a firehose of concurrent updates, which is the two-structure invariant that does not…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [Design a thread-safe leaderboard with concurrent updates](https://enginebogie.com/public/question/low-level-design-threadsafe-lederboard-system-with-concurrent-updates/1190). Shows up wherever a score, a rank, or a live ordering is the product: games, trading dashboards, ad auctions, any "top movers" board.

### Problem

Design a leaderboard used by many threads at once. Two kinds of operation, with wildly different frequencies:

- `updateScore(player, delta)`: the hot path, called at high frequency from many threads. Adjust a player's score and let their rank move accordingly.
- `topN()` and `rank(player)`: the read path, called far less often, must reflect the current ordering.

The catch is that a leaderboard is *two* structures wearing one name: a score store (player → score) so you can find and mutate a player's score in O(1), and a rank order (a sorted structure keyed by score) so you can answer top-N and rank in log time. Both must always agree, and the hot path touches both.

### Constraints

- `updateScore` dominates; it is the path whose throughput you are graded on. A design that serializes it on one global lock has a hard contention ceiling, name it.
- The two structures must stay mutually consistent: for every player exactly one entry exists in each, and the player's ranked position equals their stored score.
- `rank(player)` and `topN` must be answerable in better than O(n) if the board is large; a linear scan on every query is a non-answer for a big board.
- Reads may tolerate slight staleness or may need to be exact, ask which, it changes the design.

### Clarify before solving

- **How exact must rank and top-N be?** The single most consequential question, because it is really "is the ordering a hard global invariant or a statistical signal?" A live game leaderboard that is a few milliseconds stale is fine; a payout-deciding final ranking is not. The senior move in this family is negotiating this contract (patterns §7), so ask before designing.
- **Read:write ratio, and how many writer threads?** Decides whether you optimize the update path (striping, per-player locks) or the read path (snapshots). "Firehose of updates, occasional reads" and "read-mostly" want opposite designs.
- **How large is the board, and do you need the whole ordering or just the top K?** If only the top K ever matters, you do not need a fully sorted structure of everyone; a bounded top-K structure plus a cheaper score store may do.
- **Are scores monotonic (only increase) or can they go down?** Monotonic scores let you skip some remove-then-reinsert work and simplify reasoning about a player's movement.
- **Ties, how are they broken?** Equal scores need a deterministic secondary key (player id, timestamp) or the sorted structure collapses distinct players into one slot.

### Why this problem matters

It is the family's cleanest example of an invariant that spans *two* structures and does **not** trivially decompose. A hash map shards because each bin's invariant is local; a leaderboard cannot, because the consistency clause ("this player's position in the rank order equals their score in the store") is a statement *about both structures at once*. Moving a player is not one mutation, it is remove-the-old-score-entry then insert-the-new, a check-then-act across two containers, and any thread that observes the gap between them sees a player who is momentarily in neither position, or in both. Making that transition atomic without freezing every other player is the whole problem.

It is also where you learn that "use a concurrent sorted structure" is a partial answer, not a complete one. `ConcurrentSkipListMap` gives you concurrent ordered access, but it solves the *rank order* in isolation; it does nothing about keeping the *two* structures agreeing with each other. Realizing that the concurrency primitive covers one of your two structures and leaves the cross-structure invariant entirely to you is the graded insight.

---

## Strategy

### Classify

Concurrent data structure, guarded-state family with a throughput requirement (framework Type B under load). But the distinguishing feature is that the state is **two coupled structures**, not one, and the invariant is the coupling between them. That is what makes it harder than a concurrent map and different from an LRU cache (which also keeps two structures consistent, and transfers directly).

### Invariant

For every player, **exactly one entry exists in the score store and exactly one corresponding entry in the rank order, and that entry's ranked position is determined by the score in the store.** No player appears twice, no player is missing from one structure while present in the other, and no observable instant shows a player at a rank that contradicts their stored score.

The linearization point of `updateScore` is the moment the player's new score becomes visible in *both* structures as a unit. Everything hard about this problem is making that single logical instant real when it is physically two writes to two containers.

### Mental model

A wall of numbered pigeonholes (the rank order) and a name-tag board (the score store). To move a player up, you must peel their tag off the old pigeonhole and stick it on the new one. There is an instant between peel and stick where the tag is in your hand and on no wall. If another clerk reads the wall in that instant, they see a player who has vanished; if two clerks try to move the *same* player at once, one peels a tag the other already moved and you get duplicates or a lost update. The fix is to require that whoever is moving a given player holds that player's name-tag, one owner per player at a time, while the pigeonholes themselves stay open for everyone else.

### Deriving the design ([patterns §9 recipe](/interview/multithreading/patterns/concurrent-data-structures/): don't recite it, run it)

**Baseline, and say it first: one lock over both structures.** A `HashMap<Player,Score>` plus a `TreeMap<Score,Player>` (or skip list), every operation under a single mutex. This is *correct*, the invariant holds trivially because nobody sees a partial move, and you should state it as your starting point without apology. Its ceiling is exactly the problem statement: every `updateScore` serializes against every other, so the hot path throughput is capped at one core's worth of critical section regardless of how many writers you have. Name that ceiling; do not leave the baseline until you have a reason.

**Split by read:write pressure.** The updates are the firehose, so the escape is to let updates on *different* players proceed in parallel while keeping each individual player's move atomic.

- The **score store** is a natural fit for a concurrent map keyed by player, `ConcurrentHashMap`, or lock striping by player id. Different players hash to different stripes and their updates never contend. This is the [lock-striping](/interview/multithreading/problems/lock-striping-and-concurrent-hashmap/) result applied wholesale.
- The **rank order** wants concurrent ordered access, which is what `ConcurrentSkipListMap<Score,Player>` (or a `ConcurrentSkipListSet` of (score, player) pairs) buys you: concurrent inserts, removes, and ordered traversal without a global lock.

But here is the seam the two primitives do **not** cover: a move is `rankOrder.remove(oldScore, player)` followed by `rankOrder.put(newScore, player)`, plus the store update. Two concurrent `updateScore` calls on the *same* player will interleave those steps and corrupt the rank order, leaving the player at two scores or none. Concurrency on *different* players is free; concurrency on the *same* player is a check-then-act that must be serialized **per player**. So the design is: stripe/lock by player id for the update, hold that player's row lock across the whole remove-old-then-insert-new sequence on the shared ordered index, then release. The per-player lock makes the two-structure move atomic *for that player* while every other player moves in parallel.

**Say aloud why this doesn't fully decompose.** Even with per-player locking, the rank order is a single shared structure that all writers mutate. `ConcurrentSkipListMap` lets them do so without a global lock, but two players changing places still both touch the ordered index. You have removed the *global* serialization (the baseline's one lock) and replaced it with per-player serialization plus a lock-free ordered index, which is a real win, but the ordered structure is still shared state and its scalability, not the map's, is now the ceiling.

### The read path

`rank(player)` on a skip list means counting how many entries precede the player, which is O(n) unless the structure maintains subtree/span counts (an order-statistics or indexed skip list). Name that: a plain `ConcurrentSkipListMap` answers "who is above me" only by traversal, so if exact rank is a frequent query on a big board you need an order-statistics structure, or you accept the scan, or you relax the requirement.

`topN` is the friendlier query: the ordered index gives you the first N by walking from the top, O(N). If reads tolerate staleness, this is where **copy-on-write snapshots** earn their place: periodically publish an immutable snapshot of the top slice (or the whole ordering) via one volatile reference swap, and let every reader read it lock-free with zero contention against the writers. Readers see a slightly stale board; writers never block on readers. This is the [copy-on-write registry](/interview/multithreading/problems/copy-on-write-snapshot-registry/) pattern imported wholesale, and it is the right answer precisely when the clarifying question established that reads tolerate lag.

### When exactness isn't required: bucketed / approximate leaderboards

If the product doesn't need a total order, only a coarse ranking ("top 1%", "your tier"), quantize scores into buckets and keep a **striped counter per bucket** ([LongAdder](/interview/multithreading/problems/striped-counter-longadder/) shape). An update decrements the old bucket's counter and increments the new one, both hot-write, cold-read, contention-free per bucket; rank becomes "sum the buckets above yours," an estimate. This collapses the exact two-structure invariant into an approximate one-structure invariant and removes the per-player serialization entirely. State it as the option that trades exactness for throughput, which is the family's senior move made concrete.

### Pitfalls

1. Treating "use `ConcurrentSkipListMap`" as the whole answer. It makes the *rank order* concurrent; it says nothing about keeping the score store and rank order agreeing. The cross-structure invariant is still yours.
2. Updating the two structures under separate locks (or no lock), so a reader, or a second updater on the same player, observes the gap between remove-old and insert-new. Duplicates, losses, or a player at a contradictory rank. The per-player critical section must span both writes.
3. Two concurrent updates to the same player racing the remove-then-insert on the ordered index. Classic check-then-act; needs the player's row locked, not just the map's per-bin lock, because the compound spans two containers.
4. Answering `rank(player)` with an O(n) count on every call on a large, hot board. Either an order-statistics structure, or relax to an estimate, but not a silent linear scan.
5. Forgetting ties: equal scores keyed only by score collapse players. The ordered key must be (score, tiebreaker).
6. Reaching for the striped/lock-free design unprompted when the interviewer never said the single lock was too slow. Start coarse, escalate on stated evidence (patterns §16 / anti-over-engineering).

### Check your understanding

1. State the two-structure invariant in one sentence, then explain why it does not decompose the way a hash map's per-bin invariant does. What is the clause that spans both containers?
2. `updateScore` is remove-old-score-entry then insert-new. Construct the interleaving of two same-player updates that corrupts the rank order. Then the interleaving of an update and a `topN` that shows a vanished player. What lock, held across what span, forbids both?
3. Why does per-player striping give you almost-free parallelism on the *store* but not fully free parallelism on the *rank order*? What is still shared?
4. Reads tolerate 500 ms of staleness. What changes? Name the pattern and the exact happens-before edge that publishes a snapshot to a lock-free reader.
5. The interviewer says "we only ever show the top 100, and tiers are approximate." How does the design collapse? Which two structures merge into one, and what does `rank` return now?
6. When is the single global lock the correct final answer, and how would you defend shipping it?

### Transfers to

[lock-striping-and-concurrent-hashmap](/interview/multithreading/problems/lock-striping-and-concurrent-hashmap/) (the store is exactly this, striped by player); [thread-safe-lru-cache](/interview/multithreading/problems/thread-safe-lru-cache/) (the sibling two-structures-kept-consistent problem, map plus an ordered list, same atomic-move discipline); [copy-on-write-snapshot-registry](/interview/multithreading/problems/copy-on-write-snapshot-registry/) (the stale-tolerant read path, snapshot published by one volatile swap); [striped-counter-longadder](/interview/multithreading/problems/striped-counter-longadder/) (the per-bucket counts of the approximate variant); and [concurrent-stock-exchange](/interview/multithreading/problems/concurrent-stock-exchange/) (an ordered book that must stay consistent with the account state, the same coupled-structures invariant at higher stakes).

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/concurrent-data-structures/thread-safe-leaderboard).
