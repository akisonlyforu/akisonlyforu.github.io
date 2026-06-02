"""Measure RabbitMQ's consistent-hash exchange against a real broker.

An `x-consistent-hash` exchange hashes each message's routing key onto a ring and
routes it to exactly one bound queue. The point of consistent hashing (vs a naive
`hash(key) % N`) is that adding a queue only reshuffles a small slice of the keys
instead of almost all of them. Three experiments prove that out:

  A. Distribution evenness - 8 equal-weight queues, ~100k messages over ~50k keys,
     read each queue's depth, report the spread vs the ideal total/8.
  B. Rebalance cost        - map K=10k keys across 8 queues, add a 9th, count how
     many keys changed queue. Then do the same with Python `hash % 8` vs `% 9`.
  C. Affinity              - confirm every key lands in exactly one queue (we send
     several copies of each key and check they all coalesce).

Everything routes through the real broker via pika; nothing is simulated except the
modulo baseline in B, which is the whole point of the comparison.

Env: RABBITMQ_HOST (127.0.0.1), RABBITMQ_PORT (6672 AMQP), RABBITMQ_MGMT_PORT
(16672), RESULTS_DIR (./results).
"""
import base64
import csv
import hashlib
import json
import os
import random
import statistics
import time
import urllib.request

import pika

HOST = os.environ.get("RABBITMQ_HOST", "127.0.0.1")
PORT = int(os.environ.get("RABBITMQ_PORT", "6672"))
MGMT_PORT = int(os.environ.get("RABBITMQ_MGMT_PORT", "16672"))
USER = os.environ.get("RABBITMQ_USER", "guest")
PASS = os.environ.get("RABBITMQ_PASS", "guest")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

# Digest-pinned image this harness is written against (see docker-compose.yml).
IMAGE_DIGEST = "sha256:ad4268113c27d02f08ac1151f9651d6e475c955f81c3a5ad522b79955ce11cf3"

EXCHANGE_TYPE = "x-consistent-hash"

_LINES = []


def log(msg=""):
    print(msg)
    _LINES.append(msg)


