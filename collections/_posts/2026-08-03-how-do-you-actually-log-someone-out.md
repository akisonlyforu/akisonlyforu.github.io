---
layout: "post"
title: "How Do You Actually Log Someone Out Of A JWT ?"
date: "2026-08-03T05:34:15+00:00"
description: "TLDR: you can’t. At least not the token itself."
tags: ["substack"]
categories: ["substack"]
source: "substack"
substack_id: "https://akisonlyforu.substack.com/p/how-do-you-actually-log-someone-out"
substack_url: "https://akisonlyforu.substack.com/p/how-do-you-actually-log-someone-out"
canonical_url: "https://akisonlyforu.substack.com/p/how-do-you-actually-log-someone-out"
image: "/images/substack/how-do-you-actually-log-someone-out/33ba25dbef0da0ae.jpg"
---

<!-- Generated from Substack. Edit the Substack post instead. -->

<p>JWTs are signed and carry claims. One of those is <code>exp</code>, the point after which the token should stop being accepted. That’s the only thing the token says about its own lifetime, and nothing you do afterward changes it. You can choose to log out of the app or close the tab or delete the cookie: the token is still valid right up to <code>exp</code>. If someone copied it, it still works and there is nothing you can do about it.</p>
<h2>The problem</h2>
<div class="captioned-image-container"><figure><a class="image-link image2 is-viewable-img" target="_blank" href="https://substackcdn.com/image/fetch/%24s_!DK2-!,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2Fd27a976c-3176-4d35-8836-dd484d3d702b_2448x863.png"><div class="image2-inset">
<img src="/images/substack/how-do-you-actually-log-someone-out/33ba25dbef0da0ae.jpg" width="1456" height="513" class="sizing-normal" alt="Deleting the cookie removes your copy. Any copy someone else took keeps working until exp, which is why revocation has to live somewhere the server controls." title="Deleting the cookie removes your copy. Any copy someone else took keeps working until exp, which is why revocation has to live somewhere the server controls."><div class="image-link-expand"><div class="pencraft pc-display-flex pc-gap-8 pc-reset">

</div></div>
</div></a></figure></div>
<p>So how do you actually log out, or kill a token when you need to?</p>
<div class="subscription-widget-wrap-editor"><div class="subscription-widget show-subscribe">
<div class="preamble"><p class="cta-caption">Thanks for reading Avinash’s Substack! Subscribe for free to receive new posts and support my work.</p></div>
<div class="fake-input-wrapper">
<div class="fake-input"></div>
<div class="fake-button"></div>
</div>
</div></div>
<p>It comes down to one thing: put some state back on the server, and figure out how to check it without increasing your latency.</p>
<p>One assumption before we go further: access tokens are short, 15 minutes, and the long-lived thing is an opaque refresh token you store and rotate. That choice is doing most of the work here, and everything below falls apart without it.</p>
<p>The first idea everyone has is Redis. Keep a set of revoked tokens, check it on every request. That’s the right instinct and the wrong implementation, and the reason why is a counting problem.</p>
<h2>What actually goes in Redis</h2>
<p>Before we go further, let’s clear this part. What do we store in Redis? Do we store the token itself?</p>
<p>You never store the token. It’s a live credential, and anyone who can read your cache walks away with working bearer tokens for the next fifteen minutes. It’s also ~800 bytes each. Your token carries two ids. <code>jti</code> is this specific token, minted fresh on every refresh. <code>sid</code> is the session, generated once at login and carried through every refresh for as long as the user stays logged in. One login, one sid, many jti.</p>
<p>Say someone works an 8-hour day on 15-minute tokens. That’s about 32 tokens under one sid. At logout you only hold the current one. Blacklist that jti and their next refresh hands them a brand new token with a brand new jti that isn’t on any list. You blocked a string. The session is still alive.</p>
<div class="captioned-image-container"><figure><a class="image-link image2 is-viewable-img" target="_blank" href="https://substackcdn.com/image/fetch/%24s_!q-fJ!,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2Fb0641485-d492-422f-b1a6-1a6c894078a4_2448x1103.png"><div class="image2-inset">
<img src="/images/substack/how-do-you-actually-log-someone-out/ef09607598af1bf3.png" width="1456" height="656" class="sizing-normal" alt="Same logout, two ids. The jti you happen to be holding is one string out of about thirty two, and the session mints the next one fifteen minutes later without asking anybody." title="Same logout, two ids. The jti you happen to be holding is one string out of about thirty two, and the session mints the next one fifteen minutes later without asking anybody." loading="lazy"><div class="image-link-expand"><div class="pencraft pc-display-flex pc-gap-8 pc-reset">

