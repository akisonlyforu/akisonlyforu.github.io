"""Does the classic RabbitMQ "confirmed messages can still be lost on failover"
bug still bite in 2026? This harness re-tests it against real brokers.

Three experiments, three real clusters' worth of measurement:

  A. THE PROBLEM  (RabbitMQ 3.13, classic MIRRORED queue)
     Publish M messages with publisher confirms, arrange for the mirrors to be
     UNSYNCHRONISED (ha-sync-mode=manual), then crash the master. The default
     ha-promote-on-failure=always promotes an unsynchronised mirror, which does
     NOT have the confirmed messages -> confirmed-but-lost.

  B. THE FIX      (RabbitMQ 4.0, QUORUM queue)
     Same shape: publish M with confirms, crash the queue LEADER, let Raft elect
     a new leader, drain. A quorum-queue confirm means a majority already has the
     message, so losing one node of three loses nothing -> expect 0 lost.

  C. THE CATCH    (RabbitMQ 4.0, QUORUM queue availability)
     Measure publisher-confirm success as nodes are removed: 3 up, 2 up (still a
     majority), 1 up (majority lost). Quorum queues choose consistency over
     availability, so at 1/3 the confirms block/fail. That's the honest trade.

The harness drives failures with `docker kill` / `docker start` (a crash, not a
graceful shutdown) and inspects the cluster over the management HTTP API and
`rabbitmqctl`. Which experiments run depends on which cluster is up, chosen by the
mode argument:  `python benchmark.py mirrored`  or  `python benchmark.py quorum`.

Env: RABBITMQ_HOST (127.0.0.1); per-node AMQP ports RMQ0_PORT/RMQ1_PORT/RMQ2_PORT
(5772/5773/5774); management ports RMQ0_MGMT/RMQ1_MGMT/RMQ2_MGMT (15772/15773/
15774); MESSAGES (5000); RESULTS_DIR (./results).
"""
import base64
import csv
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pika

HOST = os.environ.get("RABBITMQ_HOST", "127.0.0.1")
USER = os.environ.get("RABBITMQ_USER", "guest")
PASS = os.environ.get("RABBITMQ_PASS", "guest")
MESSAGES = int(os.environ.get("MESSAGES", "5000"))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

# container name -> (amqp host port, management host port). Container names are
# pinned in the compose files so we can `docker kill` a node by name.
NODES = {
    "rmq0": (int(os.environ.get("RMQ0_PORT", "5772")), int(os.environ.get("RMQ0_MGMT", "15772"))),
    "rmq1": (int(os.environ.get("RMQ1_PORT", "5773")), int(os.environ.get("RMQ1_MGMT", "15773"))),
    "rmq2": (int(os.environ.get("RMQ2_PORT", "5774")), int(os.environ.get("RMQ2_MGMT", "15774"))),
}
NODE_NAME = {c: f"rabbit@{c}" for c in NODES}  # container -> erlang node name

IMAGE_DIGEST = {
    "mirrored": "sha256:e582c0bc7766f3342496d8485efb5a1df782b5ce3886ad017e2eaae442311f69",  # rabbitmq:3.13-management
    "quorum": "sha256:ad4268113c27d02f08ac1151f9651d6e475c955f81c3a5ad522b79955ce11cf3",    # rabbitmq:4.0-management
}

_LINES = []


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


# ---------------------------------------------------------------------------
# docker / broker helpers
# ---------------------------------------------------------------------------
def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check,
                          capture_output=True, text=True)


def rabbitmqctl(container, *args):
    r = docker("exec", container, "rabbitmqctl", *args, check=False)
    return r.stdout + r.stderr


def connect(container):
    amqp_port, _ = NODES[container]
    params = pika.ConnectionParameters(
        host=HOST, port=amqp_port,
        credentials=pika.PlainCredentials(USER, PASS),
        heartbeat=600, blocked_connection_timeout=30,
        socket_timeout=10, connection_attempts=1,
    )
    conn = pika.BlockingConnection(params)
    return conn, conn.channel()


