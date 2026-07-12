---
layout: post
title: State Variation Playbook
date: 2026-07-12
description: The three tiers of state modeling, bare if-checks vs transition tables vs full State classes, and how to keep transitions atomic under concurrency.
categories: interview lld patterns
---

Deep dive on the State variation type, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). Applies whenever the variation axis of the problem lives in an entity's lifecycle rather than in a swappable algorithm.

## 1. When State is the answer

Triggers, in rising order of strength:

1. **Lifecycle invariants**, "order can only be cancelled before shipping", "an attempt gets exactly one final submission." The entity has a status field and rules about what's legal in each status.
2. **Illegal-operation-per-state**, the same API call must be rejected or ignored depending on where the entity is: `dispense()` before payment, `capture()` on a FAILED intent.
3. **Per-state behavior differences**, the same call does genuinely *different work* per state: `insertCoin()` in IDLE starts a session, in HAS_MONEY accumulates, in DISPENSING is rejected. This is the only trigger that earns full state classes.

**The distinction that scores at senior level**, there are three implementations, not one, and most candidates only know the GoF one:

| Tier | What it is | When | Canonical |
|---|---|---|---|
| **Bare status enum + if-checks** | `if (status != PLACED) throw` inside each service method | 2-3 states, one or two guarded methods, no follow-up expected | Simple terminal-state checks |
| **Enum + transition table** | Legality is *data*: `Map<StateEvent, State>` consulted by one `transition()` method | Many states/events but the *work* per event is the same shape, validate, flip, log, notify. Most order/payment/booking lifecycles | Payment gateway, task planner with per-type tables |
| **Full State pattern (classes)** | Interface + one class per state; each state class implements the whole operation set; illegal ops rejected *by type* | Behavior, not just legality, differs per state, and the operation set is wide | Vending machine, ATM session |

**Decision rule (say it out loud):** *use state CLASSES when behavior differs per state; use a TRANSITION TABLE when only legality differs; use bare if-checks when there are ≤3 states and one guard.* The senior move is naming which tier you chose and why: "the order lifecycle has seven states but every transition does the same thing, validate, set, emit event, so a data-driven table beats seven classes; the vending machine is the opposite, so there I'd write state classes."

Two special cases the tiers don't cover, fold them into your narration:
- **Parser FSMs** run per *character* in a hot loop. Enum + `switch` inside the loop; do NOT objectify states or build a table of lambdas. Enumerating the states out loud IS the answer there.
- **Multiple concurrent state variables** (a workflow's run state AND per-step states; an elevator's CarState AND Direction). Don't force one mega-machine; keep orthogonal machines separate and state which invariant links them.

## 2. Low-level mechanics (Java)

**Enum-with-methods**, the underrated middle ground. Java enums take abstract methods, so you get per-state behavior *without* extra classes:

```java
enum MachineState {
    IDLE { ... }, HAS_MONEY { ... }, DISPENSING { ... };
    abstract MachineState insertCoin(VendingMachine ctx, Coin c);
    abstract MachineState selectItem(VendingMachine ctx, String slot);
}
```
Good when per-state behavior is short. It fuses definition and behavior, so it can't do the definition/instance split, fine for singleton devices, wrong for an FSM library.

**State-class approach**, interface + per-state classes:

```java
interface MachineState {
    MachineState insertCoin(VendingMachine ctx, Coin c);
    MachineState selectItem(VendingMachine ctx, String slotId);
    MachineState dispense(VendingMachine ctx);
    MachineState cancel(VendingMachine ctx);
}
```
**Who owns the transition:** prefer *states return the next state* and the context assigns it (`this.state = state.insertCoin(this, c)`). The alternative, states calling `ctx.setState(next)` mid-method, works but scatters mutation and makes the atomic boundary harder to see. Either way the context owns the `state` field; states stay stateless (share singletons).

**Transition table**, legality as data:

```java
record StateEvent(OrderStatus from, Event event) {}
record Transition(OrderStatus to, Consumer<Order> action) {}
Map<StateEvent, Transition> table;                       // built once, immutable
OrderStatus fire(Order order, Event event);              // the ONLY method that writes status
```
One `fire()` method centralizes validation, mutation, logging, and observer emission. Adding a state or transition is a table entry, not a class.

**Definition vs instance split**, worth borrowing everywhere: the *definition* (states, transition table, guards, actions) is built once, then immutable and shared across all instances; each *instance* holds only mutable current state (+ context data). Immutable definition = free thread-safety; per-instance state = the only thing you synchronize.

```java
final class StateMachineDefinition { /* Map<StateEvent, Transition>, immutable */ }
final class MachineInstance { volatile State current; State fire(Event e); }
```

**Illegal-transition handling**, decide and state the policy, don't let it be accidental:
- *Business lifecycles* (payments, orders, bookings): **throw** a custom `IllegalStateTransitionException(from, event)`, silent ignores hide bugs and violate "every event recorded once."
- *Idempotent replays* (webhook vs poll delivering the same event): **ignore-if-already-there**, same-target transitions are no-ops, not errors.
- *Library/framework*: make it **configurable**, undefined-event policy as a strategy (THROW / IGNORE / LOG).