</div></div>
</div></a></figure></div>
<p>So you revoke the sid.</p>
<pre><code><code>SET rev:sid:a3f9c1e0 1 EX 640
</code></code></pre>
<p>Key is the session id. The value is a placeholder as existence of the key is the answer. TTL is whatever’s left of the token’s life, so Redis deletes the entry at the exact moment it stops mattering.</p>
<h2>How big does that set actually get</h2>
<p>Here’s what people get wrong. Your denylist doesn’t hold every logout that ever happened. It only holds logouts from the last token lifetime. Older ones already fail on <code>exp</code>, so those entries are doing nothing and Redis has dropped them. Entries drain out as fast as they come in. So the size isn’t your logout count. It’s peak logout rate × access token TTL. If logouts arrive at 90/sec and each entry survives 900 seconds, the steady-state count is 90 × 900 = 81,000.</p>
<p>At 40 bytes for a session id, 81,000 entries is about 3MB. Even a million entries is 40MB raw, and under 100MB once you count .NET’s object overhead. That still fits as gateway pods usually run with a 512MB to 1GB limit, but it’s heavier than it needs to be, and a million live strings sitting in gen2 get walked on every full GC.</p>
<p>The fix is to stop storing strings. Hash the sid to 64 bits and keep a <code>HashSet&lt;long&gt;</code>. No object per entry, nothing for the collector to trace, and a million entries drops to ~16MB. A hash collision would reject one valid session, not admit a revoked one, so the failure direction is the safe one.</p>
<div class="captioned-image-container"><figure><a class="image-link image2 is-viewable-img" target="_blank" href="https://substackcdn.com/image/fetch/%24s_!4lyB!,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2Fa0b20932-79b2-47f3-909f-0647ecd676eb_2448x1052.png"><div class="image2-inset">
<img src="/images/substack/how-do-you-actually-log-someone-out/e86157c1cd5ac280.jpg" width="1456" height="626" class="sizing-normal" alt="Arithmetic, not a measurement. Because the size follows from the rate and the TTL, a logout spike costs you a bigger set for exactly one token lifetime and then it drains itself." title="Arithmetic, not a measurement. Because the size follows from the rate and the TTL, a logout spike costs you a bigger set for exactly one token lifetime and then it drains itself." loading="lazy"><div class="image-link-expand"><div class="pencraft pc-display-flex pc-gap-8 pc-reset">

</div></div>
</div></a></figure></div>
<p>One more assumption: all of this happens at the gateway. It’s the only entry point, so it’s the only place that needs the set. That’s a handful of pods holding 10MB each, not every service in your fleet. The tradeoff is that everything behind the gateway now trusts whatever identity it forwards, so that internal boundary has to be real. Otherwise anything that can reach a service directly skips auth entirely.</p>
<h2>Getting the set into every pod</h2>
<p>So how does the set get into every pod?</p>
<p>Redis is still the source of truth but instead of it being the source of truth for a request, each gateway pod will just keep a copy. There are two mechanisms to orchestrate it with.</p>
<p>Push. Logout writes the key to Redis and publishes on a channel. Every pod is subscribed and adds the sid to its local set within a few milliseconds. This is the fast path and it’s what makes revocation feel instant.</p>
<p>Reconcile. Every second or two, each pod pulls the full current set from Redis and swaps it in.</p>
<p>Redis pub/sub is fire-and-forget. There’s no acknowledgement, no replay, no delivery guarantee. A pod that was mid-startup when the message went out never sees it. A pod that had a two-second network blip never sees it. The poll is what heals that.</p>
<p>Recommended: schedule the polls with some ms of jitter. Otherwise all fifty pods hit Redis at the same instant, every two seconds, forever.</p>
<p>One more thing which is easy to miss. A pod that starts with an empty set accepts everything until its first reconcile lands. During a rolling deploy that’s a real window. So a cold pod does a full load before it takes traffic: subscribe to the channel first, then pull the set, otherwise a revocation landing between the two gets missed by both. Use <code>SCAN</code>, not <code>KEYS</code>. Redis is single-threaded and <code>KEYS</code> walks the whole keyspace in one uninterruptible go, while <code>SCAN</code> returns a cursor and a small batch at a time, so nothing stalls.</p>
<div class="captioned-image-container"><figure><a class="image-link image2 is-viewable-img" target="_blank" href="https://substackcdn.com/image/fetch/%24s_!7ad6!,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2Fe6c23470-b511-4e8d-96d1-aba5e88371d7_2448x1169.png"><div class="image2-inset">
<img src="/images/substack/how-do-you-actually-log-someone-out/dfc104fde2a8e93c.jpg" width="1456" height="695" class="sizing-normal" alt="Pub/sub has no acknowledgement and no replay, so a pod that was starting, or that blipped for two seconds, only finds out on its next poll." title="Pub/sub has no acknowledgement and no replay, so a pod that was starting, or that blipped for two seconds, only finds out on its next poll." loading="lazy"><div class="image-link-expand"><div class="pencraft pc-display-flex pc-gap-8 pc-reset">