def mgmt(container, path, method="GET", body=None, timeout=10):
    _, mgmt_port = NODES[container]
    url = f"http://{HOST}:{mgmt_port}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def rabbitmq_version(container):
    return mgmt(container, "/overview").get("rabbitmq_version", "unknown")


def wait_amqp(container, timeout=90):
    """Poll until a node accepts AMQP connections."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            conn, _ = connect(container)
            conn.close()
            return True
        except Exception:
            time.sleep(1)
    return False


def wait_all_running(via, expect_containers, timeout=90):
    """Poll `rabbitmqctl cluster_status` on `via` until all expected nodes run."""
    want = {NODE_NAME[c] for c in expect_containers}
    end = time.time() + timeout
    while time.time() < end:
        out = rabbitmqctl(via, "cluster_status")
        if all(n in out for n in want):
            # crude but effective: every wanted node name appears in Running Nodes
            running = out.split("Running Nodes", 1)[-1]
            if all(n in running for n in want):
                return True
        time.sleep(2)
    return False


def restore_cluster(all_up_wait=True):
    """docker start every node, wait until all three run and cluster is whole."""
    for c in NODES:
        docker("start", c, check=False)
    for c in NODES:
        wait_amqp(c)
    if all_up_wait:
        # find any running container to query from
        for c in NODES:
            if wait_all_running(c, list(NODES)):
                return c
    return next(iter(NODES))


def queue_info(via, qname, timeout=10):
    return mgmt(via, f"/queues/%2F/{qname}", timeout=timeout)


def queue_depth_ctl(via_container, qname):
    """Authoritative, immediate message count via rabbitmqctl (the management stats
    DB lags a few seconds, which would put bogus zeros in the timeline)."""
    out = rabbitmqctl(via_container, "list_queues", "name", "messages")
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].strip() == qname:
            try:
                return int(parts[1].strip())
            except ValueError:
                return -1
    return -1


# ---------------------------------------------------------------------------
# publisher confirms
# ---------------------------------------------------------------------------
def publish_confirmed(ch, exchange, rkey, n, body_prefix="m"):
    """Publish n durable messages one at a time in confirm mode. Returns the count
    the broker positively confirmed (a nack or a raised exception is NOT counted)."""
    confirmed = 0
    for i in range(n):
        try:
            ch.basic_publish(
                exchange=exchange, routing_key=rkey,
                body=f"{body_prefix}-{i}".encode(),
                properties=pika.BasicProperties(delivery_mode=2),  # persistent
                mandatory=False,
            )
            confirmed += 1  # basic_publish returns normally only on a broker ack
        except (pika.exceptions.NackError, pika.exceptions.UnroutableError,
                pika.exceptions.AMQPError):
            pass
    return confirmed


def drain_count(ch, qname, timeout=20):
    """Count every message on a queue by consuming with acks."""
    got = 0
    for method, _props, _body in ch.consume(qname, auto_ack=True,
                                             inactivity_timeout=2.0):
        if method is None:
            break
        got += 1
    ch.cancel()
    return got


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


# ---------------------------------------------------------------------------
# EXPERIMENT A -- mirrored queue confirmed-message loss (RabbitMQ 3.13)
# ---------------------------------------------------------------------------
def experiment_a():
    qname = "ha.q"
    timeline = []

    def mark(via, event):
        d = queue_depth_ctl(via, qname)
        timeline.append((event, d))
        return d

    log("=" * 70)
    log("EXPERIMENT A  classic MIRRORED queue, confirmed-message loss (RabbitMQ 3.13)")
    log("=" * 70)

    via = restore_cluster()
    version = rabbitmq_version(via)
    log(f"  cluster whole, RabbitMQ {version}, connected via {via}")

    # HA policy: mirror to all nodes, MANUAL sync (mirrors never auto-catch-up),
    # and promote an unsynchronised mirror on both crash and shutdown. The manual
    # sync + always-promote pair is the real-world footgun this experiment isolates.
    policy = {
        "pattern": "^ha\\.",
        "apply-to": "queues",
        "definition": {
            "ha-mode": "all",
            "ha-sync-mode": "manual",
            "ha-promote-on-failure": "always",
            "ha-promote-on-shutdown": "always",
        },
        "priority": 0,
    }
    mgmt("rmq0", "/policies/%2F/ha-all", method="PUT", body=policy)
    log("  policy ha-all set: ha-mode=all, ha-sync-mode=manual, "
        "ha-promote-on-failure=always, ha-promote-on-shutdown=always")

    # Declare the durable classic queue on rmq0 -> master lives on rmq0.
    conn, ch = connect("rmq0")
    ch.queue_delete(queue=qname)
    ch.queue_declare(queue=qname, durable=True,
                     arguments={"x-queue-type": "classic"})
    conn.close()
    time.sleep(2)
    mark("rmq0", "declared")
    log(f"  declared durable classic queue '{qname}' on rmq0 (master=rabbit@rmq0)")

    # Take the two mirrors DOWN, so messages we publish next land only on the master.
    log("  docker kill rmq1 rmq2  (mirrors go down)")
    docker("kill", "rmq1"); docker("kill", "rmq2")
    time.sleep(6)

    # Publish M with publisher confirms to the master.
    conn, ch = connect("rmq0")
    ch.confirm_delivery()
    log(f"  publishing {MESSAGES} persistent messages with publisher confirms...")
    confirmed = publish_confirmed(ch, "", qname, MESSAGES)
    conn.close()
    d = mark("rmq0", "publish_done")
    log(f"  broker positively confirmed: {confirmed} / {MESSAGES}   (master depth now {d})")

    # Bring the mirrors back. With manual sync they rejoin UNSYNCHRONISED: they hold
    # none of the confirmed messages and will not catch up on their own.
    log("  docker start rmq1 rmq2  (mirrors rejoin, but UNSYNCHRONISED under manual sync)")
    docker("start", "rmq1"); docker("start", "rmq2")
    wait_amqp("rmq1"); wait_amqp("rmq2")
    wait_all_running("rmq0", list(NODES))
    time.sleep(6)

    synced = rabbitmqctl("rmq0", "list_queues", "name", "messages",
                         "slave_pids", "synchronised_slave_pids")
    log("  rabbitmqctl list_queues name messages slave_pids synchronised_slave_pids:")
    for line in synced.splitlines():
        if qname in line or "name" == line.strip().split("\t")[0:1][:1]:
            log(f"      {line.strip()}")
    mark("rmq0", "mirrors_rejoined_unsynced")

    # Crash the master. ha-promote-on-failure=always promotes an UNSYNCHRONISED
    # mirror, which does not have the confirmed messages.
    log("  docker kill rmq0  (crash the master; an UNSYNCHRONISED mirror is promoted)")
    docker("kill", "rmq0")
    time.sleep(2)

    survivor = "rmq1"
    wait_amqp(survivor)
    # wait for the surviving cluster to re-elect / expose the queue with a new master
    end = time.time() + 60
    new_master = None
    while time.time() < end:
        try:
            info = queue_info(survivor, qname)
            node = info.get("node")
            if node and node != NODE_NAME["rmq0"]:
                new_master = node
                break
        except Exception:
            pass
        time.sleep(2)
    mark(survivor, "master_killed_promoted")
    log(f"  promoted new master: {new_master}")

    # Drain whatever survived on the promoted queue.
    conn, ch = connect(survivor)
    recovered = drain_count(ch, qname)
    conn.close()
    timeline.append(("drained", recovered))
    lost = confirmed - recovered
    lost_pct = 100.0 * lost / confirmed if confirmed else 0.0

    log("")
    log(f"  >>> confirmed = {confirmed}   recovered = {recovered}   "
        f"LOST = {lost}  ({lost_pct:.1f}% of confirmed messages)")
    log("")

    # clean up policy + queue on survivor
    try:
        mgmt(survivor, "/policies/%2F/ha-all", method="DELETE")
        conn, ch = connect(survivor)
        ch.queue_delete(queue=qname)
        conn.close()
    except Exception:
        pass

    _write_timeline("loss_timeline_mirrored.csv", timeline)
    result = {
        "model": "mirrored", "rabbitmq_version": version,
        "image_digest": IMAGE_DIGEST["mirrored"],
        "messages_confirmed": confirmed, "messages_recovered": recovered,
        "messages_lost": lost, "lost_pct": round(lost_pct, 2),
    }
    _save_json("_mirrored.json", {"loss": result, "summary": "\n".join(_LINES)})
    return result


# ---------------------------------------------------------------------------
# EXPERIMENT B -- quorum queue survives leader loss (RabbitMQ 4.0)
# ---------------------------------------------------------------------------
def experiment_b():
    qname = "qq"
    timeline = []

    def depth(via):
        return queue_depth_ctl(via, qname)

    def settled_depth(via, expect, timeout=20):
        """Poll the authoritative count until it reaches `expect` (quorum commits
        apply asynchronously, so an immediate read can understate the true depth)."""
        end = time.time() + timeout
        d = depth(via)
        while d < expect and time.time() < end:
            time.sleep(0.5)
            d = depth(via)
        return d

    log("=" * 70)
    log("EXPERIMENT B  QUORUM queue survives leader loss (RabbitMQ 4.0)")
    log("=" * 70)

    via = restore_cluster()
    version = rabbitmq_version(via)
    log(f"  cluster whole, RabbitMQ {version}, connected via {via}")

    conn, ch = connect("rmq0")
    ch.queue_delete(queue=qname)
    ch.queue_declare(queue=qname, durable=True,
                     arguments={"x-queue-type": "quorum"})
    ch.confirm_delivery()
    log(f"  declared quorum queue '{qname}' (x-queue-type=quorum)")
    time.sleep(3)

    info = queue_info("rmq0", qname)
    members = info.get("members") or info.get("online") or []
    log(f"  quorum members: {len(members)} -> {members}")
    timeline.append(("declared", depth("rmq0")))

    log(f"  publishing {MESSAGES} persistent messages with publisher confirms...")
    confirmed = publish_confirmed(ch, "", qname, MESSAGES)
    conn.close()
    d = settled_depth("rmq0", confirmed)
    timeline.append(("publish_done", d))
    log(f"  broker positively confirmed: {confirmed} / {MESSAGES}   (queue depth {d})")

    # Find and crash the LEADER.
    info = queue_info("rmq0", qname)
    leader = info.get("leader")
    leader_container = next(c for c in NODES if NODE_NAME[c] == leader)
    log(f"  current leader: {leader}  ->  docker kill {leader_container}")
    timeline.append(("leader_killed", d))
    docker("kill", leader_container)
    time.sleep(2)

    survivor = next(c for c in NODES if c != leader_container)
    wait_amqp(survivor)
    # wait for a new leader to be elected
    end = time.time() + 60
    new_leader = None
    while time.time() < end:
        try:
            info = queue_info(survivor, qname)
            nl = info.get("leader")
            if nl and nl != leader:
                new_leader = nl
                break
        except Exception:
            pass
        time.sleep(2)
    log(f"  new leader elected: {new_leader}")
    timeline.append(("new_leader", settled_depth(survivor, confirmed)))

    conn, ch = connect(survivor)
    recovered = drain_count(ch, qname)
    ch.queue_delete(queue=qname)
    conn.close()
    timeline.append(("drained", recovered))
    lost = confirmed - recovered
    lost_pct = 100.0 * lost / confirmed if confirmed else 0.0

    log("")
    log(f"  >>> confirmed = {confirmed}   recovered = {recovered}   "
        f"LOST = {lost}  ({lost_pct:.1f}% of confirmed messages)")
    log("")

    _write_timeline("loss_timeline_quorum.csv", timeline)
    return {
        "model": "quorum", "rabbitmq_version": version,
        "image_digest": IMAGE_DIGEST["quorum"],
        "messages_confirmed": confirmed, "messages_recovered": recovered,
        "messages_lost": lost, "lost_pct": round(lost_pct, 2),
        "members": len(members),
    }


# ---------------------------------------------------------------------------
# EXPERIMENT C -- quorum queue availability vs surviving-node count (RabbitMQ 4.0)
# ---------------------------------------------------------------------------
def experiment_c():
    qname = "qq.avail"
    attempts_per_stage = int(os.environ.get("AVAIL_ATTEMPTS", "50"))
    per_publish_timeout = int(os.environ.get("AVAIL_TIMEOUT", "6"))
    rows = []

    log("=" * 70)
    log("EXPERIMENT C  QUORUM queue availability vs surviving-node count (RabbitMQ 4.0)")
    log("=" * 70)

    via = restore_cluster()
    version = rabbitmq_version(via)

    conn, ch = connect("rmq0")
    ch.queue_delete(queue=qname)
    ch.queue_declare(queue=qname, durable=True,
                     arguments={"x-queue-type": "quorum"})
    conn.close()
    time.sleep(3)
    info = queue_info("rmq0", qname)
    log(f"  quorum queue '{qname}' declared, members={len(info.get('members') or [])}")

    def try_confirms(container, n):
        """Attempt n confirmed publishes on `container`, each guarded by an
        alarm timeout. Returns how many the broker positively confirmed. On the
        first timeout/error we stop (the group has no majority; the rest would
        block too) and count the remainder as failures."""
        succeeded = 0
        try:
            conn, ch = connect(container)
            ch.confirm_delivery()
        except Exception as e:
            log(f"    connect to {container} failed: {e!r}")
            return 0
        old = signal.signal(signal.SIGALRM, _alarm)
        try:
            for i in range(n):
                signal.setitimer(signal.ITIMER_REAL, per_publish_timeout)
                try:
                    ch.basic_publish(
                        exchange="", routing_key=qname,
                        body=f"avail-{i}".encode(),
                        properties=pika.BasicProperties(delivery_mode=2),
                    )
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    succeeded += 1
                except (_Timeout, pika.exceptions.AMQPError, Exception):
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    break  # no majority -> remaining attempts would block too
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            try:
                conn.close()
            except Exception:
                pass
        return succeeded

    # Stage 1: all 3 up (majority present)
    n_up, majority = 3, True
    ok = try_confirms("rmq0", attempts_per_stage)
    rows.append((n_up, majority, attempts_per_stage, ok))
    log(f"  3 nodes up (majority)  : {ok}/{attempts_per_stage} confirmed")

    # Stage 2: kill one -> 2 up (still a majority of 3)
    docker("kill", "rmq2")
    time.sleep(6)
    n_up, majority = 2, True
    ok = try_confirms("rmq0", attempts_per_stage)
    rows.append((n_up, majority, attempts_per_stage, ok))
    log(f"  2 nodes up (majority)  : {ok}/{attempts_per_stage} confirmed")

    # Stage 3: kill another -> 1 up (minority, no quorum)
    docker("kill", "rmq1")
    time.sleep(6)
    n_up, majority = 1, False
    ok = try_confirms("rmq0", attempts_per_stage)
    rows.append((n_up, majority, attempts_per_stage, ok))
    log(f"  1 node up  (MINORITY)  : {ok}/{attempts_per_stage} confirmed  "
        f"(expected 0: no quorum -> consistency over availability)")
    log("")

    # write availability.csv
    with open(os.path.join(RESULTS, "availability.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nodes_up", "majority", "confirms_attempted",
                    "confirms_succeeded", "confirm_success_pct"])
        for n_up, maj, att, ok in rows:
            pct = 100.0 * ok / att if att else 0.0
            w.writerow([n_up, str(bool(maj)).lower(), att, ok, f"{pct:.1f}"])

    return {"version": version, "rows": rows,
            "attempts": attempts_per_stage, "timeout": per_publish_timeout}


# ---------------------------------------------------------------------------
# artifact writers
# ---------------------------------------------------------------------------
def _write_timeline(fname, timeline):
    with open(os.path.join(RESULTS, fname), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "queue_message_count"])
        for event, count in timeline:
            w.writerow([event, count])


def _save_json(fname, obj):
    with open(os.path.join(RESULTS, fname), "w") as f:
        json.dump(obj, f, indent=2)


def _load_json(fname):
    p = os.path.join(RESULTS, fname)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def rebuild_combined():
    """Rebuild loss.csv, run_metadata.csv and summary.txt from whichever per-mode
    JSON blobs exist, so the combined artifacts are correct after either run."""
    mir = _load_json("_mirrored.json")
    quo = _load_json("_quorum.json")

    if mir and quo:
        with open(os.path.join(RESULTS, "loss.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["model", "rabbitmq_version", "messages_confirmed",
                        "messages_recovered", "messages_lost", "lost_pct"])
            for blob in (mir, quo):
                r = blob["loss"]
                w.writerow([r["model"], r["rabbitmq_version"],
                            r["messages_confirmed"], r["messages_recovered"],
                            r["messages_lost"], r["lost_pct"]])

    # run_metadata.csv (single row summarising both models + experiment C)
    if mir and quo:
        avail = quo.get("availability", {})
        with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "messages_per_run",
                "mirrored_rabbitmq_version", "mirrored_image_digest",
                "mirrored_confirmed", "mirrored_recovered", "mirrored_lost", "mirrored_lost_pct",
                "quorum_rabbitmq_version", "quorum_image_digest",
                "quorum_confirmed", "quorum_recovered", "quorum_lost", "quorum_lost_pct",
                "availability_3up_pct", "availability_2up_pct", "availability_1up_pct",
            ])
            m, q = mir["loss"], quo["loss"]
            w.writerow([
                MESSAGES,
                m["rabbitmq_version"], m["image_digest"],
                m["messages_confirmed"], m["messages_recovered"], m["messages_lost"], m["lost_pct"],
                q["rabbitmq_version"], q["image_digest"],
                q["messages_confirmed"], q["messages_recovered"], q["messages_lost"], q["lost_pct"],
                avail.get("3", ""), avail.get("2", ""), avail.get("1", ""),
            ])

    # summary.txt = concatenation of both captured consoles
    parts = []
    if mir:
        parts.append(mir.get("summary", ""))
    if quo:
        parts.append(quo.get("summary", ""))
    if parts:
        with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
            f.write("\n\n".join(parts) + "\n")


# ---------------------------------------------------------------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mirrored"
    os.makedirs(RESULTS, exist_ok=True)

    if mode == "mirrored":
        experiment_a()
    elif mode == "quorum":
        b = experiment_b()
        c = experiment_c()
        avail_pct = {str(n): (100.0 * ok / att if att else 0.0)
                     for n, _maj, att, ok in c["rows"]}
        _save_json("_quorum.json", {
            "loss": b,
            "availability": {k: round(v, 1) for k, v in avail_pct.items()},
            "availability_meta": {"attempts": c["attempts"], "timeout": c["timeout"]},
            "summary": "\n".join(_LINES),
        })
    else:
        print(f"unknown mode {mode!r}; use 'mirrored' or 'quorum'")
        sys.exit(2)

    rebuild_combined()
    log(f"  artifacts in {RESULTS}/")


if __name__ == "__main__":
    main()
