---
layout: post
title: The Barbershop / Sleeping Barber (LBoS ch. 5)
date: 2026-07-19
description: >-
  Combines three things you know into one design: a bounded counter with BALKING (reject, don't block: your first fail-fast design), sleep/wake signaling (the sleeping barber…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Little Book of Semaphores ch. 5; Dijkstra's classic. Worth doing: asked at OS-heavy shops.

### Problem

A barbershop has one barber, one barber chair, and a waiting room with n chairs. Customers arrive on their own threads:

- If the shop is full (n waiting + 1 in the chair), the customer leaves immediately (balks).
- Otherwise they wait. The barber sleeps when no customers are around; an arriving customer wakes him.
- The barber cuts one customer's hair at a time; cut done, customer leaves, barber takes the next waiter or sleeps.

### Constraints

- The customer count check and the decision to stay/leave must be atomic (no two customers taking the last chair).
- Barber and customer must rendezvous: the cut starts only when both are ready, and both know when it's done.

### Clarify before solving

- Balk (leave) when full, not block, different from the blocking queue!
- FIFO service required? (Base version: no. Know it's a variant, Hilzer's.)

### Why this problem matters

Combines three things you know into one design: a bounded counter with BALKING (reject, don't block, your first fail-fast design), sleep/wake signaling (the sleeping barber IS a parked consumer), and a two-way rendezvous (cut start + cut end). Real-world shape: a server with a bounded request queue that sheds load when full.

---

## Strategy

### Classify

Bounded resource with balking + rendezvous. The barber is a consumer of customers; customers are both producers (of themselves) and rendezvous partners.

### Invariant

customers-in-shop ≤ n+1; a customer either enters and is eventually served, or balks immediately; barber cuts exactly one customer at a time; each cut is bracketed by both parties (starts when both ready, both observe completion).

### Mental model

Deli counter with limited standing room: bouncer at the door counts heads (atomic check-and-enter or leave), sleeping clerk woken by the door chime (semaphore signal, persists even if the clerk was mid-cleanup, remember Print in Order), and a two-way handshake per service: "I'm ready" / "you're done".

### Design

State: `waiting` counter + mutex; `customersReady = Semaphore(0)`; `barberReady = Semaphore(0)`; `cutDone = Semaphore(0)` (add `customerDone = Semaphore(0)` if you want the full symmetric handshake).

- Customer: mutex; if waiting == n+... (shop full) → mutex.release, LEAVE. Else waiting++; mutex.release; customersReady.release() (chime); barberReady.acquire() (wait for barber to be free for ME); get haircut; cutDone.acquire() (wait for finish).
- Barber loop: customersReady.acquire() (sleep until chime, a parked consumer, exactly like your blocking-queue consumer); mutex; waiting--; mutex.release; barberReady.release() (call next customer); cut hair; cutDone.release().

### What each piece teaches

1. **Balking**: the full-check and leave/enter decision under one mutex. Compare with the blocking queue: same guarded counter, different policy on "resource unavailable" (return vs wait). Blocking vs balking vs timeout are policies over identical guarded state. Internalize that and half of LLD concurrency questions become policy discussions.
2. **The sleeping barber = semaphore persistence.** If a customer chimes while the barber is finishing a cut, the permit sits there; barber's next acquire returns instantly. No lost wakeup possible. Contrast with a naive "if barber sleeping then wake him" flag protocol, which loses the wakeup when the chime lands mid-transition.
3. **Rendezvous per service**: barberReady/cutDone pair the two threads for one transaction. Without barberReady, two waiting customers could both think they're next; without cutDone, the customer walks out mid-cut.

### Pitfalls

1. Checking fullness without the mutex → two customers take the last chair (classic check-then-act).
2. Decrementing `waiting` in the customer thread instead of the barber → transient double-count lets the shop over-admit.
3. One semaphore doing double duty for "ready" both directions → barber can rendezvous with customer A's chime but cut customer B (mismatched pairing). Keep the handshake per-direction.
4. Forgetting the balk path releases the mutex before leaving → shop deadlocks at first full house.

### Check your understanding

1. Where exactly is the check-then-act window if the full-check is unlocked?
2. Why do we need BOTH customersReady and barberReady? What goes wrong with only the chime? (Two customers, one barber-slot: who's being cut?)
3. Express this shop as a bounded queue + one consumer + a rejection policy. Which JDK pieces would you assemble? (ArrayBlockingQueue.offer → balk on false + single worker + per-item handshake if the caller must wait for completion, that's a Future!)
4. FIFO variant: what breaks in the base design and what would you add? (Semaphores don't order waiters; you'd queue customer-specific gates, Hilzer's problem. Describe only.)

### Transfers to

Bounded server with load shedding, connection pools with fail-fast checkout, and "caller waits for completion" = Future/handshake patterns (bridges to Type F).
