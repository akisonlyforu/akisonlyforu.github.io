---
layout:     post
title:      How Do You Make Your Own Reads Strongly Consistent
date:       2026-08-02
description:    S3 was eventually consistent until December 2020, and the classic symptom was an ETL job that committed cleanly and quietly wrote fewer files than it produced. What AWS did about it with per-object versions and an in-memory witness, what that costs, and how to build the same thing in your own system.
categories: aws s3 consistency distributed-systems caching
---

AWS consumers faced the same issue with S3 when they were not able to get the latest data on the read-after-write queries they were making on S3 (List Blobs to be specific).

If you were running ETL on it, it was the thing quietly eating your rows.

Netflix ended up writing a library because of this issue. Their pipeline was Pig and Hive jobs combined through a scheduler, so job 1 writes its output to S3, job 2 fires when job 1 reports done, and the first thing job 2 does is list the output directory to find out what it's reading.

When that listing came back short, job 2 just processed whatever it got, and there was no exception to catch and no retry to trigger, because as far as every component involved was concerned everything had worked.

It took them years of chasing the issue before consistency was even on the list of probable RCAs.

The fix, once you know, is not hard. The problem was that nothing pointed to it.

## The problem

A job commits, the scheduler fires the next one, and the listing that next job depends on is missing files that were definitely written. Nothing fails. The data plane has every object and a direct GET on any of them works, but the index that answers "what's under this prefix" hasn't caught up yet, so the downstream job reads a subset and reports success on it.

