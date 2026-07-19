---
layout: post
title: Guarded State & Mutual Exclusion Playbook
date: 2026-07-19
description: >-
  One invariant one lock, the check-then-act disease, the locking escalation ladder, safe publication and the Java Memory Model, and multi-lock deadlock.
categories: interview multithreading patterns
---

Deep dive on the guarded-state family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). This is the family that punishes vague thinking hardest, because the broken versions pass their tests.

The family where the enemy is not ordering or grouping but **interleaved access to shared mutable state**. Every problem here reduces to the same three questions:

1. What state is shared and mutable?
2. What invariant spans that state?
3. What guard makes every invariant-touching step atomic and visible?

If you can answer those three precisely, the code writes itself. If you can't, no amount of `synchronized` sprinkling will save you — this family punishes vague thinking more than any other, because the broken versions *pass tests*.

---

## 1. The invariant and the state it spans (one invariant → one lock)

Before any code, enumerate the shared mutable state — every field a second thread can read or write — and write the invariant as one sentence over those fields.

- Singleton: field `instance`. Invariant: *at most one instance is constructed; nobody observes it partially built.*
- Traffic light: field `greenRoad`. Invariant: *at most one road is green; a car crosses only while its road is green.*
- Bounded stack: fields `elements[]`, `size`. Invariant: *0 ≤ size ≤ capacity, LIFO order, no lost/duplicated element.*
- Dining philosophers: five forks. Invariant: *each fork held by ≤1 philosopher; a philosopher eats only holding both neighbors' forks; the waits-for graph is acyclic.*

**The rule: one invariant → one lock.** Everything a single invariant spans must be guarded by the same guard, because the invariant can be *temporarily broken inside* a critical section (size incremented, array not yet written) and must never be observable in that broken state. Two locks over one invariant means a thread holding lock 1 can expose half-updated state to a thread holding lock 2. This is why "one lock for `size`, another for the array" is wrong, and why the traffic light gets ONE lock, not one per road — the invariant "not both green" *spans both lights*.

**The converse also holds: independent invariants may take independent locks.** If the invariant genuinely decomposes into per-resource clauses with no cross-resource clause about *state* (each fork ≤1 holder is five independent invariants), per-resource locks are legitimate — that's what fine-grained locking *is*. The price: any operation needing two of those locks at once has entered multi-lock territory (§5), and any *system-level* clause (no waits-for cycle) is now enforced by protocol, not by a lock. So the honest formulation is:

> Draw a box around each invariant. One lock per box. Operations that must hold two boxes at once need an acquisition protocol.

Also name the **linearization point**: the single instruction inside the guard at which the operation logically happens (the reference assignment, the `greenRoad = myRoad` write, the `size++` paired with the array store). Being able to point at it is the compact proof that your operation is atomic.

## 2. The core disease: check-then-act

Almost every bug in this family is one disease wearing different clothes: **a decision made on state that can change between the decision and the action.**

Canonical interleaving (memorize this; you will re-narrate it in every problem):

```
Thread A                     Thread B
--------                     --------
read state  → "condition holds"
                             read state → "condition holds"
                             act (mutates state)
act (based on a now-STALE read)   ← invariant broken
```

Instances of the same disease:

| Clothing | The check | The act |
|---|---|---|
| Lazy init / singleton | `instance == null?` | construct + assign |
| Traffic light | `greenRoad != myRoad?` | flip the light |
| Stack composition | `!isEmpty()?` | `pop()` |
| Map idiom | `!containsKey(k)?` | `put(k, v)` |
| Counter | read `count` | write `count+1` (read-modify-write is check-then-act in miniature) |

**The only cure is atomicity of the *pair*:** the check and the act must execute inside one critical section (or as one atomic instruction — CAS, `putIfAbsent`, `computeIfAbsent`). Locking only the act, or only the check, cures nothing: the race lives in the *gap*, and the gap is between them.

Corollary that trips people: making the check *volatile* doesn't help. Volatile gives you a fresh read; it does not stop the world between your read and your write. Freshness ≠ atomicity.

## 3. The escalation ladder

Walk it in order, out loud, and state why you stop where you stop. Skipping rungs upward unprompted is the #1 way to fail these questions.

