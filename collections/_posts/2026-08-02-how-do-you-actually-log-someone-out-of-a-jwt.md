---
layout:     post
title:      How Do You Actually Log Someone Out Of A JWT
date:       2026-08-02
description:    You can't revoke the token itself, exp is the only thing it says about its own lifetime. What actually logging someone out takes, what goes in Redis and why it is never the token, how big the denylist really gets, and how to check it without a network hop per request.
categories: jwt auth redis distributed-systems dotnet
---

TLDR: you can't. At least not the token itself.

JWTs are signed and carry claims. One of those is `exp`, the point after which the token should stop being accepted. That's the only thing the token says about its own lifetime, and nothing you do afterward changes it. Log out of the app, close the tab, delete the cookie: the token is still valid right up to `exp`. If someone copied it, it still works.

## The problem

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
.cb-svg .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; fill: var(--cb-text); }
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
  <h3>What logging out does to the token</h3>
  <svg class="cb-svg" viewBox="0 0 640 168" role="img" aria-labelledby="jwt-life-title jwt-life-desc">
    <title id="jwt-life-title">An access token stays valid through logout until exp</title>
    <desc id="jwt-life-desc">A fifteen minute access token is minted at login. The user logs out partway through, which deletes the cookie in the browser, and the token itself keeps being accepted by every service until its exp timestamp is reached.</desc>
    <rect class="node-ok" x="40" y="46" width="256" height="34" rx="6" />
    <text class="t" x="168" y="68" text-anchor="middle">valid</text>
    <rect class="node-bad" x="300" y="46" width="316" height="34" rx="6" />
    <text class="t" x="458" y="68" text-anchor="middle">still valid, still accepted everywhere</text>
    <text class="tb" x="16" y="24">one access token, 15 minute exp</text>
    <line class="grid" x1="40" y1="86" x2="40" y2="110" />
    <line class="grid" x1="300" y1="86" x2="300" y2="110" />
    <line class="grid" x1="616" y1="86" x2="616" y2="110" />
    <text class="tb" x="40" y="128">login</text>
    <text class="cap" x="40" y="146">token minted, exp stamped</text>
    <text class="tb" x="300" y="128" text-anchor="middle">logout</text>
    <text class="cap" x="300" y="146" text-anchor="middle">cookie deleted, tab closed</text>
    <text class="tb" x="616" y="128" text-anchor="end">exp</text>
    <text class="cap" x="616" y="146" text-anchor="end">token stops being accepted</text>
  </svg>
  <figcaption>Deleting the cookie removes your copy. Any copy someone else took keeps working until exp, which is why revocation has to live somewhere the server controls.</figcaption>
</figure>

So how do you actually log out, or kill a token when you need to?

It comes down to one thing: put some state back on the server, and figure out how to check it without increasing your latency.

One assumption before we go further: access tokens are short, 15 minutes, and the long-lived thing is an opaque refresh token you store and rotate. That choice is doing most of the work here, and everything below falls apart without it.

The first idea everyone has is Redis. Keep a set of revoked tokens, check it on every request. That's the right instinct and the wrong implementation, and the reason why is a counting problem.

## What actually goes in Redis

Before we go further, let's clear this part. What do we store in Redis? Do we store the token itself?

You never store the token. It's a live credential, and anyone who can read your cache walks away with working bearer tokens for the next fifteen minutes. It's also ~800 bytes each. Your token carries two ids. `jti` is this specific token, minted fresh on every refresh. `sid` is the session, generated once at login and carried through every refresh for as long as the user stays logged in. One login, one sid, many jti.

Say someone works an 8-hour day on 15-minute tokens. That's about 32 tokens under one sid. At logout you only hold the current one. Blacklist that jti and their next refresh hands them a brand new token with a brand new jti that isn't on any list. You blocked a string. The session is still alive.

