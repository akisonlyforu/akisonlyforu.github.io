---
layout: post
title: Group Formation & Barriers Playbook
date: 2026-07-19
description: >-
  Admission versus boundary control, the permit-theft bug in naive reusable barriers, static versus dynamic composition, and the dispatcher hat.
categories: interview multithreading patterns
---

Deep dive on the group-formation family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). This family contains the Uber Ride problem, and the reuse bug that every barrier question is secretly about.

The low-level implementation strategy for the whole family: reusable barrier, Building H2O, Uber Ride, river crossing, roller coaster, and any future "wait until a valid group forms, then proceed together" problem.

Everything in this file reduces to one question asked twice:

> **WHO may join the group being formed right now?** (admission control)
> **How do we guarantee groups don't overlap?** (boundary control)

If you keep these two concerns separate (separate sentences when you state the invariant, and usually separate primitives in the code), every problem in this category is a variation on machinery you already have. If you blur them, you get the classic bugs catalogued at the end.

---

## 1. The two separable concerns

**Admission control** answers: of the threads that have arrived, which ones are allowed to become part of the *current* group? It is a per-thread, per-arrival question. Its natural tools are permits (semaphore capacities) or a guarded decision (tallies under a mutex).

**Boundary control** answers: how do we ensure the whole of group k finishes its group action before anything of group k+1 starts? It is a per-group question. Its natural tool is a rendezvous (a barrier) plus a rule about **when new admission permits come back into existence**.

Why insist they're separate: they fail differently and are fixed differently. An admission bug lets a wrong composition form (3 H in a "molecule", a 3+1 car). A boundary bug lets two *individually valid* groups smear into each other (a fast thread from group k+1 acts while group k is mid-action). You can have either bug without the other, so you must argue both independently. In an interview, saying "there are two sub-problems here: admission and boundary" before writing any code is the single highest-value sentence for this category.

The bridge between them: **admission capacity is a consumable resource, and boundary control decides when it is replenished.** Hold on to that phrasing: it explains the barrier action, the reuse bug, and the roller coaster all at once.

---

## 2. The reuse problem: permit theft by lapping threads

Every problem here is repeated: molecule after molecule, car after car, ride after ride. So the meeting mechanism must be *reusable*, and the naive reusable barrier is broken in a famous way. You must be able to produce the exact interleaving from memory.

**The naive design (count + gate), N = 2:**

```
mutex-guarded count; gate = Semaphore(0)

arrive():
    lock { count++; if (count == N) { count = 0; gate.release(N); } }
    gate.acquire()
```

**The theft interleaving (fast A, slow B):**

1. A arrives: count = 1. B arrives: count = 2 → B resets count to 0 and does `gate.release(2)`. Gate now holds 2 permits: one *intended for* A, one for B.
2. A does `gate.acquire()` (takes permit 1), passes the barrier, and (being fast) **laps the loop**: does its round-2 work and calls `arrive()` again before B has been scheduled.
3. A's second arrival: count = 1. A then calls `gate.acquire()`, and **takes permit 2, the one released for B**. A sails through round 2's barrier that hasn't formed.
4. B finally runs its `gate.acquire()` from round 1. Zero permits remain. B blocks forever. Meanwhile A is a full round ahead, and count is polluted with a round-2 arrival that round 2's opener will double-count.

Root cause, stated precisely: **permits are anonymous and rounds are not.** One permit pool serves waiters from two different generations, and a permit released *for* round k's waiters can be consumed *by* a round k+1 arrival. The counter has the mirror image of the same disease: it can't tell which round an increment belongs to. The waiter's real question is "is MY round done?", and the naive design only ever answers "what's the count?".

This is the deep idea of the whole category: **a reusable coordination point must let a signal identify which generation it belongs to.** There are exactly two known fixes.

### Fix 1: Two turnstiles (physical phase separation)

Keep permits anonymous but make it *impossible for two generations to be in the same place at once*. Two gates, strictly alternating:

- **Entry turnstile**: threads accumulate; when the N-th arrives, entry closes behind the group and exit opens.
- **Exit turnstile**: exactly the N in-flight threads drain out; the last one out re-opens entry.

A lapping fast thread that comes around again meets a **closed entry**. It cannot inject itself into the draining phase, because entry and exit are never open simultaneously. Permits for exit exist only during the drain phase and are consumed exactly N times before entry reopens. Generations can't mix because they can't coexist.

