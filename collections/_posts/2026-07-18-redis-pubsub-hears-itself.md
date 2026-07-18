---
layout:     post
title:      Your Redis Pub/Sub Node Hears Its Own Messages
date:       2026-07-18
description:    Redis pub/sub delivers every message to every subscriber on the channel, including the node that published it. If a node both publishes and subscribes, it hears its own echo. Here's why, a two-line fix, and a demo you can run to watch it happen.
categories: redis pub-sub distributed-systems operations
---

If you've run a Redis pub/sub setup where the same node both publishes to a channel and subscribes to it, you may have watched that node react to its own messages. The first time it got me it looked like a cache-invalidation bug: a node would publish "drop `user:42`" so the rest of the fleet cleared their copies, and then immediately process that same invalidation itself and reload the key it had just written. Wasted work at best, and on a busier channel it turned into nodes chattering at themselves.

It isn't a bug, it's just how the fanout works.

## The problem

A node that both publishes to a Redis channel and subscribes to it receives its own messages back. Redis fans every published message out to every subscriber on the channel with no way to skip the sender, so a node that reacts to what it just published ends up doing duplicate work or, in an invalidation setup, chasing its own tail. It isn't a bug, and the fix is small, but you have to know it's happening first.

## Why it happens

Redis pub/sub is a dumb, fast fanout, and I mean that as a compliment. You `PUBLISH` to a channel, and every client currently subscribed to that channel gets the message. Every one of them. Redis doesn't track who published, and there's no "send to everyone except the sender" option like some message brokers hand you.

A node that both publishes and subscribes is really running two connections, because a connection sitting in subscribe mode can't issue `PUBLISH` (it can only manage its subscriptions). So the node has one connection it publishes on and another it listens on. And that listening connection is a subscriber like any other on the channel, which means it receives the message the node just sent on its other connection. So the node ends up getting back a message it published.

## The fix

There's no server-side switch for this, so the filtering is yours to do, client-side. Stamp every message with the id of the node that sent it, and have each subscriber ignore anything carrying its own id.

```python
NODE_ID = "node-A"   # unique per process; a uuid generated at startup is fine

def publish_invalidation(r, key):
    r.publish("cache:invalidations", json.dumps({"origin": NODE_ID, "key": key}))

def on_message(msg):
    event = json.loads(msg["data"])
    if event["origin"] == NODE_ID:
        return               # I published this one, nothing to do
    invalidate(event["key"])
```

That's the whole idea. An envelope with an `origin` field, and one early `return` on the receiving side.

## Watch it happen

You don't have to take my word for it. The [demo is a small script and a Redis container in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/redis-pubsub-selfmsg). Two nodes subscribe to `cache:invalidations`, then `node-A` publishes exactly one event. First run has no filter:

```
============================================================
SCENARIO 1  no origin filter  (the trap)
============================================================
  node-A publishes {"origin": "node-A", "key": "user:42"} on 'cache:invalidations'
  [node-B] PROCESS invalidation user:42  (published by node-A)
  [node-A] PROCESS invalidation user:42  (published by node-A)  <-- its own message!
  => node-A processed 1 event it published itself
     node-B processed 1 event
```

`node-A` processed the event it published. Now the same run with the origin filter turned on:

```
============================================================
SCENARIO 2  origin-id filter on  (the fix)
============================================================
  node-A publishes {"origin": "node-A", "key": "user:42"} on 'cache:invalidations'
  [node-B] PROCESS invalidation user:42  (published by node-A)
  [node-A] received event from node-A  ->  SKIP (mine)
  => node-A processed 0 of its own events
     node-B processed 1 event
```

Same publish, same fanout. `node-A` sees its own message arrive and drops it, `node-B` still gets it and does the work. That's exactly the behavior you want.

## Stuff worth remembering

- Redis pub/sub has no self-exclude. Every subscriber on a channel gets every message published to it, the publisher included, and there's no flag to change that. The filtering is on you.
- A node that publishes and subscribes uses two connections, and the subscriber connection is just another subscriber, so it hears the node's own publishes.
- Put a node id in the message envelope and ignore your own on receipt. It's two lines and it saves you from a node processing its own events, which in an invalidation or event-propagation setup is how you get duplicate work or a feedback loop.
- Same thing applies to anything built on the fanout, including keyspace notifications you're also acting on from the node that caused them.

## The takeaway

Redis pub/sub will always hand a node its own messages, because the fanout doesn't know or care who published. There's no server-side switch for it, so the fix is one field: stamp every message with the id of the node that sent it, and have each subscriber drop anything carrying its own id. Two lines on the read side, and a node stops reacting to events it caused.