<style>
.cache-bench {
  --cb-bg: #f7f9fb;
  --cb-node: #ffffff;
  --cb-text: #333333;
  --cb-muted: #666666;
  --cb-grid: rgba(0, 0, 0, 0.16);
  --cb-blue: #0076df;
  --cb-orange: #d65f3c;
  --cb-green: #23856d;
  --cb-purple: #7b5bb5;
  margin: 1.8rem 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--cb-grid);
  border-radius: 8px;
  background: var(--cb-bg);
  color: var(--cb-text);
}
.cache-bench h3 { margin: 0 0 1rem; font-size: 1rem; }
.cache-bench figcaption { margin-top: 0.9rem; color: var(--cb-muted); font-size: 0.82rem; line-height: 1.45; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .t { fill: var(--cb-text); font-size: 12.5px; }
.cb-svg .tb { fill: var(--cb-text); font-size: 12.5px; font-weight: 700; }
.cb-svg .cap { font-size: 11px; }
.cb-svg .node { fill: var(--cb-node); stroke: var(--cb-grid); stroke-width: 1.5; }
.cb-svg .node-bad { fill: var(--cb-node); stroke: var(--cb-orange); stroke-width: 1.8; }
.cb-svg .node-ok { fill: var(--cb-node); stroke: var(--cb-green); stroke-width: 1.8; }
.cb-svg .node-key { fill: var(--cb-node); stroke: var(--cb-blue); stroke-width: 1.8; }
.cb-svg .arrow { fill: none; stroke: var(--cb-muted); stroke-width: 1.6; }
.cb-svg .arrow-bad { fill: none; stroke: var(--cb-orange); stroke-width: 1.6; }
.cb-svg .arrow-key { fill: none; stroke: var(--cb-blue); stroke-width: 1.6; }
.cb-svg .dash { stroke-dasharray: 4 3; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .ahead { fill: var(--cb-muted); stroke: none; }
.cb-svg .ahead-bad { fill: var(--cb-orange); stroke: none; }
.cb-svg .ahead-key { fill: var(--cb-blue); stroke: none; }
:root[data-theme="dark"] .cache-bench {
  --cb-bg: #252525;
  --cb-node: #2f2f2f;
  --cb-text: #e0e0e0;
  --cb-muted: #b0b0b0;
  --cb-grid: rgba(255, 255, 255, 0.18);
  --cb-blue: #4dabf7;
  --cb-orange: #ff8a65;
  --cb-green: #51cf66;
  --cb-purple: #b197fc;
}
</style>

<figure class="cache-bench">
  <h3>What job 2 actually sees</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="s3-flow-title s3-flow-desc">
    <title id="s3-flow-title">A commit that succeeds while the listing is still short</title>
    <desc id="s3-flow-desc">Job 1 writes 40 files to the S3 data plane. The LIST index is updated asynchronously and shows 38 keys, so job 2 lists the directory, gets 38 files, and processes them without any error.</desc>
    <defs>
      <marker id="ah-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
      <marker id="ah-a-bad" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead-bad" />
      </marker>
    </defs>
    <rect class="node" x="16" y="34" width="176" height="58" rx="6" />
    <text class="tb" x="104" y="59" text-anchor="middle">job 1 commits</text>
    <text class="t" x="104" y="78" text-anchor="middle">40 files written</text>
    <path class="arrow" d="M192,63 H240" marker-end="url(#ah-a)" />
    <rect class="node" x="248" y="34" width="176" height="58" rx="6" />
    <text class="tb" x="336" y="59" text-anchor="middle">S3 data plane</text>
    <text class="t" x="336" y="78" text-anchor="middle">40 objects, GET works</text>
    <path class="arrow dash" d="M336,92 V136" marker-end="url(#ah-a)" />
    <text class="cap" x="346" y="118">updated asynchronously</text>
    <rect class="node-bad" x="248" y="142" width="176" height="58" rx="6" />
    <text class="tb" x="336" y="167" text-anchor="middle">LIST index</text>
    <text class="t" x="336" y="186" text-anchor="middle">38 keys visible</text>
    <path class="arrow-bad" d="M424,171 H462" marker-end="url(#ah-a-bad)" />
    <rect class="node-bad" x="470" y="142" width="154" height="58" rx="6" />
    <text class="tb" x="547" y="167" text-anchor="middle">job 2 LISTs</text>
    <text class="t" x="547" y="186" text-anchor="middle">processes 38</text>
    <text class="cap" x="336" y="232" text-anchor="middle">exit code 0, no exception raised, dashboard reads about 2% low</text>
  </svg>
  <figcaption>The two missing files are durable and readable by key the whole time. The only thing that is wrong is the answer to "what exists under this prefix", and that is the exact question every commit protocol asks.</figcaption>
</figure>

## Why LIST was the weak spot

Two things about S3 don't behave like a filesystem, and this failure needs both of them.

The first is that object data and object metadata live in different subsystems, so a GET goes and fetches an object by key while a LIST queries an index that answers a completely different question, namely what exists under this prefix. Two systems means they can disagree, which is why read-after-write consistency and list consistency were always described as separate guarantees with separate timelines. The index was updated asynchronously, so a brand new object could be missing from a listing while a direct GET on that same key worked perfectly.

The second is that there is no rename. In a filesystem, rename is an atomic pointer flip and costs nothing. In S3 it's a LIST, then a COPY of every object it found, then a DELETE of every original, which is non-atomic, O(n) in the number of files, and routed through that index at every step.

Now look at what Hadoop, Hive and Spark actually do when they commit. Each task writes into its own private directory, and when the task succeeds the committer renames that directory into the final output location. So every commit runs through the index, and a listing that was missing files gave you a rename that silently moved fewer of them. Sometimes you got a `FileNotFoundException` partway through, which is the good outcome because at least it stops. The rest of the time you ended up with fewer files in the destination than you wrote and nothing said so.

## What people did until 2020

Everybody built solutions on the same idea, which was to keep a second index, somewhere strongly consistent, that recorded what you had actually written, until 2020 when AWS finally decided to fix it and rolled it out worldwide.

So you worked around a consistency problem by running more infrastructure that had a consistency problem of its own.

## What AWS actually did

In December 2020, S3 became strongly consistent for reads, overwrites, deletes and listings. Every object, every region, no opt-in, no price change.

Constraints they were under: no cost increase, no performance regression, no partial rollout where only some buckets get it, no availability tradeoff. Designs that gave up one of those were quickly rejected in the design phase.

S3's metadata layer sits behind a cache that was specifically built to keep serving even when the infrastructure underneath is going through availability issues. In 2006 that was the right call. The side effect is that a write could go through one part of that cache while a read was being answered by another part.

The obvious sounding fix is to skip the cache and read the persistence tier directly, and that got rejected on latency, so instead they treated it as a cache coherence problem. (The same class of problem CPUs solve to keep cores from reading each other's stale values.) Solving it that way needs a version number on every object and something that always knows which version is current.

**Per-object ordering.** Stale is a comparison and comparisons need numbers, so every update to an object needs a defined place in that object's history. This already existed, because the replication logic behind event notifications and Replication Time Control needs per-object ordering to be correct, so the persistence tier was already keeping it.

Per-object is doing the heavy lifting in that sentence. There is no global sequence across S3 and there was never going to be, because a global total order means consensus sitting on the hot path, which costs exactly the latency and availability they had already ruled out. Ordering per key is a local problem, so only the shard that owns that key has to sequence anything.

**The witness.** A component that gets told about every object change and holds nothing except a map from key to version, in memory, never written to disk. When the cache wants to serve a read it asks the witness whether the version it's holding is still current. If it is, the cache serves it. If it isn't, the cache throws it away and goes to the persistence tier. This is the shape AWS described publicly rather than a specification, and they have never published the coherence protocol or what happens during a witness handoff.

The direction matters more than the mechanism. Push invalidation means the writer has to notify every cache node that might be holding that key, which is fan-out that grows with the fleet, and the moment one of those nodes is unreachable you have to choose between blocking the write and serving something stale. Neither is a choice you want to make on every PUT. The witness turns it around, so the writer tells one place and the readers do the checking.

<figure class="cache-bench">
  <h3>Push invalidation against a witness</h3>
  <svg class="cb-svg" viewBox="0 0 640 240" role="img" aria-labelledby="push-pull-title push-pull-desc">
    <title id="push-pull-title">Push invalidation compared with a pull-based witness</title>
    <desc id="push-pull-desc">On the left, a writer must notify every cache node holding the key, and one unreachable node forces a choice between blocking the write and serving stale data. On the right, the writer notifies a single witness and readers ask the witness whether their cached version is still current.</desc>
    <defs>
      <marker id="ah-b" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
      <marker id="ah-b-bad" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead-bad" />
      </marker>
      <marker id="ah-b-key" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead-key" />
      </marker>
    </defs>
    <text class="tb" x="16" y="20">push: the writer tells every cache</text>
    <rect class="node" x="16" y="54" width="88" height="40" rx="6" />
    <text class="t" x="60" y="79" text-anchor="middle">writer</text>
    <path class="arrow" d="M104,74 C146,74 146,49 182,49" marker-end="url(#ah-b)" />
    <path class="arrow" d="M104,74 C146,74 146,99 182,99" marker-end="url(#ah-b)" />
    <path class="arrow-bad dash" d="M104,74 C146,74 146,149 182,149" marker-end="url(#ah-b-bad)" />
    <rect class="node" x="186" y="32" width="110" height="34" rx="6" />
    <text class="t" x="241" y="54" text-anchor="middle">cache 1</text>
    <rect class="node" x="186" y="82" width="110" height="34" rx="6" />
    <text class="t" x="241" y="104" text-anchor="middle">cache 2</text>
    <rect class="node-bad" x="186" y="132" width="110" height="34" rx="6" />
    <text class="t" x="241" y="154" text-anchor="middle">cache 3</text>
    <text class="cap" x="16" y="190">cache 3 unreachable, so block the</text>
    <text class="cap" x="16" y="206">write or serve something stale</text>
    <line class="grid" x1="318" y1="8" x2="318" y2="222" />
    <text class="tb" x="340" y="20">pull: readers ask one witness</text>
    <rect class="node" x="340" y="54" width="88" height="40" rx="6" />
    <text class="t" x="384" y="79" text-anchor="middle">writer</text>
    <path class="arrow-key" d="M428,74 H480" marker-end="url(#ah-b-key)" />
    <text class="cap" x="454" y="66" text-anchor="middle">notify</text>
    <rect class="node-key" x="486" y="54" width="138" height="40" rx="6" />
    <text class="t" x="555" y="79" text-anchor="middle">witness: key to version</text>
    <rect class="node" x="340" y="150" width="88" height="40" rx="6" />
    <text class="t" x="384" y="175" text-anchor="middle">readers</text>
    <path class="arrow-key" d="M428,170 C500,170 522,158 552,100" marker-end="url(#ah-b-key)" />
    <text class="cap" x="470" y="196" text-anchor="middle">still current?</text>
  </svg>
  <figcaption>Push costs the writer a fan-out that grows with the fleet and gives it an impossible decision when one node is unreachable. Pull costs every read one small lookup, which is the path you were already paying for.</figcaption>
</figure>

Holding no durable state is what makes it survivable. You can kill a witness and start another one immediately, with no state to transfer and no log to replay, and a fresh witness that has never heard of your key cannot tell you your cached copy is current, so it says stale and you go read the real thing. Being wrong about what it knows costs you a slower read and never a wrong one.

<figure class="cache-bench">
  <h3>The read path, and what happens when the witness has nothing</h3>
  <svg class="cb-svg" viewBox="0 0 640 240" role="img" aria-labelledby="read-path-title read-path-desc">
    <title id="read-path-title">Read path through cache and witness</title>
    <desc id="read-path-desc">A read reaches a cache holding version 7. The cache asks the witness whether version 7 is still current. If yes, it serves from cache. If no, or if the witness has no record of the key, the read goes to the persistence tier.</desc>
    <defs>
      <marker id="ah-c" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
      <marker id="ah-c-ok" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
    </defs>
    <rect class="node" x="16" y="94" width="110" height="44" rx="6" />
    <text class="t" x="71" y="121" text-anchor="middle">GET key</text>
    <path class="arrow" d="M126,116 H162" marker-end="url(#ah-c)" />
    <rect class="node" x="170" y="94" width="126" height="44" rx="6" />
    <text class="t" x="233" y="121" text-anchor="middle">cache holds v7</text>
    <path class="arrow" d="M296,116 H322" marker-end="url(#ah-c)" />
    <polygon class="node-key" points="406,72 486,116 406,160 326,116" />
    <text class="t" x="406" y="112" text-anchor="middle">witness:</text>
    <text class="t" x="406" y="129" text-anchor="middle">still v7?</text>
    <path class="arrow" d="M486,116 C508,116 508,70 524,70" marker-end="url(#ah-c-ok)" />
    <text class="cap" x="498" y="62">yes</text>
    <rect class="node-ok" x="530" y="48" width="94" height="44" rx="6" />
    <text class="t" x="577" y="75" text-anchor="middle">serve cached</text>
    <path class="arrow" d="M406,160 V202 H524" marker-end="url(#ah-c)" />
    <text class="cap" x="414" y="180">no, or the witness</text>
    <text class="cap" x="414" y="194">has no record</text>
    <rect class="node-bad" x="530" y="180" width="94" height="44" rx="6" />
    <text class="t" x="577" y="200" text-anchor="middle">persistence</text>
    <text class="t" x="577" y="216" text-anchor="middle">tier</text>
  </svg>
  <figcaption>An empty witness sends everyone down the slow branch, which is why one can be killed and replaced without a handover. Not knowing costs latency and never correctness.</figcaption>
</figure>

## What it costs

It isn't free and the announcement language hides that reasonably well.

Every read now takes an extra hop, and not just the misses, every GET and every LIST whether it hits or not. It's a small hop, an in-memory lookup returning roughly a key and an integer, well under a millisecond against an S3 GET measured in tens of them. But no performance tradeoff means it fits inside the latency budget they already had, not that the work isn't happening.

The guarantee is also narrower than people assume when they hear strongly consistent. It's per key. No atomic updates across multiple keys, no object locking, and two concurrent PUTs to the same key are still last writer wins with no ordering you can rely on.

That last one is not an oversight, it drops straight out of the mechanism, because ordering that only exists per object can only buy you guarantees per object. It's also the honest answer to what Iceberg, Delta Lake and Hudi are for. They build multi-object atomicity on top of single-key strong consistency. (If you have ever wondered why a table format needs a manifest at all, that's why.)

## Does this break CAP

Strong consistency alongside four nines of availability sounds like someone beat the theorem. It doesn't, because the word availability is doing two different jobs in that sentence.

CAP availability is absolute, meaning every non-failing node returns a non-error response, always, and that's a 100% requirement with no budget attached. 99.99% is an SLA, roughly 52 minutes a year where you're allowed to be down, and that gap is the room a CP system lives in. S3's metadata path is CP and it pays for that out of its error budget.

The 2020 change genuinely moved it along that axis. The old cache was designed to keep answering during impairment, which is an AP choice, made on purpose. Strong consistency means giving it up.

Where the cost shows up is worth being specific about.

- Reader can't reach the witness: it skips the fast path and reads the persistence tier, which is slower and still correct, and isn't really an availability loss since the persistence tier was always the real source of truth.
- Writer can't reach the witness: the write can't be acknowledged and it fails. That's the actual CAP tax and it sits entirely on writes.
- Persistence tier partitioned past quorum: you're unavailable, but you were equally unavailable in 2006.

So reads degrade into being slow and writes are the ones that get refused, which is a deliberate split rather than something that fell out by accident.

PACELC describes it better than CAP does. Before 2020, S3's metadata path was PA/EL, available under partition and preferring latency over consistency even when nothing was wrong. After, it's PC/EC, refusing writes rather than diverging and accepting the witness hop in normal operation to get consistency. The else-latency half is that extra hop, and PACELC is the framework that makes you write it down as a price instead of pretending it's free.

## Building this in your own system

The pieces are less exotic than they sound, but the order matters because two of them are load-bearing and the rest is detail.

You need per-key ordering before anything else. Every update needs a version number that only moves forward, assigned by whoever owns that key, and if your writes don't already carry something like that then you have no way to express stale and none of the rest works. S3 got this free because replication already needed it. You probably have it too, in a row version, an offset, a sequence number, an `updated_at` that is genuinely monotonic and not just usually monotonic. If you don't, that's the first thing to build.

Then the witness, which is small. A map from key to current version, in memory, no disk and no replication, fed by whoever does the writing. It does not hold your data, and that is the whole reason it can be fast and disposable.

Then get the direction right, because this is where people go wrong. The instinct is to have the writer push invalidations to every cache that might be holding the key, which means fan-out that grows with your fleet and a decision to make when one of those caches doesn't answer, and both available answers are bad. Have the writer tell one place and have the readers ask. Reads are already your frequent path, so you're adding a cheap lookup to something you were doing anyway instead of adding an unbounded broadcast to your writes.

Make it fail closed, which comes down to one rule: if the witness has never heard of your key, it says stale. Not unknown, not assume-fresh, stale. A restarted witness with an empty map then degrades into everyone reading the real store for a while, which is slow and correct, and that is exactly what lets you kill and replace it without ceremony.

Two things to budget for. Writes now depend on the witness being reachable, because you can't acknowledge a write whose version nobody recorded, so that's where your availability cost lands. And the whole thing should live inside one failure domain, one region or one cluster or whatever your unit is, because the moment a cross-region link can sit between a reader and its witness you've taken a partition risk that buys you nothing.

What you don't get is anything across keys. Per-key ordering gives you per-key guarantees and that is the end of it. If you need more, you're not building a witness, you're building a commit log with a manifest.

And the honest version for most people is that you don't build the witness at all, you do what everyone did before 2020 and put a small strongly consistent store in front, Postgres or etcd or DynamoDB, holding key and version. Then the decision is how much you trust it. Netflix's s3mper kept S3 authoritative and used DynamoDB only to notice that a listing looked short, at which point the job waited and listed again until the missing files turned up, so the worst case was a slow job. S3Guard made DynamoDB authoritative and answered existence questions itself, tombstones included, without asking S3 at all, which is faster and stronger right up until the table drifts, and then you get new datasets missing from listings, deletes that look like they never happened, and objects silently overwritten.

Start advisory. You can tighten it later, and slow is a much easier thing to explain than wrong.

## The takeaway

Take the CP side, then spend your effort making partitions rare, small and short enough that your error budget swallows them. You get there by keeping the consistency dependency inside one region and keeping the thing that holds it disposable enough to replace in seconds.

And one thing 2020 did not fix. The commit is still a rename, and a rename on object storage is still a LIST, then a COPY of every file, then a DELETE of every original. That was wrong before and now it's merely slow, so a job producing 200,000 small files still pays 200,000 copies and 200,000 deletes at the end, mostly serialised through the driver, and you can sit there watching a stage that finished in four minutes take forty minutes to commit. Strong consistency does not touch that number.

<figure class="cache-bench">
  <h3>Two ways to commit</h3>
  <svg class="cb-svg" viewBox="0 0 640 180" role="img" aria-labelledby="commit-title commit-desc">
    <title id="commit-title">Commit by rename compared with commit by pointer swap</title>
    <desc id="commit-desc">Committing by rename requires a LIST, a COPY of every object and a DELETE of every original, so the cost scales with file count. A table format writes the data files, writes a new manifest and swaps a single pointer, so the commit is one small write regardless of file count.</desc>
    <defs>
      <marker id="ah-d" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
    </defs>
    <text class="tb" x="16" y="20">commit by rename</text>
    <rect class="node-bad" x="16" y="30" width="132" height="42" rx="6" />
    <text class="t" x="82" y="56" text-anchor="middle">LIST prefix</text>
    <path class="arrow" d="M148,51 H158" marker-end="url(#ah-d)" />
    <rect class="node-bad" x="164" y="30" width="132" height="42" rx="6" />
    <text class="t" x="230" y="56" text-anchor="middle">COPY each object</text>
    <path class="arrow" d="M296,51 H306" marker-end="url(#ah-d)" />
    <rect class="node-bad" x="312" y="30" width="132" height="42" rx="6" />
    <text class="t" x="378" y="56" text-anchor="middle">DELETE each</text>
    <path class="arrow" d="M444,51 H454" marker-end="url(#ah-d)" />
    <rect class="node-bad" x="460" y="30" width="164" height="42" rx="6" />
    <text class="t" x="542" y="56" text-anchor="middle">O(n) in file count</text>
    <text class="tb" x="16" y="104">commit by pointer swap</text>
    <rect class="node-ok" x="16" y="114" width="132" height="42" rx="6" />
    <text class="t" x="82" y="140" text-anchor="middle">write data files</text>
    <path class="arrow" d="M148,135 H158" marker-end="url(#ah-d)" />
    <rect class="node-ok" x="164" y="114" width="132" height="42" rx="6" />
    <text class="t" x="230" y="140" text-anchor="middle">write manifest</text>
    <path class="arrow" d="M296,135 H306" marker-end="url(#ah-d)" />
    <rect class="node-ok" x="312" y="114" width="132" height="42" rx="6" />
    <text class="t" x="378" y="140" text-anchor="middle">swap one pointer</text>
    <path class="arrow" d="M444,135 H454" marker-end="url(#ah-d)" />
    <rect class="node-ok" x="460" y="114" width="164" height="42" rx="6" />
    <text class="t" x="542" y="140" text-anchor="middle">O(1), all or nothing</text>
  </svg>
  <figcaption>The pointer swap is a single-key write, which is exactly the guarantee S3 now gives you. Everything a table format does on top of object storage is built on that one primitive.</figcaption>
</figure>

Table formats get out of it by not moving anything. Iceberg, Delta Lake and Hudi write the data files where they'll live, then commit by writing a new metadata file and swapping a single pointer to it, so the commit is one small write no matter how many files the job produced, and it either happened or it didn't. You also stop listing directories to find out what's in the table, because the manifest already says. That's O(1) commits and multi-file atomicity built on the per-key guarantee, which is the whole reason those formats showed up when they did.