```
turnstile1 = Semaphore(0); turnstile2 = Semaphore(1); count under mutex

phase1: lock { count++; if count == N { turnstile2.acquire(); turnstile1.release(); } }
        turnstile1.acquire(); turnstile1.release();
phase2: lock { count--; if count == 0 { turnstile1.acquire(); turnstile2.release(); } }
        turnstile2.acquire(); turnstile2.release();
```

(The acquire-then-release "pass the turnstile" idiom lets N threads file through a 1-permit gate. Details vary by textbook; the *shape* (two gates, never both open) is what to remember.)

### Fix 2: Generation tokens (logical phase separation)

Keep one gate but make the signal *named*: each round gets a generation object/number, and a waiter waits for **its own generation** to be marked complete, not for an anonymous permit.

```
lock + condition; int count; Object generation = new Object();

await():
    lock {
        Object myGen = generation;
        if (--count == 0) { count = N; generation = new Object(); signalAll(); }
        else while (myGen == generation) wait();   // predicate names MY round
    }
```

A lapping thread that re-enters is now waiting on the *new* generation; it cannot consume a wakeup meant for the old one, because the wakeup condition is "generation changed from mine", which is per-round by construction. **This is exactly how `java.util.concurrent.CyclicBarrier` works internally**: a `Generation` object swapped on every trip (and on breakage). When you use CyclicBarrier in an answer, you are buying Fix 2 off the shelf; being able to say *why* it's safe to reuse ("generation token: the waiter's predicate identifies its own round, so lapping threads can't steal wakeups") is the depth the interviewer is fishing for.

