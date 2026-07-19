---
layout: post
title: Creational Patterns
date: 2026-07-19
description: "Object creation patterns: Singleton, Factory Method, Abstract Factory, Builder, Prototype, Object Pool, each with real Java source and a class diagram."
categories: interview lld design-patterns creational
---

Object creation control. When making an object is expensive, or the exact type isn't known until runtime, or you need exactly one instance, exactly one family of related instances, or a fast supply of near-identical copies, one of these six covers it.

- [Singleton](/interview/low-level-design/design-patterns/singleton)
  Guarantees exactly one instance exists, and makes the lazy double-checked construction safe when multiple threads race to create it first.
- [Factory Method](/interview/low-level-design/design-patterns/factory-method)
  One method owns "which concrete class to build," so every caller picks a type without duplicating the same if/else ladder.
- [Abstract Factory](/interview/low-level-design/design-patterns/abstract-factory)
  Guarantees every object built through one factory belongs to the same family, so you can't accidentally mix a Windows audio player with a VLC video player.
- [Builder](/interview/low-level-design/design-patterns/builder)
  Splits required fields, enforced in the constructor, from optional ones set via chained calls, so you skip telescoping constructors and half-built objects.
- [Prototype](/interview/low-level-design/design-patterns/prototype)
  Clones existing instances instead of rebuilding from scratch, using copy constructors instead of Java's Cloneable.
- [Object Pool](/interview/low-level-design/design-patterns/object-pool)
  Reuses a fixed set of expensive objects instead of constructing and discarding a fresh one per request.

[← Back to Design Patterns](/interview/low-level-design/design-patterns)
