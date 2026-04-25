---
layout: post
title: Rule-Chain Variation Playbook
date: 2026-07-12
description: Chain of Responsibility, rule lists, and filter pipelines. Three shapes candidates conflate, and why a flat rule list beats linked handlers most of the time.
categories: interview lld patterns
---

Deep dive on the Rule-Chain variation type, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). This is the playbook for problems where the variation axis is an ORDERED LIST OF CHECKS OR HANDLERS, not a single swappable algorithm.

## 1. When a rule chain is the answer

Triggers, in the problem statement or the follow-up you predict early:

- **N independent checks that must run in a defined order**, "validate the coupon: expired? min cart value? user segment? usage limit?" Each check is small; the design question is how they compose, not what any one does.
- **"Add / remove / reorder rules"** is the stated or obvious extension.
- **Per-rule reasons in the output**, the decision must carry which rule rejected or fired (coupon rejection reason, fraud explainability). A boolean can't do this; a rule chain with a Result type can.
- The checks guard a verb that already exists ("apply coupon", "swipe card", "submit review"), the chain is the variation package, the service stays thin.

Three shapes. Naming which one you're building is the senior move, they look alike and interviewers probe the difference.

| Shape | What happens | Fits when | Example problems |
|---|---|---|---|
| **(a) Classic CoR** | Each handler holds a `next` reference, decides whether/what to pass on | The handler consumes part of the request or escalates it, and the remainder travels down the chain | ATM cash dispenser (₹500 notes taken, remainder passed to ₹100), manager approval escalation, logging levels |
| **(b) Rule list + engine** | Dumb rules in a `List`; an engine loops and applies a policy (short-circuit, all, first-match, weighted) | Almost every eligibility / validation / fraud / policy case. The request isn't consumed, every rule sees the same facts | Coupon engine, access control, fraud rule engine, generic rule engines, chess special-move validators |
| **(c) Filter pipeline** | Stages transform (or enrich/collect) the payload as it passes; output of stage i is input of stage i+1 | Middleware and content processing, where the request or content is legitimately mutated en route | API gateway filters, content moderation filters, feed post-ranking filters |

**Default to (b), and say why.** Textbook CoR buries iteration control inside handlers. Every handler has to remember to call `next`, ordering lives in link-wiring code, and short-circuit vs evaluate-all is implicit. Add a policy change like "now report ALL failing rules" and you're touching every handler. With a list + engine, the engine owns iteration and ordering is just list order. The policy becomes one engine parameter, and rules stay dumb single-check classes. Reserve (a) for when handlers genuinely consume or escalate. In the ATM, the chain shape mirrors the physical cassette stack, each link mutates the remaining amount, so CoR earns its plumbing. Reserve (c) for when the thing flowing through is transformed. Interview line: "these checks don't consume the request, so I'll use a rule list with an engine rather than linked handlers, same Open/Closed win, less plumbing."

One near-miss shape to name and decline: an ordered **comparator cascade** (ranking rules) is a rule chain over ordering. Build it with `Comparator.thenComparing`, not a handler chain. And a single preference check is one `if`, don't chain two predicates.

## 2. Low-level mechanics (Java)

**Interface shape per variant**, pick the return type to match what the problem asks for:

- Shape (b), pass/fail only: `interface Rule { boolean test(Facts facts); }`, fine for a gate with no reason requirement (rare; check first).
- Shape (b), reasons required: `interface Rule { RuleResult evaluate(Facts facts); }` where `RuleResult` carries `ruleId`, pass/fail (or a score), and a human-readable reason. The requirement "rejected with reason" is a return-type decision. Retrofitting reasons onto boolean rules rewrites every rule.
- Shape (a): `abstract class Handler { protected Handler next; abstract DispenseResult handle(DispenseRequest remaining); }`, the request object mutates (remaining amount shrinks) as it descends.
- Shape (c): `interface Filter { GatewayRequest apply(GatewayRequest req) throws RejectedException; }`, transform or throw. Variant for collecting pipelines: `interface ExtractionRule { List<Candidate> extract(Document doc); }` and the engine accumulates + dedups.

**Ordering.** Two options: implicit list order (registration order is evaluation order, fine when rules are wired in one place) vs an explicit `int order()` / priority field on the rule, engine sorts. Prefer explicit priority the moment rules are registered from more than one place or reordering is a stated operation. Never let correctness depend on undeclared list position.

**Short-circuit vs evaluate-all.** Short-circuit (stop at first failure) is cheapest and right for gates where one deny ends it. Evaluate-all-with-reasons is required when the caller needs the full picture ("expired AND below min cart value"). Make it an engine parameter, not a per-rule concern: `evaluate(facts, EvaluationPolicy.ALL_REASONS)`. Ask early: "on rejection, do you want the first reason or all reasons?" It changes the return type of the engine. Cheap to ask, expensive to retrofit.