<figure class="cache-bench">
  <h3>What you revoke decides what survives</h3>
  <svg class="cb-svg" viewBox="0 0 640 234" role="img" aria-labelledby="jwt-ids-title jwt-ids-desc">
    <title id="jwt-ids-title">Revoking a jti compared with revoking a sid</title>
    <desc id="jwt-ids-desc">One login produces one sid and about thirty two jti values over an eight hour day. Revoking the jti you are holding blocks a single token, and the next refresh mints another one under the same session. Revoking the sid rejects the refresh and kills every token under that login.</desc>
    <defs>
      <marker id="ah-j" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
      <marker id="ah-j-bad" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead-bad" />
      </marker>
    </defs>
    <rect class="node-key" x="16" y="24" width="608" height="80" rx="6" />
    <text class="tb" x="30" y="46">sid a3f9c1e0, one login, 8 hours</text>
    <rect class="node" x="30" y="58" width="84" height="34" rx="6" />
    <text class="t" x="72" y="80" text-anchor="middle">jti 1</text>
    <rect class="node" x="122" y="58" width="84" height="34" rx="6" />
    <text class="t" x="164" y="80" text-anchor="middle">jti 2</text>
    <rect class="node" x="214" y="58" width="84" height="34" rx="6" />
    <text class="t" x="256" y="80" text-anchor="middle">jti 3</text>
    <rect class="node-bad" x="306" y="58" width="84" height="34" rx="6" />
    <text class="t" x="348" y="80" text-anchor="middle">jti 4</text>
    <rect class="node" x="398" y="58" width="84" height="34" rx="6" />
    <text class="t" x="440" y="80" text-anchor="middle">jti 5</text>
    <text class="t" x="500" y="80" text-anchor="middle">…</text>
    <rect class="node" x="520" y="58" width="90" height="34" rx="6" />
    <text class="t" x="565" y="80" text-anchor="middle">jti 32</text>
    <path class="arrow-bad" d="M161,104 V126" marker-end="url(#ah-j-bad)" />
    <rect class="node-bad" x="16" y="130" width="290" height="34" rx="6" />
    <text class="t" x="161" y="152" text-anchor="middle">revoke jti 4, the one you hold</text>
    <path class="arrow-bad" d="M161,164 V184" marker-end="url(#ah-j-bad)" />
    <rect class="node-bad" x="16" y="188" width="290" height="34" rx="6" />
    <text class="t" x="161" y="210" text-anchor="middle">refresh mints jti 5, session alive</text>
    <path class="arrow" d="M479,104 V126" marker-end="url(#ah-j)" />
    <rect class="node-ok" x="334" y="130" width="290" height="34" rx="6" />
    <text class="t" x="479" y="152" text-anchor="middle">revoke sid a3f9c1e0</text>
    <path class="arrow" d="M479,164 V184" marker-end="url(#ah-j)" />
    <rect class="node-ok" x="334" y="188" width="290" height="34" rx="6" />
    <text class="t" x="479" y="210" text-anchor="middle">refresh rejected, all 32 dead</text>
  </svg>
  <figcaption>Same logout, two ids. The jti you happen to be holding is one string out of about thirty two, and the session mints the next one fifteen minutes later without asking anybody.</figcaption>
</figure>

So you revoke the sid.

```plaintext
SET rev:sid:a3f9c1e0 1 EX 640
```

Key is the session id. The value is a placeholder as existence of the key is the answer. TTL is whatever's left of the token's life, so Redis deletes the entry at the exact moment it stops mattering.

## How big does that set actually get

Here's what people get wrong. Your denylist doesn't hold every logout that ever happened. It only holds logouts from the last token lifetime. Older ones already fail on `exp`, so those entries are doing nothing and Redis has dropped them. Entries drain out as fast as they come in. So the size isn't your logout count. It's peak logout rate × access token TTL. If logouts arrive at 90/sec and each entry survives 900 seconds, the steady-state count is 90 × 900 = 81,000.

At 40 bytes for a session id, 81,000 entries is about 3MB. Even a million entries is 40MB raw, and under 100MB once you count .NET's object overhead. That still fits as gateway pods usually run with a 512MB to 1GB limit, but it's heavier than it needs to be, and a million live strings sitting in gen2 get walked on every full GC.

The fix is to stop storing strings. Hash the sid to 64 bits and keep a `HashSet<long>`. No object per entry, nothing for the collector to trace, and a million entries drops to ~16MB. A hash collision would reject one valid session, not admit a revoked one, so the failure direction is the safe one.