</div></div>
</div></a></figure></div>
<h2>What actually happens on a request</h2>
<p>Logout: the gateway already parsed the token, so it has the sid and exp. It writes <code>rev:sid:{sid}</code> with a TTL of the remaining token life, publishes the sid on the channel, and deletes the refresh token row so the session can’t be extended. Three operations, once per logout.</p>
<p>Order matters here. Verify signature and exp first, because those reject forged and expired tokens for free.</p>
<pre><code><code>if (_revoked.Contains(Hash(sid))) return Unauthorized();
if (_userCutoff.TryGetValue(uid, out var uc) &amp;&amp; uc &gt; iat) return Unauthorized();
if (_globalCutoff &gt; iat) return Unauthorized();
</code></code></pre>
<h2>What if you need to kill everything at once</h2>
<p>A breach, a leaked signing key, or just someone hitting “log out all devices” on their account page. Per-session revocation will break here. Millions of active sessions can’t each have an entry, it won’t scale. We don’t have to make millions of decisions though, just one.</p>
<pre><code><code>SET rev:global &lt;now, unix seconds&gt; EX 900
</code></code></pre>
<p>One key holding a timestamp. Any token whose <code>iat</code> is older than that gets rejected. Ten million sessions gone, one entry, one message on the channel.</p>
<p>Same shape but one level down. <code>rev:usr:{uid}</code> holds a timestamp too, and that’s our “log this user out everywhere”. One entry regardless of number of devices. And <code>rev:sid:{sid}</code> is the per-session case we already have. So there are 3 keys with three scopes: one session, one user’s sessions, everyone.</p>
<div class="captioned-image-container"><figure><a class="image-link image2 is-viewable-img" target="_blank" href="https://substackcdn.com/image/fetch/%24s_!h590!,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2F1181f1c7-8de7-41ee-87ee-b3bb255419d5_2448x980.png"><div class="image2-inset">
<img src="/images/substack/how-do-you-actually-log-someone-out/78587a88ffbba00f.png" width="1456" height="583" class="sizing-normal" alt="The bottom row is the breach case. Ten million sessions and it&#x27;s still one entry, because the timestamp applies to all of them without listing any of them." title="The bottom row is the breach case. Ten million sessions and it&#x27;s still one entry, because the timestamp applies to all of them without listing any of them." loading="lazy"><div class="image-link-expand"><div class="pencraft pc-display-flex pc-gap-8 pc-reset">

</div></div>
</div></a></figure></div>
<h2>What to watch when this scales</h2>
<p>Skew between pods. Pod A reconciled at t=0, pod B at t=1.5s. A logout at t=1.0s means that for one second the same token is accepted by one pod and rejected by another, depending on which one the load balancer picked. Users see a flaky logout and it’s hard to reproduce. We can’t remove this but only bound it. Pick your reconcile interval knowing that’s the number you’re choosing.</p>
<p>Redis going down. Serve the last known snapshot, or 401 everyone. Failing open and allowing everyone is usually the right call. The worst case is that a logout from the last few minutes hasn’t reached every pod yet. The alternative is nobody can log in at all, which is destructive. If you fail open, don’t just monitor Redis. Monitor how stale each pod’s copy is. Redis can be perfectly healthy while a pod sits on data from four minutes ago.</p>
<p>For payments, admin, anything you can’t undo: skip the local set and hit Redis directly. That’s a network call, but it’s on a small fraction of your traffic, and those are the routes where you’d rather 401 someone than let a revoked session through.</p>
<p>Someone bumping the TTL. The whole design rests on rate × TTL being small. Move access tokens to 24 hours and 2M logouts/day becomes 2M live entries. Whoever owns that config should know it’s load-bearing, because it won’t look like an auth change when it happens.</p>
<p>Clock drift. You compute the TTL as <code>exp - now</code>. If the pod handling logout runs 30 seconds fast, the entry evicts while the token is still being accepted everywhere else, and the token comes back to life. Pad the TTL by a minute. An extra minute of storage costs nothing. Evicting early means the token starts working again.</p>
<h2>Where do we stand after this</h2>
<p>None of this makes a JWT revocable. The token is still a signed statement that stays true until it expires. What you’re doing is keeping a small, short-lived list of sessions you’ve decided to stop trusting, and making sure every pod can check it without asking anyone.</p>
<p>That’s it. The rest is picking a TTL you can live with.</p>
<div class="subscription-widget-wrap-editor"><div class="subscription-widget show-subscribe">
<div class="preamble"><p class="cta-caption">Thanks for reading Avinash’s Substack! Subscribe for free to receive new posts and support my work.</p></div>
<div class="fake-input-wrapper">
<div class="fake-input"></div>
<div class="fake-button"></div>
</div>
</div></div>
