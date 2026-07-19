---
layout: post
title: Facade
date: 2026-07-19
description: A real operation touches multiple subsystems in a specific sequence, and every caller who wants to perform that operation has to know the sequence, the dependencies between the calls, and the error handling for each step.
categories: interview lld design-patterns structural
mermaid: true
---

[← Back to Structural Patterns](/interview/low-level-design/design-patterns/structural)

If you've ever had to call three services in a specific order just to book a hotel room, and gotten it wrong once because you forgot to check availability before reserving, this is for you. That's the exact shape of the hotel example: `RoomBookingService`, `HousekeepingService`, `RestaurantService`, each fine on its own, miserable to coordinate correctly from outside.

## The problem

A real operation touches multiple subsystems in a specific sequence, and every caller who wants to perform that operation has to know the sequence, the dependencies between the calls, and the error handling for each step. Duplicate that knowledge across enough call sites and a change to the sequence means hunting down every place that got it right (or wrong) independently.

## How it's built

`HotelKeeper` is the facade. Its constructor creates and holds a `HousekeepingService`, a `RestaurantService`, and a `RoomBookingService`, all owned internally, the caller never sees them.

`bookRoom(int roomNumber)` is one method that does the whole sequence: call `roomBooking.checkAvailability(roomNumber)`, and only if that returns true, call `roomBooking.reserveRoom(roomNumber)`, then `housekeeping.cleanRoom(roomNumber)`, then `housekeeping.prepareRoom(roomNumber)`. The caller gets one method call. The ordering logic and the branch on availability live in exactly one place.

`orderRoomService(int roomNumber, String menuType, String foodItem)` does the same for the food side: it calls `getMenu(menuType)`, which is itself a small facade over a `Menu` interface (`VegMenu`, `NonVegMenu`, `MixedMenu`, each just printing their own `getMenu()`), then calls `restaurant.orderFood(foodItem)` and `restaurant.deliverFood(roomNumber, foodItem)`. The caller never touches `RestaurantService` or the `Menu` implementations directly, `HotelKeeper` is the only thing that knows they exist.

```mermaid
classDiagram
    class HotelKeeper {
        -housekeeping: HousekeepingService
        -restaurant: RestaurantService
        -roomBooking: RoomBookingService
        +getMenu(menuType: String) Menu
        +bookRoom(roomNumber: int)
        +orderRoomService(roomNumber: int, menuType: String, foodItem: String)
    }
    class HousekeepingService {
        +cleanRoom(roomNumber: int)
        +prepareRoom(roomNumber: int)
    }
    class RestaurantService {
        +orderFood(menuItem: String)
        +deliverFood(roomNumber: int, menuItem: String)
    }
    class RoomBookingService {
        +checkAvailability(roomNumber: int) boolean
        +reserveRoom(roomNumber: int)
    }
    class Menu {
        <<interface>>
        +getMenu()
    }
    class VegMenu {
        +getMenu()
    }
    class NonVegMenu {
        +getMenu()
    }
    class MixedMenu {
        +getMenu()
    }
    HotelKeeper o-- HousekeepingService
    HotelKeeper o-- RestaurantService
    HotelKeeper o-- RoomBookingService
    HotelKeeper ..> Menu
    Menu <|.. VegMenu
    Menu <|.. NonVegMenu
    Menu <|.. MixedMenu
```

## When to reach for it

- A real-world operation needs several subsystem calls in a fixed order, and you don't want that order re-implemented at every call site.
- You want a single, narrow surface (`bookRoom`, `orderRoomService`) that hides which subsystems exist behind it.
- The subsystems themselves are fine, the problem is purely that coordinating them from outside is error-prone.

## The takeaway

A facade doesn't replace the subsystem classes or simplify what they do, it just moves the coordination logic into one place instead of leaving it implicit in every caller's head. If two different call sites are calling your subsystem methods in a slightly different order, that's the smell that tells you a facade was missing.

Read the full source on [GitHub](https://github.com/akisonlyforu/design-patterns/tree/master/src/structural/facade).

[← Back to Structural Patterns](/interview/low-level-design/design-patterns/structural)