**First-match vs all-match** (which rules fire, distinct from short-circuit on failure): categorization is often first-match, exactly one category. First-match is also what makes rule order deterministic, an invariant worth stating out loud. Make the conflict policy (first-match vs all-match) a strategy the engine takes.

**Composite conditions (AND/OR trees).** When a rule's condition is itself structured, split **Rule** (condition + action/result) from **Condition** (a tree of `AndCondition` / `OrCondition` / `NotCondition` over leaf predicates). Short-circuit inside the tree (`&&` semantics). This is Composite nested inside your chain, say "the chain composes rules, Composite composes a rule's condition."

**Weighted rules → score → threshold.** Each rule returns a score contribution instead of a verdict; the engine sums `weight × signal` and a threshold table maps score → Decision (ALLOW / REVIEW / BLOCK). The decision policy itself (weighted score vs any-block-rule wins) is a strategy on the engine. This is still shape (b), only the fold changes.

**Rules as data + hot reload.** Rules are registered (`addRule`, `setEnabled`), not hardcoded, which makes the rule list mutable config. Hold `volatile List<Rule> rules` (or an immutable `RuleSet`), mutate by building a new list and swapping the reference, copy-on-write. Evaluations in flight finish on the old snapshot; no locks on the hot path.

**Default when no rule decides.** Every chain needs an explicit terminal policy: fail-secure default-DENY, or falling through to `UNCATEGORIZED`. State it as an invariant; a chain that falls off the end silently is a bug.

## 3. Rule chains + concurrency

- **Stateless rules are free.** A rule that only reads its `Facts` argument has no shared state, one instance shared across all threads, no locks. Say it: "rules are stateless, so evaluation is embarrassingly parallel per request."
- **The engine reads a snapshot.** Evaluation = read-only walk over an immutable rule list + caller-owned facts. With copy-on-write registration, readers never block writers and vice versa. This is the whole concurrency story for most shape-(b) problems, don't invent locks.
- **Mutating the rule list** is the only writer: build-new-then-swap under a small lock or `synchronized addRule`, mutation is rare, evaluation is hot, so the asymmetry is the point.
- **Per-rule state is the exception and needs its own guard.** Velocity rules keep per-user sliding windows; usage-limit rules keep per-code counters. That state lives in a `ConcurrentHashMap` inside (or injected into) the rule, updated with `compute()` per key. The chain doesn't protect this state; the map primitive does.
- Shape (a) with shared physical state (ATM cassette counts): the whole check-then-dispense walk must be atomic against concurrent refill, one inventory lock around the chain traversal, because the invariant (dispensed sum exact, counts non-negative) spans all links.

## 4. Skeletons (signatures only)

```java
// Shape (b): rule list + engine (your default)
interface Rule { String id(); int order(); RuleResult evaluate(Facts facts); }
record RuleResult(String ruleId, boolean passed, String reason) { }
final class RuleEngine {
    Decision evaluate(Facts facts, EvaluationPolicy policy);   // SHORT_CIRCUIT | ALL_REASONS | FIRST_MATCH
    void addRule(Rule r); void removeRule(String id);          // copy-on-write swap inside
}

// Weighted variant (fraud): rule returns a score, engine folds
interface ScoredRule { double score(Facts facts); double weight(); }
Decision decide(Facts facts);                                   // Σ weight·score vs threshold table

// Composite condition node
interface Condition { boolean matches(Map<String, Object> facts); }
final class AndCondition implements Condition { AndCondition(List<Condition> children); }

// Shape (a): classic CoR, consuming handler (ATM)
abstract class DenominationDispenser {
    protected DenominationDispenser next;
    abstract void dispense(int remainingAmount, Map<Denomination, Integer> plan);
}

// Shape (c): transforming filter (gateway) and collecting rule (extractor)
interface Filter { GatewayRequest apply(GatewayRequest req) throws RejectedException; }
interface ExtractionRule { List<Candidate> extract(Document doc); }
```

## 5. Anti-signals

- **Textbook CoR plumbing where a list + loop is clearer.** Hand-wiring `ruleA.setNext(ruleB)` for eligibility checks that never consume the request is pattern theater; the interviewer watches you spend five minutes on linkage the engine loop gives you for free. Worse, GoF CoR's "maybe nobody handles it" semantics are wrong for validation, where every rule must run or a definite decision must emerge.
- **Boolean-only rules when the problem asks why.** If the spec says "rejected with reason," `boolean test()` is a requirements miss dressed as simplicity. Choose the Result-returning shape up front.
- **Hidden ordering dependencies.** A rule that assumes another already ran, cheap rules sitting implicitly before expensive ones, first-match categorization whose correctness silently depends on insertion order. Make ordering explicit: an order field, or one wiring site with a comment stating the invariant.
- **Chaining what isn't a chain.** Two fixed predicates is an `if`; a comparator cascade is `thenComparing`; one swappable algorithm is a Strategy. Building a rule framework for these reads as over-engineering.
- **Silent fall-through.** No explicit default decision when no rule fires, state it as an invariant.
