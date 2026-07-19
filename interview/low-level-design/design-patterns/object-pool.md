---
layout: post
title: Object Pool
date: 2026-07-19
description: Some objects are genuinely expensive to construct and short-lived in how they're used, database connections being the standard example.
categories: interview lld design-patterns creational
mermaid: true
---

The test file for this one fakes "expensive" with a 50 millisecond Thread.sleep() in the constructor, which is a stand-in for the real thing, a database handshake, a socket setup, whatever actually costs wall-clock time to build. The pool exists so you pay that cost once per object and then hand the same object out over and over instead of re-paying it on every request.

## The problem

Some objects are genuinely expensive to construct and short-lived in how they're used, database connections being the standard example. Creating a fresh one per request and throwing it away afterward means paying the expensive part constantly and generating garbage the collector has to clean up. If the number of objects actually in use at any moment is small, a pool of pre-built, reusable ones is cheaper than constant creation and disposal.

## How it's built

Reusable is the one-method contract, reset(). ExpensiveObject implements it, carries an inUse boolean, and its constructor does the simulated expensive work (the sleep) before printing that it's done. reset() just flips inUse back to false. doWork() checks inUse first and throws IllegalStateException if the object hasn't actually been acquired, which catches the specific bug of someone holding a reference to a pooled object they never checked out.

ObjectPool<T extends Reusable> holds two lists, available and inUse, plus an ObjectFactory<T>. acquire() checks available first, if it's empty it delegates to factory.create() for a new instance, otherwise it pops the last entry off available and reuses it, either way the object goes into inUse before it's handed back to the caller. release(T obj) does the reverse, removes it from inUse, calls reset() on it, and adds it to available. The reset() call inside release() is the part that's easy to forget when reimplementing this from memory, skip it and the next caller who acquires that object gets one with leftover state from whoever used it last.

ObjectFactory<T> exists for a specific reason that has nothing to do with elegance, Java generics can't do new T(), there's no way for ObjectPool<T> to construct a T directly. So the factory is how the pool learns what to build without knowing the concrete type itself, ExpensiveObjectFactory.create() just returns new ExpensiveObject(), and the pool calls that instead of a constructor it doesn't have access to.

One thing worth flagging since it's not addressed here: available and inUse are plain ArrayLists, and acquire()/release() aren't synchronized. That's fine for a single-threaded demo but not for concurrent callers, two threads racing to acquire from a pool with exactly one available object could both pull it or corrupt the list's internal state. A real pool needs that acquire-check-and-claim sequence protected, either a lock around it or a concurrent collection built for exactly this.

```mermaid
classDiagram
    class Reusable {
        <<interface>>
        +reset() void
    }
    class ExpensiveObject {
        -inUse: boolean
        +reset() void
        +isInUse() boolean
        +setInUse(inUse: boolean) void
        +doWork() void
    }
    class ObjectFactory~T~ {
        <<interface>>
        +create() T
    }
    class ExpensiveObjectFactory {
        +create() ExpensiveObject
    }
    class ObjectPool~T~ {
        -available: List~T~
        -inUse: List~T~
        -factory: ObjectFactory~T~
        +acquire() T
        +release(obj: T) void
        +showStats() void
    }
    Reusable <|.. ExpensiveObject
    ObjectFactory <|.. ExpensiveObjectFactory
    ObjectPool o-- ObjectFactory
    ObjectPool ..> Reusable : manages
    ExpensiveObjectFactory ..> ExpensiveObject : creates
```

## When to reach for it

Database connection pools, thread pools, buffer pools, anything where construction cost is real and the object is safe to reset and reuse rather than being tied to one specific piece of state. Skip it if construction is cheap, you'll add lifecycle complexity (tracking available vs in-use, remembering to release) for no real benefit.

## The takeaway

The two things that make this pattern actually work are reset() being mandatory on release and the factory existing to solve Java's "can't new a generic type" problem. Miss either one and you either leak state between users or you can't write the pool at all.

Read the full source on [GitHub](https://github.com/akisonlyforu/design-patterns/tree/master/src/creational/object_pool).

[← Back to Creational Patterns](/interview/low-level-design/design-patterns/creational)
