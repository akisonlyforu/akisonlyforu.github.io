---
layout: post
title: Null Object
date: 2026-07-19
description: Application takes an AbstractLogger, but logging is genuinely optional in some configurations, and you don't want performOperation(), or any other method, doing a null check before every single log call, that check would end up repeated everywhere the logger gets used.
categories: interview lld design-patterns behavioral
mermaid: true
back_url: /interview/low-level-design/design-patterns/behavioral
back_label: Behavioral Patterns
---

I've lost count of how many `NullPointerException`s I've traced back to an optional dependency, a logger, a notifier, something that's fine to skip, that someone forgot to null-check three call sites deep. Null Object's fix is almost insultingly simple: make "nothing" a real object instead of the absence of one.

## The problem

`Application` takes an `AbstractLogger`, but logging is genuinely optional in some configurations, and you don't want `performOperation()`, or any other method, doing a null check before every single log call, that check would end up repeated everywhere the logger gets used.

## Without the pattern

The obvious alternative is to let `LoggerFactory.getLogger()` hand back an actual `null` when nobody's configured a logger, no `NullLogger`, just the absence of one, and push the null-check onto whoever calls `logger.log(...)`. That works, technically, as long as every single call site remembers to guard it: `performOperation()` checks `logger != null` before logging, `setLogger()` checks it, and every method anyone adds to `Application` from now on has to remember the same check, forever, because nothing in the type system reminds them the reference might be empty. Get it right in forty call sites and miss it in the forty-first, written eight months later by someone who never saw the original "logging is optional" decision, and that one call site sits there passing every test until the day it actually runs with a null logger in production.

```mermaid
sequenceDiagram
    participant Caller
    participant Application
    participant Logger as AbstractLogger (null)
    Caller->>Application: new Application(null)
    Note over Application: no logger configured, reference is null
    Caller->>Application: performOperation("audit")
    Application->>Application: if (logger != null) logger.log(...)
    Note over Application: guarded correctly, nothing happens
    Caller->>Application: exportReport()
    Note over Application: new method, guard forgotten
    Application->>Logger: logger.log("export started")
    Logger--xApplication: NullPointerException
```

## With the pattern

`AbstractLogger` is a one-method abstract class, `log(String)`. `ConsoleLogger` and `FileLogger` are the real implementations doing actual work. `NullLogger extends AbstractLogger` too, its `log()` body is empty, a legitimate implementation of the contract that just does nothing. `LoggerFactory.getLogger(String type)` is the single place that decides what you get back, if `type` is null or doesn't match `"CONSOLE"`/`"FILE"`/`"NULL"` it falls through to `new NullLogger()`, so the factory never hands back an actual null reference. `Application`'s constructor still guards with a ternary, `logger != null ? logger : new NullLogger()`, covering the case where someone bypasses the factory and passes null directly, so the null-safety is enforced at two levels, factory and consumer, belt and suspenders. Everywhere else in `Application`, `logger.log(...)` runs with zero null checks, because there's no null logger reachable through this code path anymore.

```mermaid
classDiagram
    class AbstractLogger {
        <<abstract>>
        +log(String)*
    }
    class ConsoleLogger {
        +log(String)
    }
    class FileLogger {
        -String filename
        +log(String)
    }
    class NullLogger {
        +log(String)
    }
    class LoggerFactory {
        +getLogger(String) AbstractLogger
    }
    class Application {
        -AbstractLogger logger
        +performOperation(String)
        +setLogger(AbstractLogger)
    }
    AbstractLogger <|-- ConsoleLogger
    AbstractLogger <|-- FileLogger
    AbstractLogger <|-- NullLogger
    LoggerFactory ..> AbstractLogger : creates
    Application --> AbstractLogger : logger
```

## What it costs you

`NullLogger`'s `log()` body is empty on purpose, and that's exactly the problem: a genuinely missing configuration and a deliberately empty one produce the identical object. If `LoggerFactory.getLogger("CONSOL")` gets a typo'd type string instead of `"CONSOLE"`, that falls through the same `else` branch as an intentional `"NULL"` request, both hand back a `NullLogger`, and `Application` has no way to tell "someone forgot to wire up logging" from "this environment doesn't want logging" because by the time the object reaches `Application`, that distinction is already gone. The old null-reference version would've blown up loudly at the first unguarded call site, annoying, but impossible to miss. The Null Object version just runs, quietly, producing an `Application` that never writes a single log line, and the only way anyone notices is going looking for logs that were never there, usually while debugging something else entirely. You traded a crash you'd catch in the first test run for a silence you might not catch for months.

## When to reach for it

Optional collaborators, objects your code calls but which are allowed to legitimately do nothing, logging, notifications, analytics hooks, anywhere a no-op is a valid business outcome rather than an error condition.

## The takeaway

Don't use Null Object to swallow error states, it's for "this collaborator is legitimately absent," not "something went wrong and I don't want to deal with it." If the null case should actually surface an error somewhere, a no-op object just hides the bug quietly instead of failing loudly.

Read the full source on [GitHub](https://github.com/akisonlyforu/design-patterns/tree/master/src/behavioral/null_object).

[← Back to Behavioral Patterns](/interview/low-level-design/design-patterns/behavioral)
