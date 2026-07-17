---
layout: post
title: Design Patterns
date: 2026-07-19
description: "All 26 Gang of Four design patterns, Creational, Structural, Behavioral, each with real Java source and a class diagram, no textbook filler."
categories: interview lld design-patterns
---

These aren't from a textbook chapter I read once. Every pattern in this catalog is a small Java package I've actually written, tested, and broken at least once, `singleton.java` threw a null under load before I put the `volatile` back in, the object pool test file still fakes an expensive constructor with a 50ms sleep because that's the honest way to demo one without a real database sitting behind it. 26 patterns, three buckets: how you build objects (Creational), how you wire them into bigger structures (Structural), how they talk to each other (Behavioral). Each pattern page has the real class diagram, pulled from the actual source in [design-patterns/src](https://github.com/akisonlyforu/design-patterns), not a redrawn textbook version. If you haven't read the [LLD framework](/interview/low-level-design/lld-framework) yet, do that first, these patterns are the vocabulary, that framework is when to actually reach for one.

## What they're all for

One idea sits under all 26: a pattern isn't a clever structure for its own sake, it's insurance against a specific kind of change. You pick one by the change you're afraid of. Naming a concrete class at every `new`? Factory Method and Abstract Factory absorb that. An algorithm that keeps getting rewritten? Strategy. Behavior that flips depending on what state you're in? State. Each pattern's whole job is to let *one* thing vary while everything around it stays put.

Two rules are worth keeping in your head before you read the pages below. Program to an interface, not an implementation, depend on what an object can do, never on what it is. And favor object composition over class inheritance, because inheritance freezes the choice at compile time and lets a subclass see straight into its parent, so you change the parent and the subclass breaks, while composition wires objects together at runtime through their interfaces and you swap a part without cracking it open. That second rule is why half this catalog exists.

## Creational Patterns

Object creation control. When making an object is expensive, or the exact type isn't known until runtime, or you need exactly one instance, exactly one family of related instances, or a fast supply of near-identical copies, one of these six covers it.

[Read the Creational Patterns →](/interview/low-level-design/design-patterns/creational)

## Structural Patterns

How objects get wired into bigger structures without every wire tangling into every other wire. Adapters translate, decorators layer, facades hide, proxies stand in for the real thing. Eight patterns, all about composition, none of them about creation.

[Read the Structural Patterns →](/interview/low-level-design/design-patterns/structural)

## Behavioral Patterns

How objects talk, delegate, and change behavior over time. The biggest bucket, twelve patterns, because there's more than one shape for objects to communicate in: chains, commands, states, strategies, and a handful of less common ones you'll still recognize the moment you've seen them once.

[Read the Behavioral Patterns →](/interview/low-level-design/design-patterns/behavioral)

[← Back to Low Level Design](/interview/low-level-design/)