<figure class="cache-bench">
  <h3>Steady state, and what it costs the pod</h3>
  <svg class="cb-svg" viewBox="0 0 640 220" role="img" aria-labelledby="jwt-size-title jwt-size-desc">
    <title id="jwt-size-title">Denylist steady state and per-pod memory</title>
    <desc id="jwt-size-desc">Logouts arrive at ninety per second and each entry lives for a nine hundred second token lifetime, so the live key count settles at eighty one thousand. Storing a million entries as strings costs under a hundred megabytes and a million objects for the garbage collector, while a HashSet of longs costs about sixteen megabytes and no objects.</desc>
    <defs>
      <marker id="ah-r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
    </defs>
    <text class="tb" x="16" y="18">the set drains as fast as it fills</text>
    <rect class="node" x="16" y="30" width="150" height="48" rx="6" />
    <text class="tb" x="91" y="50" text-anchor="middle">logouts in</text>
    <text class="t" x="91" y="68" text-anchor="middle">90 per second</text>
    <path class="arrow" d="M166,54 H198" marker-end="url(#ah-r)" />
    <rect class="node-key" x="202" y="30" width="236" height="48" rx="6" />
    <text class="tb" x="320" y="50" text-anchor="middle">live keys</text>
    <text class="t" x="320" y="68" text-anchor="middle">90 × 900 = 81,000</text>
    <path class="arrow" d="M438,54 H470" marker-end="url(#ah-r)" />
    <rect class="node" x="474" y="30" width="150" height="48" rx="6" />
    <text class="tb" x="549" y="50" text-anchor="middle">TTL expiry out</text>
    <text class="t" x="549" y="68" text-anchor="middle">90 per second</text>
    <text class="cap" x="320" y="98" text-anchor="middle">an older entry does nothing, exp already rejects that token</text>
    <line class="grid" x1="16" y1="112" x2="624" y2="112" />
    <text class="tb" x="16" y="136">a million entries, held in the pod</text>
    <rect class="node-bad" x="16" y="148" width="298" height="58" rx="6" />
    <text class="t" x="165" y="172" text-anchor="middle">strings, under 100MB</text>
    <text class="cap" x="165" y="192" text-anchor="middle">a million objects for every full GC to walk</text>
    <rect class="node-ok" x="326" y="148" width="298" height="58" rx="6" />
    <text class="t" x="475" y="172" text-anchor="middle">HashSet&lt;long&gt;, about 16MB</text>
    <text class="cap" x="475" y="192" text-anchor="middle">no object per entry, nothing to trace</text>
  </svg>
  <figcaption>Arithmetic, not a measurement. Because the size follows from the rate and the TTL, a logout spike costs you a bigger set for exactly one token lifetime and then it drains itself.</figcaption>
</figure>

One more assumption: all of this happens at the gateway. It's the only entry point, so it's the only place that needs the set. That's a handful of pods holding 10MB each, not every service in your fleet. The tradeoff is that everything behind the gateway now trusts whatever identity it forwards, so that internal boundary has to be real. Otherwise anything that can reach a service directly skips auth entirely.

## Getting the set into every pod

So how does the set get into every pod?

Redis is still the source of truth but instead of it being the source of truth for a request, each gateway pod will just keep a copy. There are two mechanisms to orchestrate it with.

Push. Logout writes the key to Redis and publishes on a channel. Every pod is subscribed and adds the sid to its local set within a few milliseconds. This is the fast path and it's what makes revocation feel instant.

Reconcile. Every second or two, each pod pulls the full current set from Redis and swaps it in.

Redis pub/sub is fire-and-forget. There's no acknowledgement, no replay, no delivery guarantee. A pod that was mid-startup when the message went out never sees it. A pod that had a two-second network blip never sees it. The poll is what heals that.

Recommended: schedule the polls with some ms of jitter. Otherwise all fifty pods hit Redis at the same instant, every two seconds, forever.

One more thing which is easy to miss. A pod that starts with an empty set accepts everything until its first reconcile lands. During a rolling deploy that's a real window. So a cold pod does a full load before it takes traffic: subscribe to the channel first, then pull the set, otherwise a revocation landing between the two gets missed by both. Use `SCAN`, not `KEYS`. Redis is single-threaded and `KEYS` walks the whole keyspace in one uninterruptible go, while `SCAN` returns a cursor and a small batch at a time, so nothing stalls.

