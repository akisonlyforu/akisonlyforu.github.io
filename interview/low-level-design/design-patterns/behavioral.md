---
layout: post
title: Behavioral Patterns
date: 2026-07-19
description: "Object communication patterns: Chain of Responsibility, Command, Interpreter, Iterator, Mediator, Memento, Null Object, Observer, State, Strategy, Template Method, Visitor, each with real Java source and a class diagram."
categories: interview lld design-patterns behavioral
---

How objects talk, delegate, and change behavior over time. The biggest bucket, twelve patterns, because there's more than one shape for objects to communicate in: chains, commands, states, strategies, and a handful of less common ones you'll still recognize the moment you've seen them once.

- [Chain of Responsibility](/interview/low-level-design/design-patterns/chain-of-responsibility)
  Passes a request through a sequence of handlers until one of them takes it, without any handler knowing about the others.
- [Command](/interview/low-level-design/design-patterns/command)
  Wraps an operation as an object, so an invoker can trigger, queue, or undo it without ever knowing the receiver.
- [Interpreter](/interview/low-level-design/design-patterns/interpreter)
  Represents each grammar rule as its own class and evaluates an expression by walking the resulting tree.
- [Iterator](/interview/low-level-design/design-patterns/iterator)
  Lets a caller walk a collection without touching its internal storage, and supports multiple independent traversals at once.
- [Mediator](/interview/low-level-design/design-patterns/mediator)
  Routes many-to-many communication between components through one coordinator instead of a web of direct references.
- [Memento](/interview/low-level-design/design-patterns/memento)
  Snapshots an object's state for undo/redo without exposing its internals to whatever's storing the history.
- [Null Object](/interview/low-level-design/design-patterns/null-object)
  Replaces an optional, absent collaborator with a real do-nothing object, killing null checks at every call site.
- [Observer](/interview/low-level-design/design-patterns/observer)
  Lets a publisher notify an arbitrary, changing set of subscribers without hardcoding who's listening.
- [State](/interview/low-level-design/design-patterns/state)
  Moves per-state behavior into its own class per state, so an entity's methods stop branching on which stage it's in.
- [Strategy](/interview/low-level-design/design-patterns/strategy)
  Swaps an entire algorithm at runtime behind one interface, instead of branching on which one to run.
- [Template Method](/interview/low-level-design/design-patterns/template-method)
  Locks a fixed algorithm skeleton in a final method, letting subclasses only override the steps that actually vary.
- [Visitor](/interview/low-level-design/design-patterns/visitor)
  Adds new operations over a fixed set of element types from the outside, without touching the elements themselves.

[← Back to Design Patterns](/interview/low-level-design/design-patterns)
