---
layout: post
title: Strategy Variation Playbook
date: 2026-07-12
description: When to reach for Strategy, how to shape it in Java, and the three sub-shapes candidates conflate — comparator cascade, first-success cascade, contributor list.
categories: interview lld patterns
---

Deep dive on the Strategy variation type, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). This is the largest variation type in the bank, roughly half of all LLD problems have a swappable algorithm as their primary variation axis. Master this and you have a default move for nearly half the questions you'll get asked.

## 1. When Strategy is the answer

**Triggers.** Put an interface at the axis when ALL three hold:
1. **Same question, different algorithm.** The system asks one question ("what does this ride cost?", "which spot?", "evict whom?", "in what order?") and plausible answers differ in *logic*, not just numbers.
2. **The variation is a verb on the service, not the identity of an entity.** Pricing varies per policy, not per Ride. If behavior varies by *what the object is*, use polymorphism on the entity instead.
3. **The interviewer will ask for variant #2.** Pricing, matching, eviction, ranking, backoff, scoring, allocation, routing, expiry, split types, these words in the prompt are near-guarantees.

Say it out loud: "The axis here is X, so X goes behind a strategy interface."

**Non-triggers, decline Strategy explicitly, it scores the same:**

| Situation | Use instead | Example |
|---|---|---|
| Behavior varies by entity identity/type | Polymorphism on the entity | Chess: `piece.canMove(from, to)` on the Piece hierarchy, not a MovementStrategy, the movement never gets swapped for a given piece. |
| Behavior varies by lifecycle phase | State | Vending machine, circuit breaker. Note both can coexist: elevator uses State for the car AND Strategy for dispatch, name each axis separately. |
| Independent boolean gates that accumulate or veto | Chain of Responsibility / rule list | Coupon eligibility, fraud checks, review filters. Chain when each handler may decline and pass on; Strategy when exactly one algorithm produces the whole answer. |
| Variants differ only in parameters | One parameterized class + config data | Tax slabs, coffee recipes ("new beverage = data not code"), delivery-assignment weights. The test: would variant #2 be a new `if` or a new number? New number → data. |
| The algorithm IS the whole problem and never swaps | No interface, just write it | Limit order book, regex matcher, LCS diff core. |

**The LRU rule:** if only one implementation exists *today*, don't build the interface, build the concrete thing and extract `EvictionPolicy` the moment LFU is requested. Pre-building a one-impl interface is an anti-signal. Exception: when the prompt itself names multiple variants ("support LRU and LFU"), the interface is day-one scope.

## 2. Low-level mechanics (Java)

**Naming and shape.** No `I` prefix; the interface gets the good name, implementations get descriptive names: `PricingStrategy` → `BasePricing`, `SurgePricing`; `EvictionPolicy` → `LruEviction`, `LfuEviction`; `BackoffPolicy` → `ExponentialBackoff`. One method is normal; multi-method strategies are fine when the axis needs them.

**Signature discipline: candidates in, decision out.** Strategies are pure functions of their arguments, the *service* queries repositories and passes the candidate set in: `Spot select(List<Spot> freeSpots, Vehicle v)`, `Agent pick(List<Agent> available, Order o)`. No repository fields inside strategies. This keeps them trivially testable and thread-safe.

**Package.** `strategies/`; with two-plus axes use subpackages: `strategies/pricing/`, `strategies/matching/`. Interface and impls in the same package, no interface/impl subfolders.

**One interface per axis.** Parking-lot has allocation AND pricing; ride-sharing has matching AND pricing. Never merge two axes into one fat interface, you'd force every pricing variant to reimplement matching.

**Three injection points, know which the problem needs:**
1. **Constructor injection** (default): policy fixed for the service's lifetime. `new RideService(repo, matching, pricing)`.
2. **Volatile field + setter**: runtime swap under load (below).
3. **Per-call selection**: the *caller* picks per request, or the service looks it up in a registry keyed by enum: `Map<SplitType, SplitStrategy>`, `Map<EventType, FeeRule>`. A registry map IS the factory here; a separate Factory class is overkill.

**Stateless vs stateful.** Default: stateless and immutable (all fields `final`, set in constructor), then one instance is freely shared across threads with zero locking. When the algorithm genuinely needs state, you have two designs:
- **State inside the strategy, guarded per key**: a rate limiter keeps `ConcurrentHashMap<clientId, BucketState>` and does refill-and-consume inside `compute()`, atomic per client, scales because contention is per-key. If a strategy holds state, that state needs its own guard, the strategy's mutability is now your concurrency problem, name it.
- **State on the entity, strategy stateless**, required for runtime swap, next.

**Runtime swapping.** Swapping an eviction policy on a live cache forces the key design decision: *entries carry raw, policy-agnostic metadata* (last-access timestamp, access count, insert time) so ANY policy can rebuild its private index (recency list for LRU, freq buckets for LFU) from the entries on swap. Mechanics: hold the strategy in a `volatile` field; on swap, take the write side of a `ReadWriteLock` (ops take read side, so gets/puts stay cheap), rebuild the new policy's index from entry metadata, then flip the reference. If you skip the rebuild, the new policy starts blind and evicts garbage, narrating this rebuild IS the question.

