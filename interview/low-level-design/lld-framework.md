---
layout: default
title: What do you actually do in a LLD Interview?
---

# What do you actually do in a LLD Interview?

If you're starting out, or still prefer writing code the harder way without AI, this is for you.

There was a time we all coded by hand. With the flood of candidates on the job market, tech companies added a new round called the "Low Level Design Round," where you're expected to write things by hand under a 60-90 minute constraint. As the hairs have started turning grey and the bald patch growing bigger, I've experienced a lot of LLD rounds. Aced some, got rejected in some, and have also sat on the other side of the table. Experience is the only tool under my belt, and over time I've drafted all my learnings here. I treat this doc as my source of truth, something I can come back to when I have doubts.

## What is it

The idea of an LLD round is to gauge how well you code for production business services. It's an indirect translation of a small, direct business requirement into code, something DSA fails to materialize with. How well are you able to code out the requirement, how well you're able to encapsulate all the features, making sure your code is extensible for future requirements, testable, etc. Unlike DSA, which is something you rarely use in day-to-day work, this is something you're supposed to do on a daily basis in a non-agentic world. So it's really crucial to nail it, not just from an interview point of view but also from a point of honing your craft.

## What this document captures

A repeatable framework for you to follow. Unfortunately, an interview is not an environment where you can embrace the randomness of a situation and let your mind explore, so it's really important to have a structured approach. If this guide doesn't click for you, remember what works for me might not work for you. Focus on finding your own structured approach instead. Let's get started.

## 1. Components of a Problem Statment

Every LLD question, no matter how tough, can be broken down into the same 6 things:

- **ENTITIES** → what nouns exist (Ride, Slot, Order, Cell)
- **STATES** → what lifecycle each entity has (CREATED → MATCHED → COMPLETED)
- **BEHAVIORS** → what verbs the system exposes (book(), match(), evict())
- **VARIATION** → what will the interviewer ask you to swap? (pricing, eviction, matching)
- **STORAGE** → in-memory maps that index entities for the required queries
- **CONCURRENCY** → which operations race, and on what shared state

Two additions matter most at Senior+ levels:
- INVARIANTS: for each entity, state the rules that must never break ("a slot holds at most one vehicle", "balances across a transfer sum to zero"), they drive your validation code AND your concurrency boundaries. 
- VARIATION: predict the follow-up ("now add LFU eviction", "now add surge pricing") and put an interface exactly there. But neither substitutes for the real bar: correct working code, sound judgment, and clear narration of trade-offs. A pattern-rich design that violates an invariant fails; a plain design that runs and respects them passes.

## 2. Your folder structure

The goal of this round is to project yourself as a Object Oriented Programmer and not as a CRUD Developer. I still see engineers trying to carry the MVC model in interviews. Remember, in a 60-min round it costs you time to write a Controller.

- Drop controllers. I wish someone would have told me this earlier. In machine coding there is no HTTP. A Main/Demo class that runs your scenario IS the controller. Interviewers want working code + a driver.

- Add VARIATION PACKAGE. This is where the predicted follow-up lands, and it's the package most interviewee's current structure misses. The name depends on where the variation actually lives in the problem.

| Where variation lives | Package | Example problems |
|---|---|---|
| Swappable algorithms | strategies/ | Pricing, matching, eviction, ranking, backoff |
| Lifecycle behavior per state | states/ | Vending machine, elevator, circuit breaker |
| Undoable/queued operations | commands/ | Text editor, home automation |
| Configurable rule chains | rules/ | Coupon eligibility, fraud checks, access policies |
| Pure data (no package) | config/tables | Vending recipes, tax slabs, fee schedules |
| The data structure itself | none | Limit order book, bloom filter, median store, parsers |

```
src/
├── models/          # Entities + Enums. Plain classes with behavior. No interfaces here.
│   ├── Ride.java, Driver.java, Location.java
│   └── enums/ RideStatus.java, VehicleType.java
├── strategies/      # (or states/ commands/ rules/ — the variation package, per table above)
│   ├── matching/    DriverMatchingStrategy (interface) → NearestDriverStrategy, RatingBasedStrategy
│   └── pricing/     PricingStrategy (interface) → BasePricing, SurgePricing
├── services/        # Business logic. Usually a CONCRETE class — see interface rule below.
│   └── RideService.java
├── repositories/    # In-memory stores. RideRepository (interface) → InMemoryRideRepository.
├── exceptions/      # 2-3 custom exceptions (SlotUnavailableException). Cheap, high signal.
├── factories/       # Only if creation logic is non-trivial (VehicleFactory).
└── Main.java        # Demo driver: wires dependencies, runs the scenario.
```

Interface rule: I see a lot of candidates end up creating interfaces for each non-entity class out there. While I like the strategy and sometimes I follow it too, it eats up small chunks of your time. So a better idea is to add them only at real substitution. 
- Strategies: always (multiple implementations are the point). 
- Repositories: usually (in-memory today, DB tomorrow is a credible swap, and it aids testing). 
Services: usually NOT as we generally have one implementation, no seam; a concrete RideService is fine, extract an interface only when a second implementation appears. 