**Rung 0 — Eliminate the shared mutable state.**
- *Immutability*: all fields `final`, set in the constructor, no mutators → thread-safe with zero locks, forever. Ask "can this be immutable?" before anything else.
- *Confinement*: if only one thread ever touches it (thread-local, actor-style ownership), there is no concurrency problem.
- *Class-initialization*: the JVM runs static initialization exactly once, under an internal lock, with guaranteed safe publication (JLS 12.4). This is the holder idiom's engine.

Criteria to stop here: the state never changes after construction, or a JLS mechanism already provides the once-only + publication you need.

**Rung 1 — One coarse lock.** One monitor (or `ReentrantLock`) guarding all state; every public method takes it. Correct by construction, trivially reviewable, and — say this sentence — *"I'll start coarse and refine only against a measured bottleneck."* Modern uncontended locking is nanoseconds; "synchronized is slow" without a profile is cargo cult.

Criteria to escalate: measured contention, or a stated requirement ("reads are 99% and contended", "must scale to N cores").

**Rung 2 — Read-write lock.** Readers share, writers exclude everyone. Pays off only when BOTH hold: reads truly dominate AND critical sections aren't tiny (for tiny sections, the RW lock's own bookkeeping costs more than the exclusion it saves). New risks: writer starvation under read floods, and the read→write **upgrade deadlock** (two readers both try to upgrade; each waits for the other to release its read lock — `ReentrantReadWriteLock` forbids upgrade for exactly this reason: release read, acquire write, **re-check the condition**, because the world changed in between — check-then-act again).

**Rung 3 — Fine-grained locks / CAS.** Split the state into independently-invariant pieces, one lock each (per-node hand-over-hand in a tree, striping in a map), or go lock-free with CAS retry loops on a single atomic reference. Only on explicit request. Costs: multi-lock deadlock risk (need an acquisition-order argument for every pair, e.g. parent→child in trees), invariants that now span protocol not just data, ABA hazards for naive CAS on reused nodes, and a verification burden that stops fitting in your head.

**Rung 4 — Don't build it: use the JDK.** `ConcurrentHashMap` (+ `compute*`/`merge` for atomic per-key compound ops), `ConcurrentLinkedQueue`, `CopyOnWriteArrayList` (small, read-mostly), `LongAdder` (hot counters), `AtomicReference`. Hand-rolling what the JDK ships is a design smell — mention the swap even when the interviewer wants the manual exercise, and remember the JDK structure only makes *its own* methods atomic: `map.containsKey` + `map.put` is still diseased; `putIfAbsent` is the cure.

The ladder is not strictly linear — rung 4 is often the *first* thing to say ("in production I'd use X; shall I hand-build it?"), and rung 0 short-circuits everything. But as an *escalation of locking sophistication*, never climb without a reason you can state.

## 4. Safe publication and the JMM (working depth)

Guarding mutation is half the job; the other half is making sure a thread that *first acquires a reference* to your object sees it fully built. **Writing a reference is not publishing an object.**