**Resource-owning strategies need a lifecycle.** If an implementation owns a thread/queue (a background sweeper, a log appender holding a file handle), give the interface `start()`/`close()` (or extend `AutoCloseable`) and shut the old one down on swap.

**Strategy + atomic claim (dispatch/booking problems).** The strategy picks a candidate from a snapshot; by the time you claim it, someone else may have won. Never lock the whole pool around the pick. Loop: pick from snapshot → try to claim atomically (`compute` on the agent's load / `putIfAbsent` on the spot) → on failure, remove candidate and re-pick.

**Injectable clock and RNG are degenerate strategies.** A dice roll, a slot machine's RNG, a rate limiter's clock: inject them via constructor so `Main` can pass a fixed seed / fake clock and the demo is deterministic. Say "I'm injecting the dice so I can test."

## 3. Skeletons

```java
// strategies/pricing/PricingStrategy.java — interface gets the good name
public interface PricingStrategy {
    Money price(Trip trip, RateCard rates);   // pure: candidates/inputs in, decision out
}

// strategies/pricing/SurgePricing.java — stateless: final fields only
public class SurgePricing implements PricingStrategy {
    private final PricingStrategy base;       // composes the base policy
    private final SurgeIndex surgeIndex;
    private final double surgeCap;
    public SurgePricing(PricingStrategy base, SurgeIndex surgeIndex, double surgeCap) { ... }
    @Override public Money price(Trip trip, RateCard rates) { ... }
}
```

```java
// services/RideService.java — constructor injection; swap-able axis is volatile
public class RideService {
    private final RideRepository rides;
    private final DriverMatchingStrategy matching;   // fixed for lifetime
    private volatile PricingStrategy pricing;        // runtime-swappable
    public RideService(RideRepository rides, DriverMatchingStrategy matching, PricingStrategy pricing) { ... }
    public void setPricingStrategy(PricingStrategy p) { ... }  // + rebuild/close if stateful
}
```

```java
// Main.java — the swap demo: same call, new behavior, no service edits
RideService svc = new RideService(repo, new NearestDriverStrategy(), new BasePricing());
svc.requestRide(riderA, locX);                                  // flow 1: base pricing
svc.setPricingStrategy(new SurgePricing(new BasePricing(), surgeIdx, 2.0));
svc.requestRide(riderB, locX);                                  // flow 2: surge — narrate the diff
```

Close with: "Adding time-of-day pricing is one new `PricingStrategy` class, nothing else changes."

## 4. The comparator-chain special case (and its two siblings)

**Ordered comparator composition**, for rule-cascade *ranking*: "prefer same artist, then same genre, then newer." Each rule is one small `Comparator<Song>`; the cascade is their ordered composition:

```java
Comparator<Song> cascade = byArtistMatch(profile)
        .thenComparing(byGenreMatch(profile))
        .thenComparing(byReleaseRecency());
// or from a configurable list:
Comparator<Song> cascade = rules.stream().reduce(Comparator::thenComparing).orElseThrow();
```

New rule (language match) = one comparator + its position, pure Open/Closed, no touch to the ranker. Use for: feed ranking, sort modes (hot/new/top as named comparators picked per request), priority queues (the comparator IS the scheduling strategy fed to `PriorityQueue`).

**Sibling 1, first-success cascade** (tiered *matching*, not ordering): an ordered `List<MatchRule>`, try each, return the first non-empty result. Canonical: payment reconciliation (exact-id → amount+date-window → fuzzy). Shape: `Optional<Match> match(Txn txn, List<BankRecord> candidates)` per rule; the service loops. This is Strategy-flavored Chain, say "ordered rule tiers" and nobody will quibble about the pattern name.

**Sibling 2, contributor list** (additive rules): every strategy in the list contributes a piece, results are summed/merged: payroll components, fantasy-sports scoring rules, recommendation candidate-sources + scorers. Shape: `List<PayComponent>`, each `Money compute(Employee, PayPeriod)`, service folds. New rule = append to the list.

Name which of the three shapes you're using, "this is a comparator cascade, not a chain" is a free senior signal.

## 5. Anti-signals

- **One-impl interface / strategy folder with one class.** Create the package only with real contents. Extract on the second variant unless the prompt names variant #2 up front.
- **Strategy-for-everything.** Lifecycle → State; per-entity behavior → polymorphism; veto gates → Chain; parameter-only variants → config. Forcing chess movement or vending-machine states into `strategies/` reads as one-hammer design.
- **N classes differing only by a constant.** `WeekdayPricing`/`WeekendPricing` that differ in one multiplier should be one class + a rate table.
- **Merged axes.** One `ParkingStrategy` doing both allocation and pricing, every new pricing variant drags allocation along.
- **Silent shared state.** A "strategy" with a mutable `HashMap` field shared across threads and no guard. Stateless or guarded, nothing in between.
- **Forgetting to name the axis out loud.** The interface without the sentence "the variation axis here is X" earns half the credit. Also name the NON-axes: "recipes are data, not a strategy."
- **Locking the world around pick-then-claim** instead of the retry loop, correct but serializes the hottest path; at minimum narrate the trade-off.