Java naming: no I prefix. The interface gets the good name (RideService, PricingStrategy); implementations get descriptive names (InMemoryRideRepository, SurgePricing, or DefaultRideService as last resort). Keep interface and implementations in the same package, no interface/impl subfolders.

Other rules: constructor injection everywhere (no new inside services). Models get behavior (ride.canBeCancelled()), don't make them anemic bags of getters.

## 2. The 7-step method (60-minute round)

**Step 1: Clarify & scope (5 min)**

First, confirm the round format: "Do you want fully working, runnable code, or design discussion with class diagrams and key methods?" Machine coding and design-discussion LLD have different winning moves: working code vs breadth of design reasoning. Then ask, in this order:

- "What are the 3-4 core operations you want working?" (lock scope as early as you can)
- "Single-threaded or should I handle concurrency?" (For SDE 2+ levels, assume a Yes.)
- "In-memory is fine, right?" (always yes but you will be seen as someone who clarifies requirements, interviewerd dig that)
- "Any extensions that we should plan for ?" (you don't want to write soemthing which cannot be extended for future scopes)

Then say the scope Out Loud: "I'll build X, Y, Z; I'm explicitly skipping things not in scope like auth, payments, DB persistence."

**Step 2: Entities, invariants & enums (5 min)**

List nouns → classes.

Adjectives-with-fixed-values → enums.

Lifecycles → status enums.

Don't stop at nouns, for each entity note who owns whom (a Floor owns its Slots; a Trip references a Driver) and 2-3 invariants ("one active trip per driver", "seat can't be booked twice"). Invariants become your validation checks and, later, your lock boundaries. Write this as a comment block first, code fast.

IDs are String via UUID.randomUUID(); timestamps long or Instant.

**Step 3: Name the variation axis (2 min, out loud)**

"The thing most likely to change here is ___ (pricing / eviction / matching / notification channel / discount rules), so I'll put that behind a Strategy interface." And if the problem has no swappable algorithm, say THAT: "the variation here lives in the states / the rules / the data, so I'll use State / a rule chain / a config table instead", correctly declining Strategy is the same signal.

**Step 4: Service interfaces (3 min)**

Write the service class skeleton with the 3-4 operations from Step 1.

Method signatures = your API contract.

Return domain objects, throw custom exceptions (never return null / boolean success flags). There are some exceptions though for example, a Delete API.

**Step 5: Code inside-out (30 min), THE ORDER MATTERS**

enums → models → exceptions → repository → strategies → service → Main

Never start with the service. Dependencies first means you never write code that doesn't compile. Get one end-to-end flow WORKING before adding the second feature. Write working minimal code first. Do a second pass for refactoring using patterns. A beautiful design that doesn't run fails the round; a running system you then refactor toward patterns passes it.

**Step 6: Concurrency pass (8 min)**

Do it as an explicit pass: "now let me make this thread-safe", narrating it earns the points even if you don't finish every lock.

**Step 7: Demo + extensibility pitch (5 min)**

Run your Main class. Then say something like: "To add [likely extension], I'd only add a new class implementing [interface X], nothing else changes."

When the variation is data-driven, use the data-shaped version instead: "New fiscal year / new recipe / new fee tier, our change is limited to a new data row, zero code changes." Close every round with one of these sentences.

## 3. Pattern selection: trigger table

These 8 patterns cover the vast majority of the questions asked:

| Keyword in the problem | Pattern | Sample questions |
|---|---|---|
| "support multiple X algorithms" / pricing / eviction / matching / payment methods | Strategy | Parking fee, LRU-vs-LFU, driver matching, Splitwise split types |
| "notify users when…" / subscribers / alerts / listeners | Observer | Stock alerts, auction outbid, notification service, config changes |
| entity has a lifecycle with rules per state | State | Vending machine, elevator, order lifecycle, ATM |
| complex object, many optional fields | Builder | Pizza order, ride request, search query |
| create objects by type token | Factory | Vehicle types, notification channels, piece types in chess |
| add features in layers | Decorator | Pizza toppings, coffee add-ons, logger enrichment |
| undo/redo, operation queue, audit log | Command | Text editor, remote control, transaction log |
| request tries handler after handler | Chain of Responsibility | ATM cash dispensing, logger levels, approval workflow, discount rules |
| variants differ only in VALUES, not behavior | No pattern, just a table/config | Tax slabs, vending recipes, fee schedules, transition tables |

Two rules of thumb worth remembering:

- **Rule chains**: default to a flat rule LIST evaluated by an engine, not a linked Chain of Responsibility. Only reach for linked handlers when each one actually consumes or escalates the request, like ATM cash denominations or a multi-level approval workflow.
- **Strategy**: it comes in three shapes, a comparator cascade (rank candidates, e.g. driver matching by distance then rating), a first-success cascade (try each until one works, e.g. payment gateway fallback, coupon eligibility checks), or a contributor list (combine results from all of them, e.g. total pricing = base fare + surge + taxes, each a separate strategy).

**Note on Singleton**: good to have, but it costs time. Do it when the problem actually needs it, or when you have ample time to spare. Otherwise just instantiate once in Main, and mention thread-safe lazy initialization only if asked.

Where it's actually needed: a ParkingLot in the Parking Lot problem, only one lot exists, every Level/Slot/Ticket must resolve against the same instance, and if two threads race to construct it you'd end up with two lots silently tracking separate slot states. Same shape shows up for a shared IDGenerator or a Logger, one physical resource, accessed from many services, where a second instance is a correctness bug, not just waste.

## 4. Concurrency playbook (this is the Uber L5 differentiator)

Senior engineer machine-coding round expects thread-safe code.

Start from invariants, not from tools. The method:

(1) restate the invariant ("a slot holds at most one vehicle")

(2) find the smallest sequence of reads+writes that must be atomic to preserve it, that's your atomic boundary

(3) pick the cheapest primitive that covers exactly that boundary. Then choose from this menu:

- **Storage**: ConcurrentHashMap for repositories. Note - this makes individual map operations safe, not your workflow. Two safe gets followed by a put is still a race condition to solve.
- **Single-key check-then-act** ("claim this exact slot ID if free"): atomic map ops, putIfAbsent, computeIfAbsent, compute. Powerful, but only atomic per key. It does NOT cover conditions spanning multiple keys or a range, e.g. interval-overlap booking ("is any meeting overlapping 2-3pm?") can't be solved with putIfAbsent; lock the resource's calendar object instead.
- **Multi-entity invariants** (transfer A→B, multi-seat booking): per-entity ReentrantLock; acquire in sorted ID order to prevent deadlock. Call it out loud: "I lock in a globally consistent order."
- **Counters/metrics**: Use AtomicInteger / AtomicLong / LongAdder.
- **Producer-consumer** (queues, schedulers, elevators): BlockingQueue (ArrayBlockingQueue; DelayQueue for TTL expiry).
- **Read-heavy config/tables/rule lists**: volatile reference swap of a whole immutable object (never mutate in place), or ReadWriteLock. The same idea scales up as seal-and-freeze: append to a mutable current segment, seal it immutable, then read lock-free (logs, search indexes, heatmap windows).
- **Per-entity ordering without locks**: one queue + one consumer per entity (per-device commands, per-conversation messages, per-symbol order book). Ordering comes from the single consumer, not from locking, say that.
- **Pick-then-claim retry**: when a strategy picks a candidate from a snapshot (driver, agent, worker), claim it atomically (compute() on its status) and re-pick if you lost the race. The pick can be stale; the claim cannot.

On coarse synchronized: a synchronized service method is not automatically wrong, it's correct, simple, and a legitimate first move under time pressure. The L5 move is to use it, say "this is correct but serializes all bookings; if the interviewer cares about throughput I'd narrow it to a per-resource lock", and narrow it only if asked or if time permits. Wrong is silent coarse locking with no awareness of the trade-off, or fine-grained locking that breaks an invariant.

Genuine anti-signals: Collections.synchronizedMap (CHM exists), sprinkling synchronized without naming the race it prevents, ignoring concurrency until prompted.

Narrate every choice: "booking a specific slot is single-key check-then-act, so compute() on the slot map covers the invariant."

## 5. Time budget (60 min) and L4→L5 signal map

| Min | Activity |
|---|---|
| 0-5 | Clarify + scope declaration |
| 5-12 | Entities, enums, variation axis named |
| 12-15 | Service interfaces |
| 15-45 | Code inside-out; ONE flow working by min 35 |
| 45-52 | Concurrency pass |
| 52-58 | Demo in Main + edge cases (invalid input, double-booking, not-found) |
| 58-60 | Extensibility pitch |

If you get 90 minutes instead: keep the same proportions, spend the extra ~30 min on a second working flow, a real concurrency test in Main (spawn threads, assert the invariant held), and 2-3 quick edge-case checks. Don't spend it on more patterns.

| Dimension | Mid Level answer | Senior Level answer |
|---|---|---|
| Scope | Builds what's asked | Declares what's out of scope and why |
| Patterns | Names them | Places one interface exactly at the variation axis, rejects unnecessary ones |
| Concurrency | Adds synchronized when prompted | Proactively identifies the race, picks fine-grained primitive, explains deadlock avoidance |
| Follow-ups | Rewrites code | Extension = new class only, no edits to existing code (Open/Closed in action) |
| Communication | Codes silently | Narrates trade-offs continuously |


Preparation plan: do 2 problems per archetype deeply (20 total, marked ★ in the question bank) rather than 200 shallowly, plus the two Track B concurrency drills first. For each ★: full code in your structure, timed at 60 min, concurrency pass included, then one self-inflicted follow-up.