# ---------------------------------------------------------------------------
# broker helpers
# ---------------------------------------------------------------------------
def connect():
    params = pika.ConnectionParameters(
        host=HOST,
        port=PORT,
        credentials=pika.PlainCredentials(USER, PASS),
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    conn = pika.BlockingConnection(params)
    return conn, conn.channel()


def rabbitmq_version():
    url = f"http://{HOST}:{MGMT_PORT}/api/overview"
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    return data.get("rabbitmq_version") or data.get("product_version") or "unknown"


def setup(ch, exchange, queues, weight="1"):
    """Fresh exchange + queues, each bound with routing key = weight (bucket count)."""
    ch.exchange_delete(exchange=exchange)
    ch.exchange_declare(exchange=exchange, exchange_type=EXCHANGE_TYPE, durable=False)
    for q in queues:
        ch.queue_delete(queue=q)
        ch.queue_declare(queue=q, durable=False, auto_delete=False)
        ch.queue_bind(queue=q, exchange=exchange, routing_key=weight)


def bind_one(ch, exchange, q, weight="1"):
    ch.queue_delete(queue=q)
    ch.queue_declare(queue=q, durable=False, auto_delete=False)
    ch.queue_bind(queue=q, exchange=exchange, routing_key=weight)


def teardown(ch, exchange, queues):
    for q in queues:
        ch.queue_delete(queue=q)
    ch.exchange_delete(exchange=exchange)


def publish(conn, ch, exchange, items):
    """Fire-and-forget publish; routing_key is the hash key. `items` = list of keys."""
    for i, key in enumerate(items):
        ch.basic_publish(exchange=exchange, routing_key=key, body=key.encode())
        if i % 5000 == 0:
            conn.process_data_events(time_limit=0)
    conn.process_data_events(time_limit=0)


def qdepth(ch, q):
    return ch.queue_declare(queue=q, passive=True).method.message_count


def wait_settle(ch, queues, expected, timeout=60):
    """Poll queue depths until the broker has routed everything (or timeout)."""
    end = time.time() + timeout
    total = 0
    while time.time() < end:
        total = sum(qdepth(ch, q) for q in queues)
        if total >= expected:
            return total
        time.sleep(0.3)
    return total


def drain(ch, q, timeout=5):
    """Auto-ack drain every message on a queue, returning the bodies (as str)."""
    bodies = []
    for method, _props, body in ch.consume(q, auto_ack=True, inactivity_timeout=0.4):
        if method is None:
            break
        bodies.append(body.decode())
    ch.cancel()
    return bodies


def md5int(key):
    return int.from_bytes(hashlib.md5(key.encode()).digest()[:8], "big")


# ---------------------------------------------------------------------------
# A. distribution evenness
# ---------------------------------------------------------------------------
def experiment_a(conn, ch):
    exchange = "chx.a"
    n_queues = 8
    queues = [f"chx.a.q{i}" for i in range(n_queues)]
    n_msgs = 100_000
    key_space = 50_000

    setup(ch, exchange, queues)
    rng = random.Random(1234)
    keys = [f"user-{rng.randrange(key_space)}" for _ in range(n_msgs)]
    distinct = len(set(keys))

    publish(conn, ch, exchange, keys)
    routed = wait_settle(ch, queues, n_msgs)

    counts = [qdepth(ch, q) for q in queues]
    total = sum(counts)
    ideal = total / n_queues
    mn, mx = min(counts), max(counts)
    stdev = statistics.pstdev(counts)
    max_dev = max(abs(c - ideal) for c in counts)
    max_dev_pct = 100.0 * max_dev / ideal

    log("=" * 66)
    log("EXPERIMENT A  distribution evenness (8 equal-weight queues)")
    log("=" * 66)
    log(f"  published {n_msgs} messages over {distinct} distinct routing keys")
    log(f"  routed (sum of queue depths): {routed}")
    log(f"  ideal per queue (total/8)   : {ideal:.1f}")
    for q, c in zip(queues, counts):
        dev = 100.0 * (c - ideal) / ideal
        log(f"    {q:12} {c:8}   ({dev:+6.2f}% vs ideal)")
    log(f"  min={mn}  max={mx}  stdev={stdev:.1f}")
    log(f"  max deviation from ideal    : {max_dev:.0f} msgs ({max_dev_pct:.2f}%)")

    with open(os.path.join(RESULTS, "distribution.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["queue", "message_count", "ideal", "deviation", "deviation_pct"])
        for q, c in zip(queues, counts):
            w.writerow([q, c, f"{ideal:.1f}", f"{c - ideal:+.1f}",
                        f"{100.0 * (c - ideal) / ideal:+.2f}"])

    teardown(ch, exchange, queues)
    return {
        "messages": n_msgs, "distinct_keys": distinct, "queues": n_queues,
        "ideal": ideal, "counts": counts, "min": mn, "max": mx,
        "stdev": stdev, "max_dev_pct": max_dev_pct,
    }


# ---------------------------------------------------------------------------
# B + C. rebalance cost and affinity
# ---------------------------------------------------------------------------
def map_keys(conn, ch, exchange, queues, keys, copies):
    """Publish `copies` messages per key, drain, return (key -> set(queues))."""
    stream = []
    for key in keys:
        stream.extend([key] * copies)
    random.Random(99).shuffle(stream)
    publish(conn, ch, exchange, stream)
    wait_settle(ch, queues, len(stream))

    mapping = {}
    for q in queues:
        for body in drain(ch, q):
            mapping.setdefault(body, set()).add(q)
    return mapping


def experiment_b_c(conn, ch):
    exchange = "chx.b"
    k = 10_000
    copies = 3
    base_queues = [f"chx.b.q{i}" for i in range(8)]
    ninth = "chx.b.q8"
    keys = [f"user-{i}" for i in range(k)]

    # --- N = 8 ---
    setup(ch, exchange, base_queues)
    map8 = map_keys(conn, ch, exchange, base_queues, keys, copies)

    # C: affinity - every key must have landed in exactly one queue
    multi = [key for key, qs in map8.items() if len(qs) != 1]
    affinity = len(multi) == 0
    keys_seen = len(map8)
    target8 = {key: next(iter(qs)) for key, qs in map8.items()}

    # --- N = 9 (same keys, add a queue) ---
    bind_one(ch, exchange, ninth)
    all_queues = base_queues + [ninth]
    map9 = map_keys(conn, ch, exchange, all_queues, keys, copies)
    target9 = {key: next(iter(qs)) for key, qs in map9.items()}

    # consistent-hash remap: keys present in both mappings that changed queue
    common = [key for key in keys if key in target8 and key in target9]
    ch_remapped = sum(1 for key in common if target8[key] != target9[key])
    ch_pct = 100.0 * ch_remapped / len(common)

    # naive modulo baseline on the same keys
    mod_remapped = sum(1 for key in keys if md5int(key) % 8 != md5int(key) % 9)
    mod_pct = 100.0 * mod_remapped / len(keys)

    log("\n" + "=" * 66)
    log("EXPERIMENT B  rebalance cost, 8 -> 9 queues")
    log("=" * 66)
    log(f"  keys: {len(common)} distinct (routed to both the 8- and 9-queue ring)")
    log(f"  consistent hash : {ch_remapped:6} keys remapped  ({ch_pct:.2f}%)")
    log(f"  naive modulo    : {mod_remapped:6} keys remapped  ({mod_pct:.2f}%)")
    log(f"    (ideal for consistent hash is ~1/9 = 11.1%; modulo churns almost everything)")

    log("\n" + "=" * 66)
    log("EXPERIMENT C  affinity (same key -> same queue)")
    log("=" * 66)
    log(f"  {copies} copies of each of {len(keys)} keys published on the 8-queue ring")
    log(f"  keys checked           : {keys_seen}")
    log(f"  keys in >1 queue       : {len(multi)}")
    log(f"  affinity holds         : {'yes' if affinity else 'no'}")

    with open(os.path.join(RESULTS, "rebalance.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["method", "n_before", "n_after", "keys", "remapped", "remapped_pct"])
        w.writerow(["consistent_hash", 8, 9, len(common), ch_remapped, f"{ch_pct:.2f}"])
        w.writerow(["modulo", 8, 9, len(keys), mod_remapped, f"{mod_pct:.2f}"])

    teardown(ch, exchange, all_queues)
    return {
        "keys": len(common), "copies": copies,
        "ch_remapped": ch_remapped, "ch_pct": ch_pct,
        "mod_remapped": mod_remapped, "mod_pct": mod_pct,
        "affinity": affinity, "keys_checked": keys_seen, "multi": len(multi),
    }


# ---------------------------------------------------------------------------
def main():
    os.makedirs(RESULTS, exist_ok=True)
    conn, ch = connect()
    version = rabbitmq_version()

    log(f"RabbitMQ {version}  |  {EXCHANGE_TYPE} exchange  |  image {IMAGE_DIGEST}")
    log("")

    a = experiment_a(conn, ch)
    bc = experiment_b_c(conn, ch)

    conn.close()

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "rabbitmq_version", "image_digest", "amqp_port", "mgmt_port",
            "a_messages", "a_distinct_keys", "a_queues", "a_ideal_per_queue",
            "a_max_deviation_pct",
            "b_keys", "b_copies_per_key",
            "b_consistent_hash_remapped", "b_consistent_hash_remapped_pct",
            "b_modulo_remapped", "b_modulo_remapped_pct",
            "c_affinity_holds", "c_keys_checked",
        ])
        w.writerow([
            version, IMAGE_DIGEST, PORT, MGMT_PORT,
            a["messages"], a["distinct_keys"], a["queues"], f"{a['ideal']:.1f}",
            f"{a['max_dev_pct']:.2f}",
            bc["keys"], bc["copies"],
            bc["ch_remapped"], f"{bc['ch_pct']:.2f}",
            bc["mod_remapped"], f"{bc['mod_pct']:.2f}",
            "yes" if bc["affinity"] else "no", bc["keys_checked"],
        ])

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(_LINES) + "\n")

    log(f"\n  RabbitMQ {version} | artifacts in {RESULTS}/")


if __name__ == "__main__":
    main()
