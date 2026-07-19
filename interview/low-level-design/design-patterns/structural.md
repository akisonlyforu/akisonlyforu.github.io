---
layout: post
title: Structural Patterns
date: 2026-07-19
description: "Object composition patterns: Adapter, Bridge, Composite, Decorator, Facade, Flyweight, Private Class Data, Proxy, each with real Java source and a class diagram."
categories: interview lld design-patterns structural
---

How objects get wired into bigger structures without every wire tangling into every other wire. Adapters translate, decorators layer, facades hide, proxies stand in for the real thing. Eight patterns, all about composition, none of them about creation.

- [Adapter](/interview/low-level-design/design-patterns/adapter)
  Translates one interface's calls into another's, so old and new code can talk without either one changing.
- [Bridge](/interview/low-level-design/design-patterns/bridge)
  Splits two independent hierarchies, like vehicle and workshop, apart so either can grow without multiplying the other's class count.
- [Composite](/interview/low-level-design/design-patterns/composite)
  Lets a tree of leaves and containers, like files and directories, get traversed through one shared interface, no isDirectory checks needed.
- [Decorator](/interview/low-level-design/design-patterns/decorator)
  Wraps an object in layers of optional, combinable behavior instead of writing a subclass for every combination.
- [Facade](/interview/low-level-design/design-patterns/facade)
  Wraps a fixed sequence of subsystem calls behind one method, so callers stop re-implementing the same coordination logic.
- [Flyweight](/interview/low-level-design/design-patterns/flyweight)
  Shares identical state across many objects and passes the unique part in per call, cutting memory that would otherwise scale with object count.
- [Private Class Data](/interview/low-level-design/design-patterns/private-class-data)
  Locks a class's internal state behind getters only, so nothing outside it, not even its own subclasses, can mutate it after construction.
- [Proxy](/interview/low-level-design/design-patterns/proxy)
  Stands in for a real object, deferring its expensive construction or adding checks, while callers can't tell which one they're holding.

[← Back to Design Patterns](/interview/low-level-design/design-patterns)
