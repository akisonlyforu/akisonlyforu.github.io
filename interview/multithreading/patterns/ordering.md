---
layout: post
title: Ordering & Turn-Taking Playbook
date: 2026-07-19
description: >-
  The baton mechanic behind every print-in-order problem, targeted semaphores versus the shared-state condition loop, termination, and the ten ways it breaks.
categories: interview multithreading patterns
---

Deep dive on the ordering family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). These are the problems everyone meets first, and the ones whose lessons every later family reuses.

**Family members:** Print in Order (LC 1114), Print FooBar Alternately (LC 1115), Print Zero Even Odd (LC 1116), Odd-Even Printer, FizzBuzz Multithreaded (LC 1195), N-Threads Round-Robin.

**One-line definition:** the scheduler decides who *runs*; your code decides who *proceeds*. Every problem in this family is the same question — *who is allowed to act next, and who tells them?* — asked with different costumes.

---

## 1. The core mechanic: the baton

The scheduler gives you zero ordering guarantees. The **only** way thread B acts "after" thread A is:

> B **waits** on something. A, as the last act of its turn, **signals** that something.

Call the right-to-proceed the **baton**. The family invariant, in every problem, is some version of:

> **At any instant, at most one thread holds the baton, and the baton's holder is exactly the thread the output sequence requires next.**

Three properties every correct solution must have:

1. **Exclusivity** — one baton in the whole system. If two threads can proceed at once, output interleaves garbage.
2. **Persistence** — the signal must survive being sent *before* anyone is waiting for it (thread `third()` may be scheduled last; `first()`'s "done" signal must still be there when `second()` finally arrives). A semaphore permit persists. A bare `notify()` with no state does not — **state is what persists; notify only wakes**.
3. **Progress** — every acquire/wait is eventually matched by a release/notify, *including at shutdown* (see §7 Termination — this is where most candidates actually fail).

The baton's **route** is the classifying question inside the family:

| Route | Meaning | Problems |
|---|---|---|
| **Chain** (one-way, no return) | A→B→C, each hop happens once | Print in Order |
| **Fixed rotation** (static cycle) | A→B→A→B..., route known at compile time | FooBar, Odd-Even |
| **Data-driven** (computed from state) | next holder depends on a counter's value | Zero-Even-Odd (router), FizzBuzz, Round-Robin |

---

## 2. State: exactly what you track, and what guards it

Ordering problems need shockingly little state. Enumerate it explicitly before coding:

| State variable | Purpose | Needed when | Guarded by |
|---|---|---|---|
| `current` / `i` (int counter) | which output event is next; often *doubles as the turn token* (parity/mod/divisibility derives whose turn it is) | any repeated sequence | the one lock (Style 2) — or implicitly by "only the baton holder touches it" (Style 1) |
| turn token (`stage`, `fooTurn`, enum ZERO/ODD/EVEN) | whose turn, when not derivable from the counter | condition-loop style, when counter alone doesn't encode the turn | the one lock |
| semaphore permits | the baton itself, distributed as "which door is open" | semaphore style | the semaphore's own internals |
| per-thread loop index | how many turns *this* thread takes | each thread's own stack — **no guard needed**, it's thread-confined | nothing (local) |

Two rules that prevent whole bug classes:

- **One shared mutable state ⇒ one lock.** Two locks "one per thread" cannot guard one variable — instant race.
- In the semaphore style, the shared counter is safe *without* a lock only because the baton invariant guarantees at most one thread is between `acquire` and `release` at a time — and only if that thread is the sole writer during its turn. If you break the one-baton property, this safety evaporates. Say this dependency out loud; it's a correctness argument, not luck. (Safer still: keep the mutable counter thread-confined — e.g., Zero-Even-Odd's router index lives only in the zero thread.)

---

## 3. Style 1 — Targeted semaphores ("doors")

