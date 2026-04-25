---
layout: post
title: Data-Driven Variation Playbook
date: 2026-07-12
description: When a follow-up is answered by adding data instead of adding classes. The four rungs of config, the promotion path to Strategy, and hot-reload without locks.
categories: interview lld patterns
---

Deep dive on the data-driven variation type, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). Use when the follow-up is answered by ADDING DATA, not adding classes.

## 1. When data-driven is the answer

**The litmus test.** Predict the follow-up. If it's "now add another X", another beverage, another vehicle class, another tax slab, ask one question: *does the new X need new logic, or just new values fed through existing logic?* If every variant runs the SAME algorithm over different numbers/strings/mappings, X is a row in a table, not a class. Say it: "the variation here is data, not code, I'll model X as a config table, and the extension pitch becomes 'new X = one data entry, zero code changes.'" That sentence scores exactly as high as placing a Strategy interface does on a strategy problem, and higher than a wrong Strategy.

**The spectrum**, four rungs, ordered by *who* changes the values and *when*:

| Rung | Shape | Changer | Example |
|---|---|---|---|
| 1 | Enum constants with fields | Developer, recompile | `VehicleType.CAR(20)` toll fee |
| 2 | `Map`/`List` table constants seeded in `Main` | Developer, one edit | Board jump maps, recipe tables |
| 3 | Config rows (records) loaded + validated at startup | Ops, redeploy/restart | Tax slab file per fiscal year, fee schedules |
| 4 | Runtime-editable registry behind an admin API | Admin, live | Feature flags, restock/reprice |

In a 60-minute round, rung 2 is your default: seed the table in `Main` (or a small `Config` class) and narrate "in production this loads from a file, the table shape is identical, only the source changes." Climb to rung 3/4 only if the problem demands it. Building a file parser mid-round is a time sink unless parsing IS the question.

**When to STOP, the promotion path.** The moment two variants need genuinely different behavior, not just different numbers, data has hit its ceiling. Promote in this order:

