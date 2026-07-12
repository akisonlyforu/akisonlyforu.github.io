---
layout: post
title: Command Variation Playbook
date: 2026-07-12
description: Undo/redo, operation queues, and audit logs. When to reify an operation into an object, inverse data vs full snapshots, and command queues under concurrency.
categories: interview lld patterns
---

Deep dive on the Command variation type, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). Covers the whole family: undoable operations, queued jobs, and audit/replay logs.

## 1. When Command is the answer

Reify the operation into an object when the operation itself needs a lifecycle beyond "call and forget." Five triggers:

| Trigger | Why an object beats a method call | Canonical problems |
|---|---|---|
| **Undo/redo** | You must store enough to reverse it later | Text editor, diagramming tool |
| **Operation queue** | Someone other than the caller executes it, later, possibly on another thread | Job scheduler, IoT command queue |
| **Audit / replay** | The log of operations IS the source of truth; state = fold(log) | Version history, workflow checkpoints |
| **Macro composition** | Users bundle operations into one named unit | Home automation scenes |
| **Scheduling / retry** | The operation must survive until its time comes, and survive failure | Job scheduler, offline command queue |

One problem often stacks several: a home automation scene has undo + macro + queue; an IoT hub has queue + retry + idempotency.

**Non-triggers.** Plain CRUD where the caller invokes and moves on, think `parkVehicle()` or `addToCart()`, needs no Command. If nothing ever undoes, queues, replays, or composes the operation, a service method is the design; wrapping it in a Command object is the anti-signal. Say it out loud: "these operations are fire-and-forget, so no `commands/` package here." Declining correctly is the same senior signal as choosing correctly.

## 2. Low-level mechanics (Java)

**Interface shape.** Two viable contracts here. Pick one per problem and don't mix them:

```java
interface Command { void execute(); void unexecute(); }        // inverse lives INSIDE the command
interface Command { Memento execute(); }                        // caller stores returned snapshot
```

The first is the default for editors and device hubs: the command captures its own inverse data during `execute()` (fields set at execute time, read at unexecute time). The second only earns its keep when the receiver can't cheaply describe an inverse. For queue-only commands that are never undone, drop `unexecute()` entirely: `interface Job { void run(); }`. An unimplemented `unexecute()` that throws `UnsupportedOperationException` on half your commands is a design smell.

**Inverse data vs full snapshot (Memento).** This is the trade-off to narrate:
- **Inverse data wins when state is large relative to the change.** A text editor's `DeleteCommand` stores the deleted lines and their index, not the whole document. Memento-per-keystroke on a 10MB document is the rejected alternative; reject it by name and say why (memory).
- **Full snapshot wins when state is tiny or the inverse is hard to express.** A 16-int game grid: snapshot the grid per move rather than trying to un-merge tiles, which is genuinely hard (a merge destroys information). The rule is not "snapshots bad": it's *cost of snapshot vs complexity of inverse*.
- **Overlay is the third option.** Instead of an undo log per write, stack overlay write-sets; rollback discards the top overlay. Overlay and undo-log are duals (both record "what changed"); overlay wins for nested transactions because discard is O(1) per layer.

**Two-stack undo/redo discipline.** `Deque<Command> undoStack, redoStack`. execute → push undo, **clear redo**. undo → pop undo, unexecute, push redo. redo → pop redo, execute, push undo. *Redo clears on new mutation*, that's the rule to say out loud. Forgetting it lets users redo into a timeline that no longer exists. Also cap history (`Deque` + evict oldest) and say the memory bound out loud.

**Compensation ≠ undo.** Some systems use a different reversal shape: a *forward* action that semantically reverses (compensation step, inverse commit / revert). It appends to history instead of popping it. It must run **exactly once** per completed step on abort, and it's ordered reverse-topologically, not LIFO-per-user-whim. If the interviewer says "rollback a workflow," reach for compensation-as-new-command, not the two-stack machinery. History stays immutable, that's an audit requirement.

**Macro commands.** Composite of commands: `Scene implements Command { List<Command> steps; }`. Two decisions to state: failure policy (all-or-report-partial) and undo order (unexecute in *reverse*). A macro's undo is the reversed unexecute of its children.

