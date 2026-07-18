"""Proof that Redis pub/sub delivers a message to the publishing node itself,
and that an origin-id envelope filter fixes it.

This is a behavior demo, not a performance benchmark. It runs two scenarios
against a real Redis and prints exactly which node processed which event, so a
reader can watch the self-delivery happen and then watch the filter stop it.

    REDIS_HOST / REDIS_PORT override the connection (default 127.0.0.1:6390).
"""
import json
import os
import threading
import time

import redis

HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("REDIS_PORT", "6390"))
CHANNEL = "cache:invalidations"
NODES = ("node-A", "node-B")


def client():
    return redis.Redis(host=HOST, port=PORT, decode_responses=True)


def run_scenario(filter_self):
    """Two nodes subscribe to CHANNEL. node-A publishes one event.
    Returns {node_id: [events it PROCESSED]}."""
    processed = {n: [] for n in NODES}
    stop = threading.Event()
    ready = threading.Barrier(len(NODES) + 1)

    def subscriber(node_id):
        sub = client().pubsub(ignore_subscribe_messages=True)
        sub.subscribe(CHANNEL)
        ready.wait()  # signal this subscription is live
        while not stop.is_set():
            msg = sub.get_message(timeout=0.2)
            if not msg or msg.get("type") != "message":
                continue
            event = json.loads(msg["data"])
            if filter_self and event["origin"] == node_id:
                print(f"  [{node_id}] received event from {event['origin']}  ->  SKIP (mine)")
                continue
            processed[node_id].append(event)
            tag = "  <-- its own message!" if event["origin"] == node_id else ""
            print(f"  [{node_id}] PROCESS invalidation {event['key']}  (published by {event['origin']}){tag}")
        sub.close()

    threads = [threading.Thread(target=subscriber, args=(n,), daemon=True) for n in NODES]
    for t in threads:
        t.start()
    ready.wait()          # all subscriptions confirmed live
    time.sleep(0.2)

    event = {"origin": "node-A", "key": "user:42"}
    print(f"  node-A publishes {json.dumps(event)} on '{CHANNEL}'")
    client().publish(CHANNEL, json.dumps(event))

    time.sleep(0.6)
    stop.set()
    for t in threads:
        t.join(timeout=1)
    return processed


def main():
    client().ping()  # fail fast if Redis is unreachable
    print("=" * 60)
    print("SCENARIO 1  no origin filter  (the trap)")
    print("=" * 60)
    r1 = run_scenario(filter_self=False)
    print(f"  => node-A processed {len(r1['node-A'])} event it published itself")
    print(f"     node-B processed {len(r1['node-B'])} event")
    print()
    print("=" * 60)
    print("SCENARIO 2  origin-id filter on  (the fix)")
    print("=" * 60)
    r2 = run_scenario(filter_self=True)
    print(f"  => node-A processed {len(r2['node-A'])} of its own events")
    print(f"     node-B processed {len(r2['node-B'])} event")


if __name__ == "__main__":
    main()
