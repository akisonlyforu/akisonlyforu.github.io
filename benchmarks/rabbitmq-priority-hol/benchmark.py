"""Measure head-of-line blocking of a high-priority message behind a bulk backlog
on a real RabbitMQ broker.

The story: a notification service pushes everything through ONE queue - bulk
marketing blasts and critical OTP/transactional messages. A big campaign is
enqueued, then an OTP arrives. How long until a single consumer delivers the OTP?

Three experiments, same load (a backlog of BULK messages already sitting in the
queue, then OTP messages injected while the consumer is already draining), one
consumer with a fixed per-message service time so the drain rate is deterministic:

  A. Single FIFO queue, no priority (prefetch high).
     The OTP sits at the tail behind the whole backlog. Latency ~= backlog/rate,
     i.e. seconds.
  B. Priority queue (x-max-priority=10), bulk priority 0, OTP priority 9, but a
     LARGE prefetch (the fix that surprisingly doesn't work).
     The broker only reorders messages still in the READY state. With a big
     prefetch the consumer has already buffered thousands of bulk messages
     locally (unacked, FIFO in the client), so the newly-arrived OTP is appended
     to the tail of that local buffer. Latency ~= prefetch/rate - still seconds.
  C. Same priority queue, prefetch=1 (the real fix).
     The broker re-evaluates priority before every single delivery, so the OTP
     jumps the entire backlog. Latency drops to milliseconds.

Only the priority setting (A->B) and the prefetch (B->C) change. Everything runs
against the real broker via pika; nothing is simulated. The per-message service
time is a time.sleep, which is the only artificial knob and it is identical
across all three runs.

Env: AMQP_HOST (127.0.0.1), AMQP_PORT (6672 AMQP), MGMT_PORT (16672 management),
RESULTS_DIR (./results).
"""
import base64
import csv
import json
import os
import statistics
import threading
import time
import urllib.request

import pika

HOST = os.environ.get("AMQP_HOST", "127.0.0.1")
PORT = int(os.environ.get("AMQP_PORT", "6672"))
MGMT_PORT = int(os.environ.get("MGMT_PORT", "16672"))
USER = os.environ.get("AMQP_USER", "guest")
PASS = os.environ.get("AMQP_PASS", "guest")
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))

# Digest-pinned image this harness is written against (see docker-compose.yml).
IMAGE_DIGEST = "sha256:ad4268113c27d02f08ac1151f9651d6e475c955f81c3a5ad522b79955ce11cf3"

# ---- load / timing parameters (identical across A/B/C) ----------------------
BACKLOG = 20_000          # bulk messages enqueued before the consumer starts
OTP_COUNT = 200           # high-priority messages injected mid-drain
SERVICE_TIME_MS = 0.5     # per-message consumer service time -> ~2000 msg/s ceiling
WARMUP = 500              # inject the OTPs once the consumer has drained this many
OTP_GAP_MS = 4.0          # spacing between injected OTPs (real OTPs arrive spread
                          # out, not in one microsecond burst); identical for A/B/C
MAX_PRIORITY = 10         # x-max-priority on the priority queue
BULK_PRIORITY = 0
OTP_PRIORITY = 9

SERVICE_TIME = SERVICE_TIME_MS / 1000.0

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


def declare_fresh(ch, queue, use_priority):
    ch.queue_delete(queue=queue)
    args = {"x-max-priority": MAX_PRIORITY} if use_priority else {}
    ch.queue_declare(queue=queue, durable=True, arguments=args)


def qdepth(ch, queue):
    return ch.queue_declare(queue=queue, passive=True).method.message_count