**Mental model:** one closed door in front of each party (semaphore initialized to 0 = closed; 1 = open). `acquire()` = wait at the door *and the door re-closes behind you* (the permit is consumed — this is why the style is reusable with zero reset logic). `release()` = open a specific door.

**Initial permits:** exactly one door open — the party that goes first. Everything else 0. Total permits in the system = 1, forever. This **permit-conservation argument** is the whole exclusivity proof: every `release` is preceded by an `acquire`, so the total never exceeds 1.

### Skeleton A — chain (one-shot, "X before Y before Z")

```
sem afterA = 0, afterB = 0
A: work_A(); afterA.release()
B: afterA.acquire(); work_B(); afterB.release()
C: afterB.acquire(); work_C()
```
Note: the first party has no acquire, the last has no release. A chain is a baton that is never handed back — the degenerate (one-shot) case of the family.

### Skeleton B — ping-pong (fixed 2-party rotation)

```
sem meFirst = 1, other = 0
first  thread, n times:  meFirst.acquire(); act(); other.release()
second thread, n times:  other.acquire();  act(); meFirst.release()
```
Each thread's **last act enables the other's next turn**. Generalizes to a **ring** of T semaphores for a static T-cycle: all 0 except thread 0's = 1; thread k releases gate (k+1) mod T.

### Skeleton C — router (data-driven handoff, hub-and-spoke)