Both fixes answer the same question ("is MY round done?"): one physically (generations never coexist), one logically (signals carry the round's identity). Interview default: **use CyclicBarrier; explain the two-turnstile version as the from-scratch alternative and the naive-reset bug as the reason either is needed.**

---

## 3. Admission control: static vs dynamic composition

This is the fork in the road when designing. Read the problem's **composition rule** and ask: *is there exactly one valid composition, or a choice?*

### 3a. Static composition → semaphore capacities (H2O shape)

Rule like "exactly 2 H + 1 O". One valid composition means admission is just **quotas**: give each role a semaphore initialized to its per-group quota. A thread that acquires a slot is, by definition, admitted to the current group: no decision needed, ever. The semaphores *are* the composition rule, encoded in their initial values.

```
roleA_slots = Semaphore(quotaA);  roleB_slots = Semaphore(quotaB)
group = CyclicBarrier(quotaA + quotaB, action: release quotaA + quotaB slots back)

member of role X:
    xSlots.acquire()        // admission: at most quotaX of my role in-flight
    doMyPartOfGroupAction() // safe HERE — see §5
    group.await()           // boundary: whole group completes together
```

**Recognition test:** can you write the composition rule as "exactly a of type A and b of type B and …" with fixed numbers and no "or"? Then static. Changing H2O to CO2 (1 C + 2 O) changes only the numbers, nothing structural.

### 3b. Dynamic composition → tallies + completing-arrival-as-dispatcher (Uber shape)

Rule like "4D, or 4R, or 2D+2R". Semaphore capacities cannot express "or": any fixed initial values either force one composition or admit invalid mixes (try it: capacities that allow 4D also allow 3D to sit admitted waiting for a 4th while 2R starve, or worse). When there's a choice, **someone must decide which composition to form, based on who is currently waiting.** That requires:

- **Tallies** of waiters per type, under one mutex.
- **Gates** per type, initialized to 0 (`Semaphore(0)`): nobody is admitted until selected.
- The **dispatcher hat**: not a separate thread. Each arriving thread, while holding the mutex, increments its tally and checks "does *my arrival* complete some valid composition?" If yes, this thread becomes the dispatcher for this group: it picks the composition, **decrements the tallies for exactly the chosen members, and releases exactly that many gate permits, all before releasing the mutex.** If no, it releases the mutex and blocks on its type's gate.

```
mutex; int waitingA, waitingB
gateA = Semaphore(0); gateB = Semaphore(0)
board = CyclicBarrier(G)                      // G = group size

arrive as type A:
    lock(mutex)
    waitingA++
    if (some valid composition completes with me):   // fixed check order = policy
        choose composition (a of A, b of B), including myself
        waitingA -= a; waitingB -= b                 // decide-and-decrement: atomic, §6
        gateA.release(a - 1); gateB.release(b)       // a-1: I don't permit myself, §8
        dispatcher = true
    unlock(mutex)
    if (!dispatcher) gateA.acquire()                 // doze until selected
    board.await()
    if (dispatcher) performGroupAction()             // drive() / rowBoat()
```

**Recognition test:** does the rule contain "or", a forbidden mix ("never 3+1"), or "any composition satisfying P"? Then dynamic. Also note the free by-product: dynamic composition gives you a natural **exactly-one-actor** (the dispatcher calls `drive()`), which these problems usually demand anyway.

One more recognition nuance: static-with-quotas is really the degenerate case of dynamic where the check "does my arrival complete the group?" has only one possible answer, which is why the semaphore encoding works at all. If in doubt, the dynamic design *always* works; the static design is the simplification you earn when there's no choice.

### 3c. Order-of-checks is policy, not correctness

With multiple valid compositions, the order you test them (4-same before 2+2, or vice versa) determines *which* car forms, not *whether* the invariant holds. Say this out loud: "check order is a composition-preference policy; greedy correctness is what's required." Don't agonize; do name it.

---

## 4. Symmetric peers vs coordinator thread

Second design fork: **who runs the group's lifecycle?**

**Symmetric peers** (H2O, Uber, river crossing): every participant runs the same-shaped code; group formation is emergent. The "special" work per group (the reset, the drive) is done either by a *hat*, the barrier action (static case) or the dispatcher (dynamic case), worn by whichever thread happened to complete the group. No thread is dedicated to coordination.

**Coordinator thread** (roller coaster): the problem statement itself gives you an active service thread with its own loop (the car). Then don't fake symmetry. Let the coordinator own the phases. The recurring handshake is:

> **coordinator releases C permits → participants each take one and do their part → the last participant (counted under a mutex) signals the coordinator with one release → coordinator proceeds.**

That "C permits out, 1 signal back" cell is multiplex + barrier fused, and the roller coaster uses it **twice per cycle** (board phase, unboard phase):

```
// coordinator (car) loop:
boardQueue.release(C); allAboard.acquire()      // load phase
run()                                           // group action
unboardQueue.release(C); allAshore.acquire()    // drain phase, then loop

// participant (passenger):
boardQueue.acquire(); board()
lock { if (++boarded == C) { boarded = 0; allAboard.release(); } }
unboardQueue.acquire(); unboard()
lock { if (++unboarded == C) { unboarded = 0; allAshore.release(); } }
```

Boundary control here is achieved purely by **permit issuance timing**: the next cycle's `boardQueue` permits do not exist until `allAshore.acquire()` completes, i.e., until the previous group has fully drained. A lapping passenger blocks at `boardQueue.acquire()`, same theorem as §2, proved by a different mechanism. (This is the two-turnstile idea with the coordinator playing both turnstiles.)

**How to choose:** if the problem hands you a service entity with its own verb loop ("the car runs", "the flusher writes the batch"), coordinator. If all participants are equal callers into an API, symmetric peers with a hat. Don't invent a coordinator thread for a symmetric problem: it adds a lifecycle (startup, shutdown, idle spin) the problem didn't ask for.

---

## 5. Boundary control details that people get wrong

### 5a. Centralized permit re-issue: the barrier action, not the threads

In the static design, who gives the admission permits back? **The barrier action**: the runnable that CyclicBarrier executes exactly once per trip, after all members arrived, before any is released. Centralizing the re-issue there gives you, for free: it happens exactly once, at a moment when the group is provably complete, with no race about who re-issues or when.

The tempting alternative (each thread releases its own slot after doing its part) **breaks the boundary**: a fast H that finishes bonding and releases its own hSlot lets a *new* H acquire admission while the current molecule's O hasn't bonded yet. Now threads of molecule k+1 are acting during molecule k. The composition of each individual group may still be right; the *boundary* is gone. Per-thread release turns a per-group resource decision into N racing per-thread decisions. This is the same disease as the naive barrier reset (who resets, and when?), wearing a different costume.

### 5b. Why "do your part before the barrier" is safe (the H2O crux)

It looks wrong that `releaseHydrogen()` runs *before* `await()`, surely bonds can interleave across molecules? No, and the argument is worth rehearsing because it's the crux of the static design: (fact 1) the semaphores guarantee at most 2 H + 1 O hold slots at any instant; (fact 2) no slot is re-issued until the barrier action runs, which requires all 3 to have arrived. Therefore everything that executes between two barrier-openings is exactly one group's worth of work. The boundary invariant is about *what happens between openings*, not about code position relative to `await()`.

### 5c. When you additionally need a "no admission during action" rule

The base Uber design lets *tallies* accumulate (new arrivals increment counters and doze) while a car is boarding, that's fine, because selected riders were already removed from the tallies, so no second dispatcher can select them. If the interviewer insists on literally "no one may even seat/tally during boarding," add a boarding lock held from dispatch to `drive()`. Know it, don't add it unprompted: it's stronger than the stated invariant and serializes arrivals for no benefit.

---

## 6. Decide-and-decrement atomicity (the dispatcher's law)

In the dynamic design, the mutex-protected critical section must contain **all three** of: (1) reading the tallies, (2) choosing the composition, (3) decrementing the tallies for the chosen members. One atomic decision.

Break it (check under the lock, decrement later or outside) and two near-simultaneous arrivals can each observe tallies that include the *same* waiters, and both dispatch: **double-selection**. Concretely: waitingD = 3, waitingR = 2. Democrat #4 arrives, sees 4 D, decides "4D car" but hasn't decremented; simultaneously another Republican arrives, sees waitingD ≥ 2 && waitingR ≥ 3 ≥ 2? It sees 2+2 available *using two of the same Democrats*. Two dispatchers release overlapping permit sets; some rider gets counted into two cars, some gate ends up with orphaned permits, a later group departs with the wrong composition or a rider hangs. The fix is not cleverness, it's scope: the tallies must be reduced **at the same instant** the decision is made, under the same lock, so any concurrent arrival computes on post-decision state.

Gate releases can happen inside or right after the same critical section; the essential atom is decide+decrement. (Releasing inside the lock is simplest to argue and semaphore `release` never blocks, so there's no held-lock hazard.)

---

## 7. Skeletons: the three shapes side by side

**Shape S (static composition, symmetric peers):** quota semaphores + CyclicBarrier with permit-re-issuing action.

```
slots[role] = Semaphore(quota[role])
barrier = CyclicBarrier(G, () -> for each role: slots[role].release(quota[role]))

member(role r): slots[r].acquire(); doPart(); barrier.await()
```

**Shape D (dynamic composition, symmetric peers):** tallies + dispatcher hat + zero-init gates + CyclicBarrier.

```
lock { waiting[myType]++
       if (my arrival completes a valid composition (chosen by fixed policy order)):
           waiting[t] -= chosen[t] for all t          // atomic with the choice
           gate[t].release(chosen[t] - (t==myType ? 1 : 0))
           dispatcher = true }
if (!dispatcher) gate[myType].acquire()
barrier.await()                                        // CyclicBarrier(G), reusable
if (dispatcher) groupAction()
```

**Shape C (coordinator thread):** per-phase "C permits out, 1 signal back."

```
coordinator loop:  for each phase p: phaseGate[p].release(C); phaseDone[p].acquire()
                   (group action sits between the phases that bracket it)
participant:       for each phase p: phaseGate[p].acquire(); doPhaseWork(p)
                   lock { if (++done[p] == C) { done[p] = 0; phaseDone[p].release() } }
```

All three enforce the boundary the same abstract way: **admission capacity for group k+1 is created only by the completion of group k**: by the barrier action (S), by the reusable barrier + tally decrements (D), or by the coordinator's ordering of releases (C).

---

## 8. The derivation recipe (composition rule → design)

Given any Type D problem:

1. **Extract the composition rule.** Group size G; types; the set of valid compositions. Write it as a set: H2O → {(2H,1O)}; Uber → {(4,0),(0,4),(2,2)}; barrier → {(N)} one type; roller coaster → {(C)} one type.
2. **State the two invariants separately.** Admission: "every group's composition ∈ valid set." Boundary: "all of group k's action completes before any of group k+1's action starts." (Add any exactly-one-actor requirement: one drive()/rowBoat() per group.)
3. **Static or dynamic?** |valid set| == 1 → Shape S (quota semaphores). |valid set| > 1 (any "or"/forbidden-mix rule) → Shape D (tallies + dispatcher). This is the §3 recognition test.
4. **Peers or coordinator?** Problem gives a dedicated service thread with its own loop → Shape C. Otherwise symmetric peers. (If both a coordinator *and* multiple types with choice appeared, which is rare, the coordinator runs the dispatcher logic inside its loop: tallies feed the coordinator instead of a hat.)
5. **Place the boundary mechanism.** S: CyclicBarrier(G) with centralized permit re-issue in the barrier action. D: CyclicBarrier(G) after selection; tallies-decremented-at-decision already protect against double-selection, and the barrier's generations make it reusable. C: order the coordinator's releases so next-phase/next-cycle permits are issued only after the previous drain signal.
6. **Assign the special role.** S: nobody special (the action is the hat). D: dispatcher (the completing arrival) performs the group action after `await()`. C: the coordinator performs it between phases. Check the self-permit count: a dispatcher never dozes, so it releases G−1 permits *for the group members other than itself* (split across type gates per the chosen composition).
7. **Run the failure-mode catalog (§9)** as a checklist against your design, then the framework's Step 5 checklist. Specifically re-derive: where exactly does a lapping thread block?

If a problem resists step 3 (composition depends on runtime state beyond counts, e.g., "no two passengers from the same company"), you're still in Shape D but the tallies generalize to whatever state the completion-check needs, held under the same mutex. The recipe's spine (atomic decide-and-decrement, targeted zero-init gates, reusable barrier, one designated actor) is unchanged.

---

## 9. Failure-mode catalog

Know each one as: symptom → interleaving → fix. These are the bugs interviewers plant and the ones you'll write yourself at minute 25.

1. **Permit theft (lapping/generation mixing).** Naive count+gate barrier reused; fast thread laps and consumes a permit released for a slow round-k waiter, who then blocks forever (§2 interleaving). Fix: two turnstiles or generation tokens, in practice CyclicBarrier. *Detection is evil: passes light tests; needs loop-heavy stress with skewed thread speeds, and even a passing stress test proves nothing.*
2. **Double-selection of waiters.** Dynamic composition; completion check and tally decrement not atomic; two racing dispatchers select overlapping members (§6). Symptoms: too many permits on a gate, a 3+1-style departure, or a rider seated in "two cars". Fix: decide-and-decrement as one critical section.
3. **Self-permit off-by-one.** Dispatcher releases G permits including one for itself → G+1 members pass the gates for one group (5 riders in a 4-car); or coordinator forgets it isn't a participant. Fix: the dispatcher never dozes, release G−1, targeted by type; state "who is the G-th? me" out loud.
4. **One-shot gate reuse.** Using a CountDownLatch / count+`release(N)` gate for a repeating group: second group deadlocks (latch never resets) or mixes (bug 1). Fix: reusable primitive (CyclicBarrier) or explicit phase machinery; "repeated forever?" is a mandatory clarifying question.
5. **Boundary leak via per-thread permit release.** Each member returns its own admission slot after acting → next group's members admitted while this group is mid-action (§5a). Fix: centralized re-issue at a provably-complete instant (barrier action / coordinator's post-drain release).
6. **Uncounted "last one" (unguarded counter).** Participant counting (`boarded++`, `if == C`) outside a mutex → two threads both think they're last → completion signal released twice → *next* cycle's accounting corrupted. Delayed detonation: semaphore permits are counted, extras don't vanish; the bug bites a cycle later. Fix: mutex around the count; one designated owner per counter reset.
7. **Wrong-size or per-type barrier.** Barrier of 2 for H2O, or one barrier per element type: the barrier must be the GROUP (G = 3), because the boundary invariant is about the group as a whole.
8. **Static capacities forced onto a dynamic rule.** Semaphore(2)/Semaphore(2) for Uber → cannot express "or": either deadlocks on 4-of-a-kind demand or admits invalid mixes. If you're tuning initial permit values to encode an "or", stop: you need Shape D.

---

## 10. Validation against all problems

Recipe (§8) applied to each of the five, from the composition rule forward.

### Reusable barrier

Step 1: one type, valid set {(N)}, repeated rounds. Step 2: admission is trivial (everyone is admissible, quota = group size); boundary is the *entire* problem. Step 3: static, degenerate. Step 4: symmetric peers. Step 5: this problem *is* step 5 studied in isolation: the exercise forbids CyclicBarrier precisely to make you build Fix 1 (two turnstiles) and understand why the naive reset is bug #1 (permit theft). Step 6: the "last arrival flips the turnstile" is the hat. Recipe verdict: **fits**. It's the recipe with admission stripped away, which matches its role as the foundational mechanism; the strategy section's three-attempts progression is exactly §2 of this pattern.

### Building H2O

Step 1: valid set {(2H,1O)}, singleton. Step 3: static → Shape S: hSlots(2), oSlots(1), CyclicBarrier(3). Step 5: permit re-issue centralized in the barrier action (bug #5 if per-thread). Step 6: no special actor required by the problem; barrier action is the reset hat. §5b's "bond before await is safe" argument discharges the boundary invariant. Recipe verdict: **fits exactly**. Reproduces the strategy section's design ([Template 4](/interview/multithreading/mt-framework/)) with the same two named sub-problems.

### Uber Ride

Step 1: valid set {(4D),(4R),(2D+2R)}, three members, so Step 3: dynamic → Shape D. Tallies waitingD/waitingR under a mutex; zero-init demGate/repGate; dispatcher = completing arrival; CyclicBarrier(4); dispatcher calls drive() after await. Step 6 flags the self-permit rule: 4-same → release 3 to own gate; 2+2 → dispatcher's own gate gets 1, other gate gets 2. Step 7 catches bugs #2 (decide-and-decrement, "THE bug of this problem" per the strategy section) and #3. Check-order = policy (§3c). Recipe verdict: **fits exactly**, including the strategy section's note that a boarding lock is an unrequested strengthening.

### River Crossing

Step 1: valid set {(4H),(0,4S),(2+2)}, isomorphic to Uber under renaming, which the recipe makes visible at step 1: same set, therefore same design, before any code. Steps 3–6 produce the identical Shape D instantiation. The one wrinkle ("rower boards last") is discharged at step 5: the dispatcher rows after `await()` returns, and the barrier's semantics mean all 4 have boarded by then; the property is free. Recipe verdict: **fits**, and the recipe doubles as the problem's own grasp-check ("if your two derivations differ structurally, one is wrong").

### Roller Coaster

Step 1: one type, valid set {(C)}, but Step 4 fires: the car is a dedicated thread with its own run loop → Shape C. Two phases (board, unboard) bracket run(); each phase is the "C permits out, 1 signal back" cell; boundary via permit-issuance timing (next boardQueue permits only after allAshore). Step 6: coordinator performs the group action; passengers own the last-one-signals counters (bug #6 if unguarded). Step 7's "where does a lapping thread block?" → at `boardQueue.acquire()`, permits not yet issued. Recipe verdict: **fits**. Note that step 4 (peers vs coordinator) had to come *after* step 3 in early drafts caused no issue here, but the recipe deliberately orders composition-analysis before topology so that roller coaster is recognized as "static composition, coordinator-run" rather than a new pattern.

**Validation summary:** all five derive cleanly; no recipe repairs were needed beyond ordering step 4 after step 3 and adding the step-8 note about state-richer completion checks (neither contradicted any problem; both were made explicit because river crossing's "boat of 6, loads 6-0/3-3/4-2" follow-up and roller coaster respectively probe them).

---

## What the general framework leaves out

Does the 5-step framework ([the five-step framework](/interview/multithreading/mt-framework/)) suffice for this category? **Mostly. Template 4 covers Shape S well and its footnote already warns about one-shot-gate reuse and permit theft. Three gaps, all at the level of "the framework names the pattern but not the decision procedure":**

1. **No static-vs-dynamic fork.** Template 4 is the H2O shape only. The framework's one sentence on Uber ("choose a valid composition while holding one lock, release exactly those rider permits") gestures at Shape D but gives neither the recognition test (|valid compositions| > 1 → tallies + dispatcher) nor the decide-and-decrement law. A candidate armed only with Template 4 will hit failure-mode #8 on Uber/river-crossing. This file's §3 and §6 fill that.
2. **No coordinator-thread variant.** Step 3's pattern table (barrier + turnstile) and Template 4 both assume symmetric peers. The roller coaster's "C permits out, 1 signal back" cell and the permits-issuance-timing boundary argument appear nowhere in the framework. §4 fills that.
3. **Failure modes are scattered.** Step 5's checklist is generic (race/deadlock/lost-wakeup); the Type-D-specific bugs (permit theft interleaving, double-selection, self-permit off-by-one, per-thread-release boundary leak, delayed-detonation double-signal) are only discoverable by reading all five strategy files. §9 consolidates them into a checkable catalog, which is what Step 5 needs for this category.

Nothing in the framework is *wrong* for Type D; Steps 1–2 (classify, invariant) and the anti-over-engineering rules apply unchanged, and the "called once or repeatedly?" clarifying question in the 45-minute plan is exactly the right trigger for the reuse machinery.