def pct(sorted_vals, p):
    """Nearest-rank percentile on an already-sorted list (values in ms)."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


# ---------------------------------------------------------------------------
# consumer thread
# ---------------------------------------------------------------------------
class Consumer(threading.Thread):
    """Drains `queue` at a fixed service time, timestamping every OTP delivery."""

    def __init__(self, queue, prefetch, total_expected, warmup_event):
        super().__init__(daemon=True)
        self.queue = queue
        self.prefetch = prefetch
        self.total_expected = total_expected
        self.warmup_event = warmup_event
        self.otp_latencies_ms = []   # recv_time - publish_time, milliseconds
        self.consumed = 0
        self.bulk_count = 0
        self.first_recv = None
        self.last_bulk_recv = None
        self._conn = None

    def run(self):
        self._conn, ch = connect()
        ch.basic_qos(prefetch_count=self.prefetch)

        def on_msg(chan, method, _props, body):
            now = time.time()
            if self.first_recv is None:
                self.first_recv = now
            mtype, ts = body.split(b"|", 1)
            if mtype == b"otp":
                self.otp_latencies_ms.append((now - float(ts)) * 1000.0)
            else:
                self.bulk_count += 1
                self.last_bulk_recv = now
            self.consumed += 1
            # fixed per-message service time -> deterministic drain rate
            time.sleep(SERVICE_TIME)
            chan.basic_ack(method.delivery_tag)
            if self.consumed == WARMUP:
                self.warmup_event.set()
            if self.consumed >= self.total_expected:
                chan.stop_consuming()

        ch.basic_consume(queue=self.queue, on_message_callback=on_msg, auto_ack=False)
        ch.start_consuming()
        self.warmup_event.set()  # safety: unblock publisher if we exit early
        self._conn.close()


# ---------------------------------------------------------------------------
# one experiment
# ---------------------------------------------------------------------------
def run_experiment(letter, subtitle, queue, use_priority, prefetch):
    total = BACKLOG + OTP_COUNT

    pub_conn, pub = connect()
    declare_fresh(pub, queue, use_priority)

    # 1) enqueue the whole bulk backlog first
    bulk_props = pika.BasicProperties(
        delivery_mode=2,
        priority=(BULK_PRIORITY if use_priority else None),
    )
    for _ in range(BACKLOG):
        pub.basic_publish("", queue, f"bulk|{time.time()!r}".encode(), properties=bulk_props)
    # make sure the backlog is fully enqueued before the consumer starts
    while qdepth(pub, queue) < BACKLOG:
        time.sleep(0.05)

    # 2) start the consumer draining the backlog
    warmup_event = threading.Event()
    consumer = Consumer(queue, prefetch, total, warmup_event)
    consumer.start()

    # 3) once the consumer is warmed up (backlog already draining / buffered),
    #    inject the OTPs - this is the "later-arriving" high-priority traffic
    warmup_event.wait(timeout=60)
    otp_props = pika.BasicProperties(
        delivery_mode=2,
        priority=(OTP_PRIORITY if use_priority else None),
    )
    for i in range(OTP_COUNT):
        pub.basic_publish("", queue, f"otp|{time.time()!r}".encode(), properties=otp_props)
        if i < OTP_COUNT - 1:
            time.sleep(OTP_GAP_MS / 1000.0)
    pub_conn.close()

    # 4) wait for the drain to finish
    consumer.join(timeout=300)

    lat = sorted(consumer.otp_latencies_ms)
    p50 = pct(lat, 50)
    p99 = pct(lat, 99)
    mx = lat[-1] if lat else float("nan")
    mn = lat[0] if lat else float("nan")
    mean = statistics.fmean(lat) if lat else float("nan")
    if consumer.first_recv and consumer.last_bulk_recv and consumer.bulk_count:
        span = consumer.last_bulk_recv - consumer.first_recv
        drain = consumer.bulk_count / span if span > 0 else float("nan")
    else:
        drain = float("nan")

    log("=" * 70)
    log(f"EXPERIMENT {letter}  {subtitle}")
    log("=" * 70)
    log(f"  priority queue : {'yes (x-max-priority=%d)' % MAX_PRIORITY if use_priority else 'no (plain FIFO)'}")
    log(f"  prefetch_count : {prefetch}")
    log(f"  backlog        : {BACKLOG} bulk (prio {BULK_PRIORITY if use_priority else '-'}), "
        f"then {OTP_COUNT} OTP (prio {OTP_PRIORITY if use_priority else '-'}) injected after {WARMUP} drained")
    log(f"  service time   : {SERVICE_TIME_MS} ms/msg")
    log(f"  OTPs measured  : {len(lat)}")
    log(f"  bulk drain rate: {drain:8.1f} msg/s")
    log(f"  OTP latency    : p50 {p50:9.1f} ms   p99 {p99:9.1f} ms   max {mx:9.1f} ms   "
        f"(min {mn:.1f}, mean {mean:.1f})")
    log("")

    with open(os.path.join(RESULTS, f"otp_latencies_{letter}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["otp_index", "latency_ms"])
        for i, v in enumerate(consumer.otp_latencies_ms):
            w.writerow([i, f"{v:.3f}"])

    # teardown queue
    _c, ch = connect()
    ch.queue_delete(queue=queue)
    _c.close()

    return {
        "letter": letter, "use_priority": use_priority, "prefetch": prefetch,
        "n": len(lat), "p50": p50, "p99": p99, "max": mx, "min": mn, "mean": mean,
        "drain": drain,
    }


# ---------------------------------------------------------------------------
def main():
    os.makedirs(RESULTS, exist_ok=True)
    version = rabbitmq_version()

    log(f"RabbitMQ {version}  |  priority / head-of-line-blocking demo  |  image {IMAGE_DIGEST}")
    log(f"backlog={BACKLOG}  otp={OTP_COUNT}  service_time={SERVICE_TIME_MS}ms  "
        f"warmup={WARMUP}  otp_gap={OTP_GAP_MS}ms")
    log("")

    a = run_experiment("A", "single FIFO queue (the problem)",
                       "prio.hol.a", use_priority=False, prefetch=5000)
    b = run_experiment("B", "priority queue + HIGH prefetch (the fix that doesn't work)",
                       "prio.hol.b", use_priority=True, prefetch=5000)
    c = run_experiment("C", "priority queue + prefetch=1 (the real fix)",
                       "prio.hol.c", use_priority=True, prefetch=1)

    log("=" * 70)
    log("SUMMARY  OTP end-to-end latency (delivery to consumer), ms")
    log("=" * 70)
    log(f"  {'exp':4} {'priority':9} {'prefetch':>9} {'p50':>10} {'p99':>10} {'max':>10} {'drain msg/s':>12}")
    for r in (a, b, c):
        log(f"  {r['letter']:4} {('yes' if r['use_priority'] else 'no'):9} "
            f"{r['prefetch']:>9} {r['p50']:>10.1f} {r['p99']:>10.1f} {r['max']:>10.1f} "
            f"{r['drain']:>12.1f}")
    log("")
    log(f"  A -> B  adding priority alone: p99 {a['p99']:.0f} ms -> {b['p99']:.0f} ms")
    log(f"  B -> C  dropping prefetch to 1: p99 {b['p99']:.0f} ms -> {c['p99']:.1f} ms")
    log("")

    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "rabbitmq_version", "image_digest", "amqp_port", "mgmt_port",
            "backlog", "otp_count", "service_time_ms", "warmup", "otp_gap_ms",
            "x_max_priority", "bulk_priority", "otp_priority",
            "a_prefetch", "a_p50_ms", "a_p99_ms", "a_max_ms", "a_drain_msg_s",
            "b_prefetch", "b_p50_ms", "b_p99_ms", "b_max_ms", "b_drain_msg_s",
            "c_prefetch", "c_p50_ms", "c_p99_ms", "c_max_ms", "c_drain_msg_s",
        ])
        w.writerow([
            version, IMAGE_DIGEST, PORT, MGMT_PORT,
            BACKLOG, OTP_COUNT, SERVICE_TIME_MS, WARMUP, OTP_GAP_MS,
            MAX_PRIORITY, BULK_PRIORITY, OTP_PRIORITY,
            a["prefetch"], f"{a['p50']:.1f}", f"{a['p99']:.1f}", f"{a['max']:.1f}", f"{a['drain']:.1f}",
            b["prefetch"], f"{b['p50']:.1f}", f"{b['p99']:.1f}", f"{b['max']:.1f}", f"{b['drain']:.1f}",
            c["prefetch"], f"{c['p50']:.1f}", f"{c['p99']:.1f}", f"{c['max']:.1f}", f"{c['drain']:.1f}",
        ])

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(_LINES) + "\n")

    log(f"  RabbitMQ {version} | artifacts in {RESULTS}/")


if __name__ == "__main__":
    main()