```
sem hubGate = 1, spokeGate[j] = 0 for each spoke role j
hub, for i = 1..n:
    hubGate.acquire(); act_hub()
    j = route(i)                 // decision uses data the hub owns
    spokeGate[j].release()
spoke j, for each of its turns:
    spokeGate[j].acquire(); act(); hubGate.release()
```
**Router placement rule:** the routing decision lives in the *one* thread that owns the deciding data (the hub's loop index). Spokes are dumb: wait, act, hand back. Two deciders = duplicated state = off-by-one races.

**Why separate semaphores per role, never one shared "spokeGate":** a semaphore cannot wake a *specific* waiter — any acquirer can grab the permit. One door per role is how you target. (This is the semaphore-world twin of the notify-vs-notifyAll problem.)

---

## 4. Style 2 — Shared-state condition loop ("the state is the signal")

**Mental model:** no separate signal object at all. One lock guards the state; each thread's right to proceed is **derivable from the state** by a per-thread guard predicate. Waking is broadcast; the predicate does the targeting.

### Skeleton D — the condition loop (memorize this shape cold)

```
lock L; int current = 1                       // plus turn token if needed
each thread, forever:
    synchronized (L):
        while (!myPredicate(current) && current <= LIMIT):
            L.wait()
        if (current > LIMIT): L.notifyAll(); return    // exit cascade — see §7
        act(current)
        current++                              // advance = hand off the baton
        L.notifyAll()
```

**The predicate partition — the entire correctness argument in one sentence:** the per-thread predicates must be **mutually exclusive and exhaustive** over reachable states — for every state, *exactly one* thread's predicate is true. Exclusive ⇒ one baton (exclusivity). Exhaustive ⇒ someone can always proceed (progress). Write the predicates down and check the tricky value (15 for FizzBuzz) before coding.

Examples of the partition:

| Problem | State | Predicates |
|---|---|---|
| Odd-Even | `current` | odd: `current % 2 == 1`; even: `== 0` |
| Print in Order | `stage` | each method: `stage == myTurn` (1/2/3) |
| FizzBuzz | `i` | four divisibility classes of i (3-only / 5-only / 15 / neither) |
| Round-Robin | `current` | thread k: `(current - 1) % T == k` |

Why this style needs no persistence trick: the **state variable is the persisted signal**. A thread arriving late reads the state under the lock, sees its predicate already true, and never waits. `notifyAll` only matters for threads *already asleep*.

---

## 5. Choosing the style

| Situation | Choose | Why |
|---|---|---|
| One-shot "X before Y" | Semaphore chain | 3 lines; nothing to reset |
| 2–3 parties, fixed rotation | Either; semaphores are elegant | ping-pong is 4 lines and self-explaining |
| Hub with data-driven routing, few roles | Semaphore router | targeting is explicit; hub owns the decision |
| Turn decided by *data*, symmetric roles (FizzBuzz) | Condition loop | a router would make one thread do double duty; predicates keep roles symmetric |
| **T is a runtime parameter** | Condition loop | semaphore pairs/rings multiply objects, init cases, and termination plumbing with T; the loop changes only the predicate |
| Interviewer says "use synchronized/wait/notify" | Condition loop | it's a monitor-idiom test — comply |
| Wakeup cost matters at large T | Semaphore ring | targeted wakeups: O(1) vs notifyAll's O(T) wasted wakeups per event |

The trade-off in one line: **semaphores target the wakeup but multiply the plumbing; the condition loop broadcasts the wakeup but the code never changes shape.** Naming this trade-off unprompted is the senior move — it is literally what N-Threads Round-Robin exists to test.

---

## 6. The derivation recipe (mechanical: problem statement → solution)

Run these steps in order. Each is 15–60 seconds.

1. **Write the target sequence by hand** for a tiny instance (n=2 or 3, T=3). E.g., Zero-Even-Odd n=3: `0 1 0 2 0 3`. This catches spec misreads (does fizz fire on 15?) before they become bugs.
2. **Label each event with its thread.** `0₍Z₎ 1₍O₎ 0₍Z₎ 2₍E₎ ...`. You now have the **turn function**: event index → required thread.
3. **Classify the baton route** from the labels: chain (each thread appears once, in order), fixed rotation (periodic pattern independent of values), or data-driven (which thread goes next depends on the value being emitted). Hybrids exist: Zero-Even-Odd is rotation on one axis (Z, number, Z, number...) and data-driven on the other (which number thread) — that hybrid *is* the router shape.
4. **Note one-shot vs repeated, and whether T is fixed or a parameter.** Ask the interviewer if unclear.
5. **Pick the style** from the §5 table. Say why in one sentence.
6. **Instantiate the skeleton:**
   - *Semaphores:* one gate per waiting-role; initial permits: 1 for whoever the hand-written sequence says goes first, 0 everywhere else; each thread's body = `acquire(my gate); act; release(next's gate)`, where "next" comes from the route (chain: successor once; rotation: the other/successor; router: computed by the hub, and spokes release the hub).
   - *Condition loop:* one lock + minimal state (counter; add a turn token only if the counter alone can't encode the turn — test: can you write each thread's predicate as a pure function of the counter? For Zero-Even-Odd you can't tell "zero's turn" from the counter alone, so add a token). Write **all** predicates; verify mutually exclusive + exhaustive on 3–4 concrete values including edge values.
7. **Add termination** (§7) — decide the exit condition, put the re-check after every wake, and add the exit broadcast/cascade.
8. **Verify with the family checklist:** permit-conservation or predicate-partition argument; walk one adversarial interleaving (worst thread scheduled first); walk the last two events plus shutdown; scan the failure catalog (§8).

Per-thread loop bounds fall out of step 2: count each thread's labels (odd thread: ⌈n/2⌉, even: ⌊n/2⌋). Getting these from the hand-written sequence, not from mental arithmetic, is what prevents the hang-after-correct-output bug.

---

## 7. Termination — where this family actually kills candidates

Finite sequences end. The failure mode: output is perfectly correct, then the program **hangs**, because some thread is still parked when the last printer walks away. Interviewers wait for this specifically.

**The rule: whoever finishes must free everyone still waiting.**

- **Condition loop:** the exit condition (`current > N`) must be (a) checked **under the lock**, (b) re-checked **after every wake** — any thread can be the one asleep when the sequence passes N — and (c) followed by `notifyAll()` **before returning**, so the wake cascades: each woken thread re-checks, sees `> N`, broadcasts again, exits. Skeleton D above bakes all three in. Checking `current > N` outside the lock races on the last number.
- **Semaphore ring/ping-pong:** the last printer must **still release the next gate** even though its own work is done. The released thread wakes, sees its loop is exhausted (or the counter past N), releases the *next* gate before exiting — an **exit cascade** around the ring. Dropping the "release even on the way out" step strands the neighbor. With per-thread loop bounds (FooBar: both loop exactly n), the cascade is implicit — each thread's loop just ends — but with a shared limit (round-robin ring, N % T ≠ 0) you must write it.
- **Chain (one-shot):** nothing to do — no thread waits after its single hop. One-shot designs get termination for free; that's part of why they don't generalize (a repeated Print-in-Order would need `stage` to wrap and the loop to bound — say this if asked the "make it reusable" follow-up).
- **Loop-bound termination (router style):** each spoke loops exactly its label-count from recipe step 2. Wrong bounds = a spoke blocked at its door forever after the hub exits. Test with both an odd and an even n.

---

## 8. Failure-mode catalog

Scan every one of these against your solution in Step 8 of the recipe. For each: the bug, the observable symptom, and the fix.

| # | Failure | What it is | Symptom | Fix / prevention |
|---|---|---|---|---|
| 1 | **Lost wakeup (transient signal)** | signal sent when no one is waiting yet, and the signal doesn't persist — bare `notify()` with no state check, or "flag set but reader already decided to sleep" done outside the lock | late-arriving thread sleeps forever even though its turn came | persist the signal: semaphore permit, or state variable read under the same lock in a `while` before waiting. In the monitor idiom, wait/state-check/state-change all under **one** lock makes lost wakeup impossible |
| 2 | **Lost wakeup (missing notify)** | state changed (flag flipped, counter bumped) but no `notifyAll` after | other thread sleeps forever *with the state in its favor* | mechanical rule: **every state mutation under the lock ends with `notifyAll()`** |
| 3 | **Wrong-party wakeup** | `notify()` wakes an arbitrary waiter whose predicate is false; or one shared semaphore for two different roles lets the wrong role grab the permit | woken thread re-checks, sleeps again; the eligible thread was never woken → all threads asleep, hang | `notifyAll` + while-loop (broadcast, predicate targets); or one semaphore **per role** (permits can't target waiters, doors can target roles) |
| 4 | **`if` instead of `while` around `wait()`** | spurious wakeups are legal (JLS), and wrong-party wakeups (#3) are common; an `if` acts on a predicate that may be false | wrong-parity print, double-print, or acting past N | **always `while`**. The while is not paranoia — with `notifyAll`, being woken when your predicate is false is the *normal* case |
| 5 | **notify vs notifyAll** | `notify` is only safe if *any* waiter can validly proceed — false whenever waiters have different predicates | FizzBuzz canonical hang: i=2 (number's turn), number thread bumps i, calls `notify`, JVM wakes fizz; fizz's predicate false, sleeps; number itself waits next; four threads asleep forever | default `notifyAll`; use `notify` only with a one-line proof (all waiters identical), and say the proof aloud |
| 6 | **Busy-wait** | spinning on a flag (`while (!flag);`) | burns a core; and if the flag isn't volatile/locked, the loop may **never see the write** (visibility) — two sins in one | blocking primitives only: `wait()` / `acquire()` park the thread; the OS wakes it. Spinning on a `volatile` is "technically correct, still a wrong answer" |
| 7 | **Broken permit conservation** | both semaphores initialized to 1, or a thread releases its **own** gate | both threads run at once (interleaved garbage), or one thread runs twice and the other starves | recompute total permits: must be exactly 1 at all times; every release opens the *other/next* door |
| 8 | **Termination hang** | missing exit broadcast / exit cascade / wrong loop bounds | correct full output, then hang | §7; test the last two events + shutdown, with N both multiple and non-multiple of T (or n odd and even) |
| 9 | **Predicate partition broken** | guards not mutually exclusive (fizz fires on 15) or not exhaustive (some i matches no one) | double-print / wrong print, or all threads waiting on a live state (hang) | recipe step 6: write all predicates, check exclusivity + exhaustiveness on concrete values including the overlap value |
| 10 | **Guarding one state with two locks** | "one lock per thread" | waits and notifies pass through different monitors — no mutual exclusion, no signal delivery | one shared state ⇒ one shared lock, always |

Narrating #5's exact interleaving on request is a standard follow-up — have it ready word for word.

---

## 9. Memory visibility — why you never need `volatile` here

The bugs above are *ordering* bugs; there is a second, quieter class: thread B simply **never sees** thread A's write (cached value, compiler hoisting). Both styles solve it for free, and you should say why:

- **Monitor (synchronized/wait):** unlock of a monitor *happens-before* every subsequent lock of the same monitor (JLS §17). Since every read and write of `current` is inside the same `synchronized`, each thread sees the latest value. Bonus: `wait()` releases the lock atomically with parking and reacquires it before returning, so the predicate re-check always reads fresh state.
- **Semaphore:** `java.util.concurrent` guarantees `release()` *happens-before* a subsequent successful `acquire()` of the same semaphore. So everything the releasing thread did before releasing — including its unguarded write to a shared counter — is visible to the acquirer. This is the precise reason the semaphore style may touch a shared counter without a lock: **the baton handoff is also the visibility handoff.**

Contrast: a plain (non-volatile) boolean flag with no lock has *neither* ordering nor visibility — the reader may spin forever on a stale value. That is failure #6's second sin, and the one-sentence answer to "why not just use a boolean?"

---

## 10. Validation against all problems

The §6 recipe applied to each problem, mechanically. (Recipe notes: validation forced two refinements now baked in above — the **chain** route as a first-class degenerate case with its no-first-acquire/no-last-release shape, prompted by Print in Order; and the **hybrid route ⇒ router** rule plus the "can the predicate be a pure function of the counter?" test for adding a turn token, prompted by Zero-Even-Odd. Re-validated below with those in.)

### Print in Order (LC 1114)
Steps 1–3: sequence `first second third`, one label each in order → **chain**. Step 4: one-shot, T=3 fixed. Step 5: semaphore chain (Skeleton A) — or condition loop with `stage` if asked for the monitor idiom. Step 7: chain ⇒ termination free. **Verdict: recipe produces exactly the two known solutions.** Miss found and fixed: original recipe assumed every baton returns (a cycle); the chain case is now explicit, including that the reusable follow-up needs `stage` reset/wrap.

### Print FooBar Alternately (LC 1115)
Steps 1–3: `foo bar foo bar` → **fixed 2-party rotation**. Step 4: repeated n times, T=2. Step 5: ping-pong (Skeleton B), `fooGate=1, barGate=0`; wait/notify version is Skeleton D with a `fooTurn` flag (predicate cannot be a pure function of a counter that doesn't exist — the flag *is* the state). Step 7: both loop exactly n → implicit cascade, nothing extra. Verify: permit conservation gives exclusivity + alternation. **Verdict: clean fit, both styles.** No recipe gaps.

### Print Zero Even Odd (LC 1116)
Steps 1–3: `0₍Z₎ 1₍O₎ 0₍Z₎ 2₍E₎ 0₍Z₎ 3₍O₎` → **hybrid**: rotation Z↔number, data-driven pick of the number thread ⇒ router (Skeleton C), zero = hub. Step 5: router placement rule puts the parity decision in the zero thread (it owns the loop index i, which stays thread-confined). Step 6 condition-loop alternative: predicate test says the counter alone can't distinguish "zero's turn" from "number's turn" → add a ZERO/ODD/EVEN token. Step 7: loop-bound termination — labels give odd ⌈n/2⌉, even ⌊n/2⌋; test odd and even n. **Verdict: recipe produces the strategy-file solution including the two things candidates miss (router placement, loop bounds).** Miss found and fixed: hybrid routes and the turn-token test weren't in the original recipe; both added.

### Odd-Even Printer
Steps 1–3: fixed rotation, but Step 4's interviewer constraint ("use synchronized/wait/notifyAll") forces the condition loop via the §5 table's last-but-one row. Step 6: predicates `current % 2 == 1` / `== 0` — counter alone encodes the turn, no token needed; partition trivially exclusive + exhaustive. Step 7: shared limit ⇒ full exit machinery (re-check after every wake + `notifyAll` before return). Failure scan flags exactly the strategy section's pitfalls (#4, #5, #8, #10). **Verdict: clean fit.** No recipe gaps.

### FizzBuzz Multithreaded (LC 1195)
Steps 1–3: labels depend on divisibility of i → **data-driven, symmetric roles** ⇒ condition loop per §5 (router rejected: the number thread would print *and* route — double duty; LC's 4 symmetric methods fight it). Step 6: four divisibility predicates; the exclusivity check on i=15 is precisely recipe step 6's "check the overlap value". Step 7: any of the four can be the last sleeper ⇒ exit broadcast mandatory. Failure #5 is this problem's signature bug, and the catalog has the narratable interleaving. **Verdict: clean fit; the recipe's predicate-partition check and failure catalog cover the two classic mistakes.** No recipe gaps.

### N-Threads Round-Robin
Steps 1–3: rotation of period T — but Step 4: **T is a runtime parameter** ⇒ §5 table row: condition loop; predicate `(current-1) % T == myId` (verify the mod mapping by hand-writing T=3, N=7 — recipe step 1 again). Semaphore ring named as the large-T/wakeup-cost alternative, with the §7 exit-cascade warning (last printer still releases next; cascade rounds the ring). Step 7: N % T ≠ 0 is exactly failure #8's test case. **Verdict: recipe produces the counter solution, the trade-off discussion the question exists to test, and the ring's termination trap.** No recipe gaps.

**Summary: 6/6 validate.** Two recipe refinements were made during validation (chain as degenerate route; hybrid-route/router rule + turn-token test) and are incorporated above; re-running all six against the final recipe produces the strategy-file solutions with no awkward fits.

---

## What the general framework leaves out

The 5-step framework classifies this family correctly (Type A → the Little Book of Semaphores patterns 1/2) and Templates 1/2 are the right seeds. For this category specifically, it leaves five gaps — all covered in this playbook:

1. **No style-selection rule.** The framework offers Template 1 (condition loop) and Template 2 (semaphore signaling) but no criterion for choosing. The real decision — targeted wakeups vs uniform code, and "parameterized T ⇒ condition loop" — is the crux of Round-Robin and FizzBuzz (§5).
2. **Template 2 is one-shot only.** It shows the single `release/acquire` pair (Print in Order's chain) but not the ping-pong loop, ring, or router shapes that reusable ordering problems need (§3 Skeletons B/C).
3. **Type A → pattern 1/2 under-specifies data-driven turns.** FizzBuzz and Round-Robin are Type A but are best solved with Template 1 and a predicate partition, not pure signaling; the mapping table would steer you to semaphores. The route taxonomy (chain / rotation / data-driven) is the missing sub-classification (§1).
4. **Normal-completion termination is absent.** Step 5's checklist covers cancellation/interruption but not the finite-sequence shutdown that dominates this family: the final `notifyAll` before return, the semaphore exit cascade, N % T ≠ 0 loop bounds (§7). This is the single most common failure point in the category.
5. **The predicate-partition correctness argument is missing.** "Guards must be mutually exclusive and exhaustive" is the two-sentence proof for every multi-role condition-loop problem; the framework's invariant step gestures at it but never states the check (§4).

Minor, not a gap: the framework's verification checklist (while-around-wait, notifyAll-after-change, lost wakeup) covers failure modes #1–#5 well; this playbook only extends it with the family-specific ones (#7–#9).
