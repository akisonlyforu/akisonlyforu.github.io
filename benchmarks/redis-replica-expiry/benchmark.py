"""Measure Redis replica expiration lag: replicas do not expire keys on their own
clock, they wait for the primary to propagate a DEL. So a replica physically holds
logically-expired keys, and any count/keyspace observation off the replica (DBSIZE,
SCAN) is reasoning about ghosts, even though value reads are masked.

Three experiments, all against a real primary + read-replica:
  A. Ghost keyspace   - replica DBSIZE vs how many keys still read back.
  B. Command behavior - what each command returns for one expired key, primary vs replica.
  C. DBSIZE drift      - primary vs replica keyspace size over time as a batch expires.

Env: PRIMARY_PORT (6391), REPLICA_PORT (6392), host 127.0.0.1.
"""
import csv
import os
import time

import redis

HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
PPORT = int(os.environ.get("PRIMARY_PORT", "6391"))
RPORT = int(os.environ.get("REPLICA_PORT", "6392"))
RESULTS = os.path.join(os.path.dirname(__file__), "results")


def c(port):
    return redis.Redis(host=HOST, port=port, decode_responses=True)


def wait_link(primary, replica, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        info = replica.info("replication")
        if info.get("role") == "slave" and info.get("master_link_status") == "up":
            return
        time.sleep(0.1)
    raise RuntimeError("replica did not link to primary")


def wait_replicated(primary, replica, expect, timeout=10):
    """Block until the replica has applied the primary's writes."""
    primary.wait(1, 2000)
    end = time.time() + timeout
    while time.time() < end:
        if replica.dbsize() == expect:
            return
        time.sleep(0.05)


def active_expire(primary, on):
    primary.execute_command("DEBUG", "SET-ACTIVE-EXPIRE", "1" if on else "0")


def experiment_a(p, r):
    p.flushall()
    active_expire(p, False)            # deterministic: nothing sweeps behind our back
    n = 1000
    pipe = p.pipeline()
    for i in range(n):
        pipe.set(f"job:{i}", "owner", ex=2)
    pipe.execute()
    wait_replicated(p, r, n)
    time.sleep(3)                      # every key is now logically expired

    p_db, r_db = p.dbsize(), r.dbsize()
    reads_alive = sum(1 for i in range(n) if r.get(f"job:{i}") is not None)
    print("=" * 62)
    print("EXPERIMENT A  ghost keyspace on the replica")
    print("=" * 62)
    print(f"  set {n} keys with EX 2 on primary, waited 3s (all expired)")
    print(f"  primary DBSIZE : {p_db}")
    print(f"  replica DBSIZE : {r_db}")
    print(f"  of those {r_db} replica keys, how many return a value on GET: {reads_alive}")
    print(f"  => the replica reports {r_db} keys; {r_db - reads_alive} are ghosts (GET masks them)")
    return {"keys": n, "primary_dbsize": p_db, "replica_dbsize": r_db, "replica_reads_alive": reads_alive}


def experiment_b(p, r):
    """One expired key, queried ONLY on the replica. Replica reads don't delete,
    so nothing is contaminated: DBSIZE keeps counting the ghost while every read
    command masks it. (A read on the PRIMARY would lazily delete it, measured
    separately below.)"""
    p.flushall()
    active_expire(p, False)
    p.set("job:solo", "owner", ex=2)
    wait_replicated(p, r, 1)
    time.sleep(3)                      # expired, and we never touch it on the primary

    rows = [
        ("DBSIZE (counts it?)", r.dbsize()),
        ("GET job:solo", r.get("job:solo")),
        ("EXISTS job:solo", r.exists("job:solo")),
        ("TTL job:solo", r.ttl("job:solo")),
        ("SCAN finds job:solo?", "job:solo" in r.scan(match="job:solo")[1]),
        ("DBSIZE again (reads deleted it?)", r.dbsize()),
    ]
    print("\n" + "=" * 62)
    print("EXPERIMENT B  one expired key, all queried on the REPLICA")
    print("=" * 62)
    for name, val in rows:
        print(f"  {name:34} {str(val):>10}")

    # contrast: a single read on the primary lazily expires + deletes it
    p_before = p.dbsize()
    p_get = p.get("job:solo")
    p.wait(1, 1000)
    p_after = p.dbsize()
    print(f"  --- contrast on the PRIMARY ---")
    print(f"  DBSIZE {p_before} -> GET returns {p_get} (lazy-deletes) -> DBSIZE {p_after}")
    return [{"command": n, "replica": str(v)} for n, v in rows] + \
           [{"command": "PRIMARY GET lazy-delete", "replica": f"{p_before}->{p_after}"}]


def experiment_c(p, r):
    p.flushall()
    active_expire(p, True)             # realistic: primary's active cycle runs
    wait_replicated(p, r, 0)
    m = 5000
    pipe = p.pipeline()
    for i in range(m):
        pipe.set(f"job:{i}", "owner", ex=2)
    pipe.execute()
    wait_replicated(p, r, m)

    print("\n" + "=" * 62)
    print("EXPERIMENT C  DBSIZE drift as a batch expires (active-expire ON)")
    print("=" * 62)
    timeline = []
    t0 = time.time()
    peak = 0
    while time.time() - t0 < 10:
        t_ms = (time.time() - t0) * 1000
        pd, rd = p.dbsize(), r.dbsize()
        timeline.append((round(t_ms), pd, rd))
        peak = max(peak, rd - pd)
        time.sleep(0.2)
    with open(os.path.join(RESULTS, "dbsize_timeline.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "primary_dbsize", "replica_dbsize"])
        w.writerows(timeline)
    print(f"  set {m} keys EX 2, sampled DBSIZE on both every 200ms for 10s")
    print(f"  peak replica-minus-primary keyspace gap: {peak} keys")
    print(f"  wrote {len(timeline)} samples to results/dbsize_timeline.csv")
    return {"keys": m, "peak_gap": peak, "samples": len(timeline)}


def main():
    os.makedirs(RESULTS, exist_ok=True)
    p, r = c(PPORT), c(RPORT)
    p.ping(); r.ping()
    wait_link(p, r)
    ver = p.info("server")["redis_version"]

    a = experiment_a(p, r)
    b = experiment_b(p, r)
    cc = experiment_c(p, r)

    with open(os.path.join(RESULTS, "command_behavior.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["command", "primary", "replica"])
        w.writeheader()
        w.writerows(b)
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["redis_version", "primary_port", "replica_port",
                    "a_replica_dbsize", "a_replica_reads_alive", "c_peak_gap"])
        w.writerow([ver, PPORT, RPORT, a["replica_dbsize"], a["replica_reads_alive"], cc["peak_gap"]])

    active_expire(p, True)
    print(f"\n  redis {ver} | artifacts in results/")


if __name__ == "__main__":
    main()