1. **Value in a table**, variants differ in numbers. (Every beverage brews identically: check ingredients, deduct all-or-nothing, pour. A latte vs a mocha is purely a different `Map<Ingredient, Integer>`. Recipes STAY data forever.)
2. **Table row carries a key that selects a strategy**, variants share structure but one field picks behavior. (A fee schedule's `category × tier` rates are data, but a "first 10 listings free" promo is different. That needs a counter and a fee *rule* class keyed by event type, with the table just feeding it parameters.)
3. **Full Strategy**, variants ARE algorithms. (A flat rate table is data; surge pricing that multiplies by live demand is a `PricingStrategy`. Some problems make both moves at once: rate table data + congestion strategy.)

The judgment call the interviewer is probing: recipes never leave rung 1-2, but the ingredient-check *policy* (fail-fast vs wait-for-refill) is behavior → strategy. One problem, both answers, and you must draw the line out loud.

## 2. Low-level mechanics (Java)

**Table shapes, cheapest first:**

- **Enum with fields**, `enum VehicleType { CAR(20), BUS(50); final int fee; }`. Self-validating, exhaustive switches, zero infrastructure. Ceiling: values are compiled in.
- **`Map` constants**, `static final Map<VehicleType, Integer> RATES = Map.of(...)` or seeded via a `seed()` call in `Main`. `Map.of`/`Map.copyOf` give immutability free.
- **`EnumMap` keyed tables**, when the key is an enum, `EnumMap<Regime, List<SlabRow>>` is dense, ordered, and reads as documentation.
- **Record rows**, `record SlabRow(BigDecimal upTo, BigDecimal rate) {}`. Tax slabs are a `List<SlabRow>` sorted ascending; computing tax is one fold over the list per regime. Records give you `equals`/`hashCode`/immutability for free, the ideal config-row type.
- **Composite-key tables**, `record TransitionKey(State from, Event event) {}` → `Map<TransitionKey, State>`. This is the **FSM-as-data** form: a transition table `Map<(state, event), Transition>` inside an immutable `Definition` shared by many mutable instances. Adding a workflow state = table rows, no code.
- **The property map** (anti-subclassing). Naive design: a base type with subclasses per variant, each duplicating fields and forcing a new class per type. The lesson: those subclasses differ only in *attributes*, so replace the hierarchy with a `Spec` holding `Map<Property, Object>`. Searching becomes a map comparison that ignores unset keys, and a new type is now a data entry instead of a class. Rule of thumb: subclass when behavior differs, property-map when only attributes differ.
- **Effective-dated rows**, fee schedules, payroll revisions, time-banded rates: give rows `validFrom`/`validTo` and make lookup filter by date. Old invoices recompute against old rows, never overwrite history, append new rows.

**Seeding vs loading.** Seed in `Main` during the round; keep construction behind one factory method (`RateTable.of(rows)`) so "load from file" later touches one call site.

**Validate at load, fail fast.** Bad config must die at construction with a specific `InvalidConfigException`, never surface mid-request. Name the checks for your problem: slabs contiguous and ascending; board endpoints in range; transition targets are defined states. A validated-at-startup table is an invariant you never re-check on the hot path, say that.

**The pitch shape.** Close with: "New fiscal year = new slab file, zero code. New beverage = one recipe entry. New vehicle class = one rate row." This replaces "new class implementing interface X" for these problems.

## 3. Data-driven + concurrency

Tables are read-hot and write-rare, the easiest concurrency story in the framework if you say it right:

- **Immutable after load = thread-safe by construction.** `Map.copyOf`/`List.copyOf` into `final` fields gets you safe publication for free via final-field semantics. Readers run concurrently with no locks needed, there's nothing to narrate beyond "it's immutable, so it's free." This is the definition/instance split: one immutable shared `Definition`, per-instance mutable `current` guarded separately.
- **Hot reload = volatile swap of the WHOLE table, never mutate in place.** Build the new table off to the side, validate it (fail fast, a bad reload must not take down live traffic), then one write to a `volatile` reference. Readers grab the reference once per operation so a single request sees one consistent table, never a torn mix of old and new rows. In-place mutation of a live map is the classic wrong answer.
- **Runtime-editable registries** (rung 4): `ConcurrentHashMap<Key, VersionedEntry>` when entries change independently (per-key atomic `compute`, versioned entries for rollback); copy-on-write when the table is small and read-massively. If changes notify listeners, snapshot the listener list and invoke *outside* the lock, never call foreign code holding your lock.

## 4. Skeletons (declarations only)

```java
// Rate/slab table (immutable, validated at construction)
public record SlabRow(BigDecimal upTo, BigDecimal ratePercent) {}
public final class SlabTable {
    private final List<SlabRow> rows;                        // sorted ascending; last row upTo = MAX
    private SlabTable(List<SlabRow> rows);                   // throws InvalidConfigException on gaps/overlaps
    public static SlabTable of(List<SlabRow> rows);          // single seam: Main seeds today, file loads tomorrow
    public BigDecimal taxOn(BigDecimal income);
}

// Recipe registry, new beverage = one entry
public record Recipe(String beverage, Map<Ingredient, Integer> amounts) {}
public final class RecipeBook {
    private final Map<String, Recipe> byName;                // Map.copyOf, immutable after build
    public static RecipeBook of(Collection<Recipe> recipes); // validates ingredients known, amounts > 0
    public Optional<Recipe> find(String beverage);
}

// Transition table (FSM as data)
public record TransitionKey(TaskStatus from, TaskEvent event) {}
public final class TransitionTable {
    private final Map<TransitionKey, TaskStatus> table;      // validated: all endpoints are defined states
    public static TransitionTable of(Map<TransitionKey, TaskStatus> t);
    public Optional<TaskStatus> next(TaskStatus from, TaskEvent event);  // empty = illegal transition
}

// Hot-reload holder, swap whole immutable tables, never mutate in place
public final class ConfigHolder<T> {
    private volatile T current;                              // T is an immutable table type
    public T get();                                          // callers read ONCE per operation (consistent snapshot)
    public void reload(T freshValidatedTable);               // validate BEFORE the single volatile write
}
```

## 5. Anti-signals

- **Subclass-per-variant differing only in values**, the classic: subclasses overriding `getFee()` to return literals. That's a rate table wearing a class hierarchy; each new variant costs a file instead of a row. Fix: enum-with-field or `Map`.
- **Magic values scattered through logic**, `if (type == BUS) fee = 50;` inline in the service. The values exist but have no single home, so "change bus fees" is a grep exercise. One table, one lookup.
- **The inverse failure: stringly-typed data when variants DO have behavior**, a `Map<String, Object>` with `"pricingType": "surge"` interpreted by an if/else ladder is a poor man's DSL; you've hidden a Strategy inside untyped data. When a row's field selects *logic*, promote: `Map<Key, PricingStrategy>` registry, data picks the strategy, code implements it.
- **Building config-file parsing infrastructure** in a 60-min round when seeding in `Main` behind one factory method carries the same design signal.
- **Mutable global tables**, a `static` map anyone can `put` into, no owner, no reload discipline: hidden shared state and torn reads. Immutable + holder, or an owned registry.