**Entry/exit hooks:** `onEnter(state)` / `onExit(state)` listeners plus per-transition actions. Keep hooks *outside* the mutation (fire observers after the state write commits), and treat **timers as just another event source**: a phase advance is a `ScheduledExecutor` tick that enqueues a `TIMER_ELAPSED` event, not a second code path that mutates state directly.

## 3. State + concurrency

**The transition-as-atomic-boundary rule:** the invariant "transitions follow the state machine" makes read-current-state → validate → write-next-state the smallest sequence that must be atomic. Two disciplines cover every problem in the bank:

**Discipline A, CAS / compute() on the state field** (many writers, cheap transitions):
- State in a map → `map.compute(id, (k, v) -> table-checked next)`, applies its transition table *inside* a single `compute()` per entity so races collapse.
- State on the object → `AtomicReference<State>` + `compareAndSet(expected, next)`. A circuit breaker is the textbook case: many threads observe failures simultaneously, only one CAS wins CLOSED→OPEN.
- **Competing writers, opposite intents:** a cancel-vs-take race, worker CASes QUEUED→PRINTING, cancel CASes QUEUED→CANCELLED, whoever wins the CAS wins, the loser sees the new state and backs off. No lock, no half-cancel.
- **Stale-actor protection:** when a delayed action might fire against a *newer* incarnation, **version-stamp** the state: the transition compares versions inside `compute()` and a stale token no-ops.

**Discipline B, single-writer state machine** (device controllers): one thread owns the machine; external commands *enqueue* instead of mutating. A traffic-signal scheduler thread advances phases, `requestPedestrian()` / `emergencyOverride()` put commands on a queue. The narration: "callers never touch the state field, they submit events; one writer means transitions are trivially atomic and the safety invariant can't race."

Choose A when transitions are short and writers are many (server-side lifecycles); choose B when the machine has its own thread of control anyway (devices, controllers). A coarse per-entity `synchronized transition()` is a legitimate first move, narrate the narrowing.

**Hooks under concurrency:** run observers/entry-actions *after* the atomic write, outside any lock, a listener that calls back into the machine while you hold its lock is a deadlock you built yourself.

## 4. Skeletons (signatures only)

**Enum + transition table** (order/payment/booking lifecycles):

```java
enum OrderStatus { CREATED, PAID, SHIPPED, DELIVERED, CANCELLED }
enum OrderEvent  { PAY, SHIP, DELIVER, CANCEL }
record Transition(OrderStatus to, Consumer<Order> action) {}

final class OrderStateMachine {                 // shared, immutable definition
    private final Map<StateEvent, Transition> table;
    OrderStatus fire(Order order, OrderEvent e);          // throws IllegalStateTransitionException
    boolean canFire(OrderStatus from, OrderEvent e);
}
final class OrderService {
    void ship(String orderId);                  // orders.compute(id, ..) -> machine.fire(..)
}
```

**State classes** (vending machine / ATM session):

```java
interface MachineState {
    MachineState insertCoin(VendingMachine ctx, Coin c);
    MachineState selectItem(VendingMachine ctx, String slotId);
    MachineState dispense(VendingMachine ctx);
    MachineState cancel(VendingMachine ctx);
}
final class IdleState implements MachineState { ... }        // stateless -> singletons
final class HasMoneyState implements MachineState { ... }
final class DispensingState implements MachineState { ... }

final class VendingMachine {                     // context owns the field
    private MachineState state;                  // = IdleState.INSTANCE
    void insertCoin(Coin c);                     // state = state.insertCoin(this, c)
    void selectItem(String slotId);
    Item dispense();
}
```

Same public API either way, that's the point. You can start with the table and refactor to classes if the interviewer pushes per-state behavior, not vice versa.

## 5. Anti-signals

- **State classes for a 3-state lifecycle where a table would do.** Seven files of `PlacedState`, `PaidState`, `ShippedState` whose methods all read "validate, set status, return" is ceremony, the behavior doesn't differ, only legality does. Correctly *declining* full State is the same senior signal.
- **If-else chains on status scattered across services**, `if (order.getStatus() == PLACED || order.getStatus() == CONFIRMED)` copy-pasted into cancel, amend, refund, notify. Legality lives in ONE place (`fire()` or the state class), every service goes through it.
- **Transitions not guarded under concurrency**, get-status, check, set-status as three separate steps on a shared entity. Naming the pattern and then racing the transition fails the bar harder than skipping the pattern.
- **States mutating context fields ad hoc**, state classes reaching into the context and setting other fields plus calling `setState()` mid-method; keep mutation at one assignment point.
- **Enum-ordinal transition "validation"**, `next.ordinal() == current.ordinal() + 1`, breaks the moment the graph branches (cancel, return, retry).
- **Observer callbacks inside the transition lock.**
