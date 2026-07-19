---
layout: post
title: Flyweight
date: 2026-07-19
description: You need a large number of similar objects, and most of their state is actually identical across instances.
categories: interview lld design-patterns structural
mermaid: true
---

[← Back to Structural Patterns](/interview/low-level-design/design-patterns/structural)

If you've ever built something that renders thousands of icons and noticed most of them are the same image just sitting at a different position, this is for you. The file explorer example is exactly that: a thousand files, but only four file types and three folder colors, so there's no reason to load the same icon image a thousand times.

## The problem

You need a large number of similar objects, and most of their state is actually identical across instances. Storing that identical state once per object is wasted memory that scales with object count instead of with the actual variety of data.

## How it's built

`Icon` is the flyweight interface: `draw(int x, int y)`. Notice `x` and `y` are parameters, not fields, that's the extrinsic state, unique per usage, passed in at call time rather than stored on the flyweight.

`FileIcon` and `FolderIcon` are the concrete flyweights. `FileIcon` stores `type` and `image` as its intrinsic state, the constructor builds `image` once as `"Image for " + type + " files"`. `FolderIcon` stores `color` and `image` the same way. Both are the same for every instance representing the same `type` or `color`, which is exactly why they're safe to share.

`IconFactory` is the flyweight factory, holding a static `Map<String, Icon> iconCache`. `getFileIcon(String fileType)` builds a key like `"FILE_" + fileType.toUpperCase()`, checks the cache, and only calls `new FileIcon(fileType)` if that key isn't already there. `getFolderIcon(String color)` does the same with a `"FOLDER_"` prefix. Every subsequent call for the same type or color returns the identical cached instance rather than building a new one, you can see that directly in the test file, `IconFactory.getFileIcon("TXT")` called three times in a row returns the same object, same hash code, every time.

`FileSystemItem` is the context: it holds `name`, `x`, `y` (the extrinsic state, unique per item) and a reference to a shared `Icon`. Its `display()` method prints the name and calls `icon.draw(x, y)`, handing the extrinsic state to the flyweight at the moment it's needed. A thousand `FileSystemItem` instances can all point at the same handful of `Icon` objects.

```mermaid
classDiagram
    class Icon {
        <<interface>>
        +draw(x: int, y: int)
    }
    class FileIcon {
        -type: String
        -image: String
        +draw(x: int, y: int)
        +getType() String
    }
    class FolderIcon {
        -color: String
        -image: String
        +draw(x: int, y: int)
        +getColor() String
    }
    class IconFactory {
        -iconCache: Map~String, Icon~$
        +getFileIcon(fileType: String)$ Icon
        +getFolderIcon(color: String)$ Icon
        +displayCacheStats()$
    }
    class FileSystemItem {
        -name: String
        -x: int
        -y: int
        -icon: Icon
        +display()
        +getName() String
    }
    Icon <|.. FileIcon
    Icon <|.. FolderIcon
    IconFactory ..> Icon
    FileSystemItem o-- Icon
```

## When to reach for it

- You've got a large object count where most of the per-object state is actually identical across many instances.
- The identical portion is safely immutable, sharing it can't cause one caller's mutation to leak into another's.
- The unique-per-instance part (position, name, whatever) is small enough to hand in as a parameter instead of storing on the shared object.

## The takeaway

The whole pattern hinges on correctly splitting state into intrinsic and extrinsic. Get that split wrong, put something that should be per-instance into the shared flyweight, and you've built a bug where one caller's data bleeds into another's, not a memory optimization.

Read the full source on [GitHub](https://github.com/akisonlyforu/design-patterns/tree/master/src/structural/flyweight).

[← Back to Structural Patterns](/interview/low-level-design/design-patterns/structural)