<figure class="cache-bench">
  <h3>Push for speed, poll for what push missed</h3>
  <svg class="cb-svg" viewBox="0 0 640 252" role="img" aria-labelledby="jwt-fanout-title jwt-fanout-desc">
    <title id="jwt-fanout-title">Publishing a revocation to every gateway pod, and reconciling</title>
    <desc id="jwt-fanout-desc">The logout handler writes the revocation key to Redis and publishes it. Two pods add it to their local set within milliseconds, while a pod that was mid startup never receives the message. Every couple of seconds each pod SCANs the keyspace and swaps in the full set, which is how the pod that missed the message catches up.</desc>
    <defs>
      <marker id="ah-p" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead" />
      </marker>
      <marker id="ah-p-key" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="ahead-key" />
      </marker>
    </defs>
    <rect class="node" x="16" y="34" width="128" height="42" rx="6" />
    <text class="t" x="80" y="60" text-anchor="middle">logout handler</text>
    <path class="arrow" d="M144,55 H172" marker-end="url(#ah-p)" />
    <rect class="node-key" x="176" y="30" width="176" height="50" rx="6" />
    <text class="tb" x="264" y="52" text-anchor="middle">Redis</text>
    <text class="cap" x="264" y="70" text-anchor="middle">SET rev:sid:a3f9c1e0 EX 640</text>
    <text class="cap" x="378" y="134" text-anchor="middle">PUBLISH</text>
    <path class="arrow-key" d="M352,55 C380,55 378,24 400,24" marker-end="url(#ah-p-key)" />
    <path class="arrow-key" d="M352,55 C380,55 378,64 400,64" marker-end="url(#ah-p-key)" />
    <path class="arrow-key" d="M352,55 C380,55 378,104 400,104" marker-end="url(#ah-p-key)" />
    <rect class="node-ok" x="404" y="8" width="220" height="32" rx="6" />
    <text class="t" x="514" y="28" text-anchor="middle">pod A, set updated in ms</text>
    <rect class="node-bad" x="404" y="48" width="220" height="32" rx="6" />
    <text class="t" x="514" y="68" text-anchor="middle">pod B, mid startup, missed it</text>
    <rect class="node-ok" x="404" y="88" width="220" height="32" rx="6" />
    <text class="t" x="514" y="108" text-anchor="middle">pod C, set updated in ms</text>
    <line class="grid" x1="16" y1="140" x2="624" y2="140" />
    <text class="tb" x="16" y="164">every couple of seconds, with jitter</text>
    <rect class="node" x="16" y="176" width="180" height="38" rx="6" />
    <text class="t" x="106" y="200" text-anchor="middle">SCAN rev:sid:*</text>
    <path class="arrow" d="M196,195 H224" marker-end="url(#ah-p)" />
    <rect class="node" x="228" y="176" width="180" height="38" rx="6" />
    <text class="t" x="318" y="200" text-anchor="middle">swap the whole set in</text>
    <path class="arrow" d="M408,195 H436" marker-end="url(#ah-p)" />
    <rect class="node-ok" x="440" y="176" width="184" height="38" rx="6" />
    <text class="t" x="532" y="200" text-anchor="middle">pod B catches up</text>
    <text class="cap" x="16" y="240">a cold pod subscribes first, then loads, then takes traffic</text>
  </svg>
  <figcaption>Pub/sub has no acknowledgement and no replay, so a pod that was starting, or that blipped for two seconds, only finds out on its next poll.</figcaption>
</figure>

## What actually happens on a request

Logout: the gateway already parsed the token, so it has the sid and exp. It writes `rev:sid:{sid}` with a TTL of the remaining token life, publishes the sid on the channel, and deletes the refresh token row so the session can't be extended. Three operations, once per logout.

Order matters here. Verify signature and exp first, because those reject forged and expired tokens for free.

```csharp
if (_revoked.Contains(Hash(sid))) return Unauthorized();
if (_userCutoff.TryGetValue(uid, out var uc) && uc > iat) return Unauthorized();
if (_globalCutoff > iat) return Unauthorized();
```

## What if you need to kill everything at once

A breach, a leaked signing key, or just someone hitting "log out all devices" on their account page. Per-session revocation will break here. Millions of active sessions can't each have an entry, it won't scale. We don't have to make millions of decisions though, just one.

```plaintext
SET rev:global <now, unix seconds> EX 900
```

One key holding a timestamp. Any token whose `iat` is older than that gets rejected. Ten million sessions gone, one entry, one message on the channel.