**Command queues.** When executor ≠ caller: jobs sit in a shared `BlockingQueue` (workers take), or a `DelayQueue`/`PriorityQueue` keyed by fire time (dispatcher pops due jobs to a pool). The command object is what makes this possible. You can't queue a method call, but you can queue an object.

**Commands as audit log / event-sourcing-lite.** Keep the executed commands (or immutable ChangeRecords) in an append-only list; replay = fold. Two invariants to state: the log is append-only (never mutate a past record), and per-entity ordering is strict. Compaction (snapshot every N changes) is the standard follow-up.

**Idempotency under retry.** Queued commands get retried; each carries a `commandId`, and the receiver dedups. Mechanism: `ConcurrentHashMap#putIfAbsent(cmdId, ...)` as the dedup gate, or a per-command status field advanced via `compute()`. Say "the command is idempotent because the receiver dedups by id, so retry is safe."

## 3. Command + concurrency

Commands aren't just for undo. Reified operations are a **serialization mechanism**. Three idioms:

1. **Per-entity serialized queue (the actor-ish pattern).** One logical queue + one consumer per entity gives per-entity ordering *without locks on the entity state*. App, schedule, and sensor commands never interleave on one device. Narrate: "instead of locking device state, I serialize all mutations through the device's queue, ordering by construction." Cross-entity operations still need sorted-lock discipline; the queue only serializes one entity.
2. **Shared work queue = producer-consumer.** Workers `take()` from a `BlockingQueue`, the take is the atomic claim, no extra lock. For a scheduler, the race to watch is pop-vs-cancel, make pop+mark-running atomic via a per-job state `compute()`.
3. **Undo history under concurrency.** Usually you don't need it. Most editors are single-user; *say* "single-user, so sequential, and I'll spend the saved time making undo exact." If forced multi-user, the honest answer is per-document serialization (one queue again). Interleaved undo stacks are a much bigger discussion, so name the boundary rather than hand-wave.

## 4. Skeletons (signatures only)

```java
public interface Command {
    void execute();
    void unexecute();
    String describe();                       // audit-log line; cheap, high signal
}

public class DeleteLinesCommand implements Command {
    private final Document doc;
    private final int fromLine, toLine;      // set at construction
    private List<String> deletedLines;       // inverse data, captured during execute()
    public void execute();                   // remove lines, capture them into deletedLines
    public void unexecute();                 // re-insert deletedLines at fromLine
}

public class CommandHistory {
    private final Deque<Command> undoStack;  // bounded, evict oldest
    private final Deque<Command> redoStack;
    public void executed(Command c);         // push undo, CLEAR redo
    public void undo();                      // pop undo → unexecute → push redo
    public void redo();                      // pop redo → execute → push undo
}

public class MacroCommand implements Command {   // e.g. a home-automation Scene
    private final List<Command> steps;
    public void execute();                   // forward order; state failure policy
    public void unexecute();                 // REVERSE order
}
```

## 5. Anti-signals

- **Full-document snapshot per keystroke.** Using Memento where inverse data would suffice is memory-blind. (Conversely, hand-rolling a fragile un-merge when a 16-int snapshot is free is its own mistake. The trade-off cuts both ways.)
- **Forgetting redo-clear.** Execute a new mutation with a non-empty redo stack and not clearing it. Interviewers test this sequence deliberately: type, undo, type, redo.
- **Undo that doesn't restore exactly.** Off-by-one on re-insert index, clipboard clobbered by unrelated commands, device left in a default rather than *prior* state. Prove it in `Main`: run a command, undo, assert state deep-equals the original; then assert `undo(redo(x)) == x`.
- **Command for operations that never need undo/queue/audit.** A `CreateUserCommand` wrapping `userService.create()` in a CRUD problem is ceremony, not design.
- **`unexecute()` stubs on queue-only commands.** If jobs are never reversed, don't carry the method.
- **Mutating the audit log.** "Undoing" by deleting log entries in an audit/replay problem. Append a compensating entry instead; the log is append-only.