The mental model: publishing is mailing an envelope. Without a memory barrier, the compiler/CPU may mail the envelope (write the reference) *before putting the letter in* (the constructor's field writes). A reader on another core opens the envelope and finds garbage. Nothing in a data-race-y program forbids this — reordering is legal wherever no happens-before edge constrains it.

**Happens-before (HB) — the only edges you need for this family:**

1. *Program order*: within one thread, earlier actions HB later ones (as observed by that thread — reordering is still allowed if that thread can't tell).
2. *Monitor*: unlock of M happens-before every subsequent lock of M. → Everything written inside a critical section is visible to the next thread entering it. This is why "lock the writes, read without the lock" is broken: the unlocked read has no incoming edge.
3. *Volatile*: write to volatile v happens-before every subsequent read of v. One write→read edge, and it carries *everything the writer did before the write* (the piggyback rule): `data = 42; flag = true(volatile)` then `if (flag) read data` — the data read is safe.
4. *Final fields*: if an object is constructed *without leaking `this`*, its final fields (and what they transitively reference at freeze time) are visible fully-initialized to any thread that gets the reference — even without synchronization. This is the special exemption that makes immutable objects publishable by any means.
5. *Thread lifecycle*: `t.start()` HB everything in t; everything in t HB `t.join()` returning.
6. *Class init*: static initializers HB any use of the class by any thread (the holder idiom's guarantee).

HB is transitive. A "data race" = two conflicting accesses (at least one write) with no HB path between them — and then *you get no guarantees at all*: stale values, half-objects, reads out of order.

**The canonical exhibit — double-checked locking without volatile:**

```
check instance != null (NO LOCK)  → return it     ← the broken path
lock; re-check; instance = new Thing(); unlock
```

Inside `new Thing()` there are field writes, then the reference assignment. Nothing orders them *for the unlocked reader* — the writer's monitor edge only helps threads that also take the lock. So the reference can become visible before the fields. Thread B's unlocked check sees non-null, returns, and reads garbage fields. Invisible in testing, catastrophic in production. Making the field `volatile` adds edge #3: constructor writes HB volatile write HB volatile read HB B's field reads. Fixed.

**Safe publication checklist** — a reference to a just-built object is safely published if it's handed over via: a `static final` field / class init; a `volatile` field or `AtomicReference`; a lock that the reader also takes; a concurrent collection or `BlockingQueue`; a properly-final-field'd immutable object by any route. And *never* let `this` escape from a constructor (registering listeners, starting threads in the constructor) — that publishes a partially-built object by definition.

## 5. Multi-lock acquisition and deadlock

When one operation must hold two or more locks (transfer between two accounts, two forks, hand-over-hand), a new failure mode appears that no single lock can have.

**Deadlock requires all four Coffman conditions simultaneously:**
1. Mutual exclusion — the resources are exclusive.
2. Hold-and-wait — a thread holds one resource while waiting for another.
3. No preemption — resources can't be forcibly taken.
4. Circular wait — a cycle in the waits-for graph (A waits for what B holds, ... , back to A).

**Every fix breaks exactly one condition — always name which.** The reusable structure for any deadlock question: (a) construct the cycle concretely, (b) recite the four conditions and check them off, (c) present fixes and name the broken condition per fix.

The canonical cycle (philosophers): everyone grabs left, waits for right → P0→P1→P2→P3→P4→P0.

**Fix 1 — Global lock ordering (breaks circular wait).** Impose a total order on locks (fork index, account id, `System.identityHashCode` with a tiebreak); every operation acquires its locks in ascending order. Proof of impossibility in two sentences: every waiting thread waits for a lock *higher* than every lock it holds; a cycle would need some thread to wait for a lock *lower* than one it holds — contradiction. This is THE production answer for "path 1 locks A then B, path 2 locks B then A".

**Fix 2 — Don't hold-and-wait: acquire all-or-nothing.** `tryLock` the second; on failure **release the first** and back off (randomized delay) before retrying. Breaks hold-and-wait. Cost: livelock in theory — all contenders can cycle in lockstep (grab, fail, release, retry, forever); randomized backoff desynchronizes them. The classic self-defeat: retrying *without* releasing the first lock — that IS hold-and-wait; you fixed nothing.

**Fix 3 — Cap the contenders (breaks hold-and-wait at system level).** Admit at most N−1 threads to an N-resource arena (Semaphore(4) around 5 forks): pigeonhole guarantees someone gets a full set, finishes, releases. Simple, slightly conservative.

**Fix 4 — Timeout as defense-in-depth.** `tryLock(timeout)` + release + report, when you don't control all the code paths. Turns a hang into a recoverable failure.

Two more deadlock shapes to check even with one lock: **calling alien code while holding your lock** (a listener/callback that re-enters or takes its own lock — never hold your lock across foreign calls you don't control), and **wait() while holding a second lock** (you release only the monitor you wait on; the other stays held forever).

Deadlock-freedom ≠ starvation-freedom: under lock ordering an unlucky thread can lose the race indefinitely. If asked: fair locks/semaphores (FIFO). Don't volunteer it.

## 6. Compound operations: why safe methods don't compose

Per-method atomicity does not give per-*sequence* atomicity. Every method of a class can be perfectly synchronized and the caller's two-line sequence is still racy:

```
if (!stack.isEmpty())      // atomic ✓, then LOCK RELEASED
    stack.pop();           // atomic ✓, but the world changed in the gap
```

The lock protects each call; nothing protects the *composition*. Same disease as §2, one level up. Fixes, in preference order:

1. **Move the composition inside**: expose an atomic compound method — `Optional<T> tryPop()`, `putIfAbsent`, `computeIfAbsent`, `getAndIncrement`, `transfer(from, to, amt)`. The API grows a method whose body is one critical section covering check + act.
2. **Client-side locking**: document that callers must synchronize on a published lock object around sequences. Fragile (relies on every caller's discipline) — mention, don't recommend.
3. Related honesty about **snapshots**: under concurrency, `size()`, `isEmpty()`, `contains()` return values that are stale the instant they return. They're hints, not guarantees; any caller decision based on them is a check-then-act. Iteration under a coarse lock means holding the lock the whole loop (or copying out first); concurrent collections instead give weakly-consistent iterators — know which contract you're offering.

**Bring the compound-operation trap up unprompted** in any "make it thread-safe" question — it is usually the actual point of the question.

## 7. Pseudocode skeletons

Compact shapes to code from — fill in the invariant, not the ceremony.

**S1 — Guarded state (the default):**
```
lock = mutex; state = ...

operation():
    lock:
        check invariant-relevant condition   // check
        mutate state                         // and act, SAME critical section
        // linearization point = the mutation
```

**S2 — Atomic compound method (fixing composition):**
```
tryPop():
    lock:
        if empty: return None
        return remove_top()        // check + act, one section
```

**S3 — Holder idiom (lazy singleton, the recommended answer):**
```
class S:
    private static class Holder { static final S INSTANCE = new S() }
    static getInstance(): return Holder.INSTANCE
    // JVM class-init: once-only + safe publication, zero sync code
```

**S4 — DCL with volatile (explain-on-demand, don't lead with it):**
```
volatile instance

getInstance():
    if instance == null:               // unlocked fast path (volatile read)
        lock:
            if instance == null:       // re-check: world may have changed
                instance = new S()     // volatile write publishes safely
    return instance
```

**S5 — Ordered multi-lock (transfer / philosophers):**
```
op(a, b):
    first, second = order_by_global_key(a, b)   // e.g. index, account id
    lock first:
        lock second:
            act on both
```

**S6 — tryLock with release + backoff:**
```
loop:
    lock left
    if trylock(right):
        eat; unlock both; break
    unlock left                    // MUST release, or it's hold-and-wait
    sleep(random_small)            // desynchronize → no livelock
```

**S7 — Read-write split:**
```
read():  rlock: return snapshot
write(): wlock: check; mutate
// upgrade = release rlock, take wlock, RE-CHECK condition
```

**S8 — CAS retry loop (single atomic reference/counter):**
```
loop:
    old = ref.get()
    new = f(old)                   // pure function of old
    if ref.compareAndSet(old, new): break
// atomicity of check(old unchanged)+act(swap) provided by hardware
```

## 8. The derivation recipe

Run these steps in order on any Type B problem. Each step either solves the problem or hands a sharper problem to the next step.

1. **Enumerate shared mutable state.** List every field/resource a second thread can touch. If the list is empty (immutable, confined) → done, say why, stop.
2. **Write the invariant(s)** — one sentence per independent invariant, naming exactly which state each spans. Also name what the *operations* are (the public verbs).
3. **Check for a zero-lock exit** (rung 0/4): does a JLS mechanism (class init, final fields) or a JDK structure already give exactly this invariant atomically? If yes → that's the answer; the hand-built version is backup material.
4. **Draw lock boxes: one lock per invariant.** All state one invariant spans → same lock. Independent invariants may get independent locks — but default to ONE lock covering everything until the problem demands otherwise.
5. **Find every check-then-act window.** For each operation: where does it read state to decide, and where does it act? Pull each check+act pair inside one critical section of the owning lock. Mark the linearization point.
6. **Audit multi-lock operations.** If any operation holds ≥2 locks (or the design forced per-resource locks in step 4): construct the potential cycle, check Coffman, choose a fix (ordering first) and *name the condition it breaks*.
7. **Audit publication.** How does another thread first obtain a reference to this object/state? Verify an HB edge exists on that path (final/volatile/lock/class-init/concurrent handoff). Any unlocked fast-path read of a mutable field ⇒ that field must be volatile *and* you must argue why freshness alone suffices there (usually: it's a re-checked hint, as in DCL).
8. **Audit composition.** Which method *sequences* will callers realistically write? For each racy sequence, add an atomic compound method (or explicitly document client-side locking).
9. **Escalate only on stated evidence.** Note where the simple design is conservative (serialized reads, one-diner-at-a-time baseline, lock held during a slow foreign call) — *name the conservatism out loud*, and describe the next rung (RW lock, fine-grained, lightswitch) without building it unless asked.
10. **Verify by narration.** Re-run the §2 canonical interleaving against your design and show where it now blocks; walk one happy path and one contention path; point at the linearization point; run the failure-mode catalog (§9) as a checklist.

## 9. Failure-mode catalog

The family's known diseases — use as the step-10 checklist and as a source of "what's wrong with this code" answers.

| # | Failure mode | Signature | Cure |
|---|---|---|---|
| 1 | Check-then-act outside the lock | check unlocked (or in a separate section), act locked | check+act in ONE section (§2) |
| 2 | Composition race | every method safe, caller sequence racy (`isEmpty`+`pop`) | atomic compound method (§6) |
| 3 | Torn invariant | two locks over state one invariant spans | one invariant → one lock (§1) |
| 4 | Unlocked reads of guarded state | "reads don't need the lock" | reads take the lock too (or the field is volatile AND single-word AND a mere hint); no HB edge = stale/torn |
| 5 | Unsafe publication | non-volatile DCL; `this` escapes constructor; plain field handoff | volatile / final / class-init / locked handoff (§4) |
| 6 | Lock-order inversion | path 1: A→B, path 2: B→A | global lock order (§5) |
| 7 | Fake tryLock fix | retry without releasing what's held | release-then-retry; that hold IS hold-and-wait |
| 8 | Livelock | all contenders cycle grab/fail/release in lockstep | randomized backoff |
| 9 | Alien call under lock | callback/listener/virtual call inside critical section | copy state out, call outside the lock |
| 10 | RW upgrade deadlock | two readers try read→write upgrade | release read, acquire write, RE-CHECK |
| 11 | Snapshot treated as guarantee | branching on stale `size()`/`contains()` | it's a hint; atomic compound op for decisions |
| 12 | Reference leak | returning internal mutable collection / array | copies or unmodifiable views; else callers bypass the lock |
| 13 | Volatile ≠ atomic | `volatile count; count++` | volatile gives freshness, not atomicity; use lock/Atomic |
| 14 | Over-engineering | fine-grained/striped/lock-free unprompted | start coarse; escalate on evidence only (§3) |
| 15 | Big-lock over-serialization (unnamed) | one mutex, cost never acknowledged (all crossings serialized, one diner at a time) | still ship it — but NAME the conservatism and the next rung |

---

## Validation against all problems

Recipe (§8) applied to each of the four, checking that it produces the known-good solution shape.

### thread-safe-singleton
Step 1: shared state = the `instance` field. Step 2: invariant = at most one construction, no partially-built observation. Step 3 — zero-lock exit **fires**: class-init (holder idiom) gives once-only + safe publication with no sync code; eager `static final` if laziness isn't required; enum for serialization. That IS the recommended answer, in the right order (simplest first). Steps 5+7 then generate the DCL discussion as the hand-built alternative: the check-then-act window (`instance == null` → construct) demands lock + re-check, and the publication audit (unlocked fast-path read of a mutable field) demands `volatile`, with the reordering argument of §4 as the "why". **Verdict: recipe produces the exact answer ladder of the strategy section, including the broken-DCL explanation as a forced consequence of step 7, not memorized trivia.** ✓

### dining-philosophers
Step 1: five forks. Step 2: invariants decompose — per-fork exclusivity (five independent invariants) plus a protocol-level clause (acyclic waits-for). Step 4: independent invariants → per-fork locks are legitimate (a single table lock is the correct-but-conservative baseline; failure mode #15 says ship-or-offer it *with the cost named*: one diner instead of two). Step 6 **fires as the heart of the problem**: eating holds 2 locks → construct the P0→…→P4→P0 cycle, check Coffman, produce the three fixes with named broken conditions (ordering / N−1 semaphore / tryLock-release-backoff), matching the strategy section exactly; catalog #7 and #8 cover the fake-fix and livelock pitfalls. Steps 5/7 are quiet (no data race, no publication issue) — correctly so; the strategy section opens by saying the danger isn't a data race. **Verdict: fits, and only because §1/step 4 carry the "independent invariants → independent locks, then protocol" nuance — a naive "one invariant one lock" reading would awkwardly force the global table mutex. Nuance was added for exactly this; re-validated clean.** ✓

### make-a-class-thread-safe
Step 1: the class's fields. Step 2: class-specific invariant (stack: 0 ≤ size ≤ capacity, LIFO, no lost element). Step 3: rung 4 check — "is this a map/queue the JDK ships?" — mention the swap. Step 4: one coarse lock (invariant spans size + elements → catalog #3 kills the two-lock split). Step 5: `pop` on empty, `push` on full = the check-then-act windows. Step 8 **fires as the trap the question exists for**: callers write `isEmpty`+`pop` → add `tryPop()`; snapshot semantics of `size()` (catalog #11). Step 7 catches reference leaks and `this`-escape (catalog #12, the strategy's "don't leak" section). Step 9 = the escalation ladder verbatim, with the anti-over-engineering sentence. Step 6 activates only if the interviewer pushes to fine-grained (hand-over-hand needs the parent→child order argument). **Verdict: recipe reproduces the strategy section's full arc — ladder, compound trap unprompted, leaks, iteration — in the same priority order.** ✓

### traffic-light-intersection
Step 1: one field, `greenRoad`. Step 2: invariant spans BOTH lights ("never both green") → step 4: ONE lock, and per-road locks are killed by §1 (catalog #3) — the strategy section's pitfall 2, derived rather than remembered. Step 5: the check ("is my road green?") + act (flip) + the crossing go in one critical section; linearization point = the `greenRoad = myRoad` write. Steps 6/7 quiet (one lock; no publication subtlety). Step 9 **fires**: holding the lock during `crossCar` serializes same-road crossings the problem would allow in parallel — name the conservatism (catalog #15), sketch the lightswitch/RW upgrade only if asked, note the starvation risk of the parallel version. **Verdict: recipe yields exactly the strategy section's solution and its design discussion, including the "ship simple, name the cost" senior move.** ✓

**Recipe fix applied during validation:** the first draft's rule was the bare "one invariant → one lock", which made dining philosophers awkward (it suggests only the global table mutex). §1 and step 4 now carry the decomposition clause — independent per-resource invariants may take per-resource locks, at the price of entering the multi-lock protocol of §5/step 6. With that, all four validate cleanly; no other step needed adjustment.

---

## What the general framework leaves out

Does the 5-step framework (classify → invariant → pattern → template → verify) suffice for this category? **Mostly, with three gaps this playbook fills:**

1. **No JMM / safe-publication step.** The framework's templates and Step 5 checklist never mention visibility, reordering, `volatile`, `final`-field publication, or happens-before — yet thread-safe-singleton is *primarily* a JMM question, and "reads don't need the lock" is a top real-world bug the checklist won't catch. Recipe step 7 (publication audit) has no counterpart in the framework.
2. **Deadlock treatment is too shallow for this family.** Step 5 asks "can two threads hold locks and wait on each other?" — detection only. It gives no Coffman vocabulary, no lock-ordering discipline, no tryLock-release protocol, i.e. none of the *fix* machinery dining-philosophers grades you on. §5 / recipe step 6 supply it.
3. **No compound-operation / composition check.** The toolbox row hints at it ("compound operations still need an atomic API") but no framework step asks "which caller *sequences* are racy?" — the central trap of make-a-class-thread-safe. Recipe step 8 supplies it.

Minor: the framework's Step 3 maps Type B to "Mutex" (pattern 3), which is the right *primitive* but flattens the real decision — the escalation ladder (where on it to stand, and why) is the actual Type B design choice; "which lock construct" is downstream of that. Not a correctness gap, but the pattern table alone under-specifies this family.

Everything else transfers intact: Step 2 (invariant first) is the load-bearing habit here more than anywhere, and the Step 5 verification checklist plus §9's catalog compose well as the closing narration.