Same shape but one level down. `rev:usr:{uid}` holds a timestamp too, and that's our "log this user out everywhere". One entry regardless of number of devices. And `rev:sid:{sid}` is the per-session case we already have. So there are 3 keys with three scopes: one session, one user's sessions, everyone.

<figure class="cache-bench">
  <h3>Three keys, three scopes</h3>
  <svg class="cb-svg" viewBox="0 0 640 200" role="img" aria-labelledby="jwt-scope-title jwt-scope-desc">
    <title id="jwt-scope-title">The three revocation keys and what each one stops</title>
    <desc id="jwt-scope-desc">A per-session key holds a placeholder and stops one login on one device. A per-user key holds a cutoff timestamp and stops every session that user has. A global key holds a cutoff timestamp and stops everyone. Each of the three is a single entry.</desc>
    <text class="tb" x="16" y="18">key</text>
    <text class="tb" x="240" y="18">value</text>
    <text class="tb" x="368" y="18">what it stops</text>
    <rect class="node" x="16" y="28" width="200" height="42" rx="6" />
    <text class="mono" x="30" y="54">rev:sid:a3f9c1e0</text>
    <rect class="node" x="224" y="28" width="128" height="42" rx="6" />
    <text class="t" x="288" y="54" text-anchor="middle">1</text>
    <rect class="node" x="360" y="28" width="264" height="42" rx="6" />
    <text class="t" x="492" y="54" text-anchor="middle">one login, on one device</text>
    <rect class="node" x="16" y="80" width="200" height="42" rx="6" />
    <text class="mono" x="30" y="106">rev:usr:8812</text>
    <rect class="node" x="224" y="80" width="128" height="42" rx="6" />
    <text class="t" x="288" y="106" text-anchor="middle">cutoff ts</text>
    <rect class="node" x="360" y="80" width="264" height="42" rx="6" />
    <text class="t" x="492" y="106" text-anchor="middle">every session that user has</text>
    <rect class="node-key" x="16" y="132" width="200" height="42" rx="6" />
    <text class="mono" x="30" y="158">rev:global</text>
    <rect class="node-key" x="224" y="132" width="128" height="42" rx="6" />
    <text class="t" x="288" y="158" text-anchor="middle">cutoff ts</text>
    <rect class="node-key" x="360" y="132" width="264" height="42" rx="6" />
    <text class="t" x="492" y="158" text-anchor="middle">everyone, every session</text>
    <text class="cap" x="16" y="192">one entry each, whatever the blast radius</text>
  </svg>
  <figcaption>The bottom row is the breach case. Ten million sessions and it's still one entry, because the timestamp applies to all of them without listing any of them.</figcaption>
</figure>

## What to watch when this scales

Skew between pods. Pod A reconciled at t=0, pod B at t=1.5s. A logout at t=1.0s means that for one second the same token is accepted by one pod and rejected by another, depending on which one the load balancer picked. Users see a flaky logout and it's hard to reproduce. We can't remove this but only bound it. Pick your reconcile interval knowing that's the number you're choosing.

Redis going down. Serve the last known snapshot, or 401 everyone. Failing open and allowing everyone is usually the right call. The worst case is that a logout from the last few minutes hasn't reached every pod yet. The alternative is nobody can log in at all, which is destructive. If you fail open, don't just monitor Redis. Monitor how stale each pod's copy is. Redis can be perfectly healthy while a pod sits on data from four minutes ago.

For payments, admin, anything you can't undo: skip the local set and hit Redis directly. That's a network call, but it's on a small fraction of your traffic, and those are the routes where you'd rather 401 someone than let a revoked session through.

Someone bumping the TTL. The whole design rests on rate × TTL being small. Move access tokens to 24 hours and 2M logouts/day becomes 2M live entries. Whoever owns that config should know it's load-bearing, because it won't look like an auth change when it happens.

Clock drift. You compute the TTL as `exp - now`. If the pod handling logout runs 30 seconds fast, the entry evicts while the token is still being accepted everywhere else, and the token comes back to life. Pad the TTL by a minute. An extra minute of storage costs nothing. Evicting early means the token starts working again.

## Where do we stand after this

None of this makes a JWT revocable. The token is still a signed statement that stays true until it expires. What you're doing is keeping a small, short-lived list of sessions you've decided to stop trusting, and making sure every pod can check it without asking anyone.

That's it. The rest is picking a TTL you can live with.
