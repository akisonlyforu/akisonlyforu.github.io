"""Measure how the RabbitMQ publisher-confirm in-flight window affects publish
throughput.

A publisher using confirms can have at most N unconfirmed ("in-flight") messages
outstanding before it must block waiting for the broker to ack them. That N is the
confirm window. This harness sweeps N and watches sustained publish throughput,
against a real digest-pinned RabbitMQ 4.0 broker, using the official RabbitMQ
PerfTest load generator (also digest-pinned) run as a container on the same docker
network.

Two experiments (both against one quorum queue, persistent messages):
  A. In-flight sweet-spot sweep - fixed producers/consumers, sweep the confirm
     window -c across 1..1000, record avg send + receive rate. This is the curve.
  B. Confirms on vs off - fire-and-forget (no confirms) vs a small confirm window
     (-c 2), same setup, to see which sustains higher publish throughput.

Each data point is run REPEATS times against a FRESH queue (the previous run's
queue is deleted first) so a growing on-disk backlog does not bias later runs.
We report the median and the min/max spread.

IMPORTANT PerfTest note (verified on image 2.25.0): `-r 0` does NOT mean "unlimited
rate" in this PerfTest version - it means a literal rate of zero, i.e. publish
nothing. Unlimited rate is achieved by OMITTING -r entirely, which is what this
harness does. (The task brief's "-r 0 = unlimited" is stale for 2.25.0.)

Env overrides: RESULTS_DIR, NETWORK, BROKER_SERVICE, PERFTEST_IMAGE,
PRODUCERS, CONSUMERS, MSG_SIZE, DURATION, REPEATS.
"""
import csv
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))

NETWORK = os.environ.get("NETWORK", "rmq-confirms-net")
BROKER_SERVICE = os.environ.get("BROKER_SERVICE", "rabbitmq")   # reachable by name on NETWORK
# PerfTest, digest-pinned. `latest` at capture time resolved to 2.25.0 / this digest.
PERFTEST_IMAGE = os.environ.get(
    "PERFTEST_IMAGE",
    "pivotalrabbitmq/perf-test@sha256:b803911d60fdf6885fc6313f0d56a0c2c743bfdd27ac118f5e4b320c36c7b8c9",
)

PRODUCERS = int(os.environ.get("PRODUCERS", "30"))
CONSUMERS = int(os.environ.get("CONSUMERS", "4"))
MSG_SIZE = int(os.environ.get("MSG_SIZE", "1000"))     # bytes
DURATION = int(os.environ.get("DURATION", "15"))       # seconds per run
SETTLE = int(os.environ.get("SETTLE", "3"))            # seconds between runs
REPEATS = int(os.environ.get("REPEATS", "3"))
INTERVAL = 1                                            # -i, must be >0 or avg reports 0

CONFIRM_WINDOWS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 1000]

SEND_RE = re.compile(r"sending rate avg:\s*([\d.]+)\s*msg/s")
RECV_RE = re.compile(r"receiving rate avg:\s*([\d.]+)\s*msg/s")


def broker_container():
    out = subprocess.run(
        ["docker", "ps", "--filter", f"network={NETWORK}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    if not out:
        sys.exit(f"no container found on docker network {NETWORK}; is the broker up?")
    return out[0]


BROKER = None


def broker_version():
    out = subprocess.run(
        ["docker", "exec", BROKER, "rabbitmqctl", "version"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out or "unknown"


def delete_queue(name):
    subprocess.run(
        ["docker", "exec", BROKER, "rabbitmqctl", "delete_queue", name],
        capture_output=True, text=True,
    )


TRANSIENT = ("UnknownHostException", "Name or service not known",
             "ConnectException", "Connection refused", "nodename nor servname")


def _one_perftest(queue, confirm_window):
    cmd = [
        "docker", "run", "--rm", "--network", NETWORK, PERFTEST_IMAGE,
        "--uri", f"amqp://guest:guest@{BROKER_SERVICE}:5672",
        "-x", str(PRODUCERS), "-y", str(CONSUMERS),
        "-u", queue, "-z", str(DURATION), "-s", str(MSG_SIZE),
        "-i", str(INTERVAL),
        "--quorum-queue", "-f", "persistent",
        "--id", queue,
        # NOTE: no -r flag => unlimited publish rate (see module docstring).
    ]
    if confirm_window is not None:
        cmd += ["-c", str(confirm_window)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DURATION + 90)
    return proc.stdout + proc.stderr


def run_perftest(queue, confirm_window, attempts=4):
    """One PerfTest run, with retry on transient docker-DNS / connection blips.
    confirm_window=None -> fire-and-forget (no -c).
    Returns (send_rate, recv_rate) in msg/s parsed from the summary lines."""
    last = ""
    for attempt in range(1, attempts + 1):
        delete_queue(queue)                   # fresh queue: no stale on-disk backlog
        time.sleep(2)                         # let the broker settle the delete before we hammer
        out = _one_perftest(queue, confirm_window)
        last = out
        sm = SEND_RE.search(out)
        rm = RECV_RE.search(out)
        transient = any(t in out for t in TRANSIENT)
        # a real run has a nonzero send rate; 0 or missing => broker/DNS blip, retry
        if sm and rm and float(sm.group(1)) > 0 and not transient:
            delete_queue(queue)
            time.sleep(SETTLE)
            return float(sm.group(1)), float(rm.group(1))
        why = "transient docker/broker error" if transient else "zero/unparseable rate"
        sys.stderr.write(f"    [retry] {queue} c={confirm_window} attempt {attempt}/{attempts}: {why}\n")
        sys.stderr.flush()
        delete_queue(queue)
        time.sleep(SETTLE + 3)
    sys.stderr.write(last[-2000:])
    sys.exit(f"could not get a valid run for queue={queue} c={confirm_window} after {attempts} attempts")


def repeated(label, confirm_window):
    sends, recvs = [], []
    for i in range(REPEATS):
        q = f"pcx-{label}-{i}"
        s, r = run_perftest(q, confirm_window)
        sends.append(s)
        recvs.append(r)
        print(f"    {label:>10}  rep {i+1}/{REPEATS}  send={s:>9.0f}  recv={r:>9.0f} msg/s")
    return sends, recvs


def summarize(vals):
    return statistics.median(vals), min(vals), max(vals)


def main():
    global BROKER
    os.makedirs(RESULTS, exist_ok=True)
    BROKER = broker_container()
    ver = broker_version()

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 70)
    emit("RabbitMQ publisher-confirm in-flight window vs publish throughput")
    emit("=" * 70)
    emit(f"  broker            : rabbitmq {ver} (container {BROKER})")
    emit(f"  load generator    : {PERFTEST_IMAGE}")
    emit(f"  producers (-x)    : {PRODUCERS}")
    emit(f"  consumers (-y)    : {CONSUMERS}")
    emit(f"  msg size (-s)     : {MSG_SIZE} bytes")
    emit(f"  duration (-z)     : {DURATION} s   (interval -i {INTERVAL}, settle {SETTLE}s)")
    emit(f"  queue             : quorum, persistent (-f persistent)")
    emit(f"  rate              : unlimited (no -r flag; -r 0 would mean ZERO in PerfTest 2.25)")
    emit(f"  repeats per point : {REPEATS} (fresh queue each run; report median [min-max])")
    emit("")

    # ---------- Experiment A: in-flight sweep ----------
    emit("EXPERIMENT A  in-flight confirm-window sweep")
    emit("-" * 70)
    sweep_rows = []          # median rows for the spec CSV
    sweep_raw = []           # every repeat
    a_summary = []
    for c in CONFIRM_WINDOWS:
        print(f"  -c {c}")
        sends, recvs = repeated(f"c{c}", c)
        for i, (s, r) in enumerate(zip(sends, recvs)):
            sweep_raw.append({"confirm_window": c, "repeat": i + 1,
                              "send_rate_msgs_s": round(s), "recv_rate_msgs_s": round(r)})
        s_med, s_lo, s_hi = summarize(sends)
        r_med, _, _ = summarize(recvs)
        sweep_rows.append({"confirm_window": c, "producers": PRODUCERS,
                           "consumers": CONSUMERS,
                           "send_rate_msgs_s": round(s_med),
                           "recv_rate_msgs_s": round(r_med)})
        a_summary.append((c, s_med, s_lo, s_hi, r_med))

    peak = max(a_summary, key=lambda t: t[1])
    emit("")
    emit("  confirm_window   send msg/s (median [min-max])        recv msg/s")
    for c, s_med, s_lo, s_hi, r_med in a_summary:
        star = "  <-- peak" if c == peak[0] else ""
        emit(f"    {c:>6}         {s_med:>8.0f}  [{s_lo:>7.0f}-{s_hi:>7.0f}]"
             f"            {r_med:>8.0f}{star}")
    emit("")
    emit(f"  peak sustained publish throughput at confirm_window = {peak[0]} "
         f"({peak[1]:.0f} msg/s median)")
    emit("")

    # ---------- Experiment B: confirms on vs off ----------
    emit("EXPERIMENT B  confirms on vs off (fire-and-forget vs -c 2)")
    emit("-" * 70)
    print("  fire-and-forget (no -c)")
    faf_s, faf_r = repeated("faf", None)
    print("  confirms -c 2")
    cw2_s, cw2_r = repeated("cw2", 2)

    faf_sm, faf_slo, faf_shi = summarize(faf_s)
    faf_rm, _, _ = summarize(faf_r)
    cw2_sm, cw2_slo, cw2_shi = summarize(cw2_s)
    cw2_rm, _, _ = summarize(cw2_r)

    onoff_rows = [
        {"mode": "fire_and_forget", "send_rate_msgs_s": round(faf_sm),
         "recv_rate_msgs_s": round(faf_rm)},
        {"mode": "confirms_window_2", "send_rate_msgs_s": round(cw2_sm),
         "recv_rate_msgs_s": round(cw2_rm)},
    ]
    emit("")
    emit("  mode                 send msg/s (median [min-max])     recv msg/s")
    emit(f"    fire-and-forget    {faf_sm:>8.0f}  [{faf_slo:>7.0f}-{faf_shi:>7.0f}]"
         f"         {faf_rm:>8.0f}")
    emit(f"    confirms -c 2      {cw2_sm:>8.0f}  [{cw2_slo:>7.0f}-{cw2_shi:>7.0f}]"
         f"         {cw2_rm:>8.0f}")
    emit("")
    faf_backlog = faf_sm - faf_rm
    emit(f"  fire-and-forget send/recv gap: {faf_backlog:.0f} msg/s "
         f"(publish accepted into a growing backlog consumers can't drain)")
    emit(f"  confirms -c 2 send/recv gap:   {cw2_sm - cw2_rm:.0f} msg/s "
         f"(bounded in-flight keeps send ~= receive)")
    emit("")

    # ---------- write artifacts ----------
    with open(os.path.join(RESULTS, "inflight_sweep.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["confirm_window", "producers", "consumers",
                                          "send_rate_msgs_s", "recv_rate_msgs_s"])
        w.writeheader()
        w.writerows(sweep_rows)
    with open(os.path.join(RESULTS, "inflight_sweep_raw.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["confirm_window", "repeat",
                                          "send_rate_msgs_s", "recv_rate_msgs_s"])
        w.writeheader()
        w.writerows(sweep_raw)
    with open(os.path.join(RESULTS, "confirms_onoff.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mode", "send_rate_msgs_s", "recv_rate_msgs_s"])
        w.writeheader()
        w.writerows(onoff_rows)

    rmq_ver = ver.replace("\n", " ").strip()
    with open(os.path.join(RESULTS, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rabbitmq_version", "perftest_image_digest", "rabbitmq_image_digest",
                    "producers_x", "consumers_y", "msg_size_bytes_s", "duration_s_z",
                    "queue_type", "rate", "repeats", "sweep_peak_window",
                    "sweep_peak_send_msgs_s"])
        w.writerow([rmq_ver,
                    "sha256:b803911d60fdf6885fc6313f0d56a0c2c743bfdd27ac118f5e4b320c36c7b8c9",
                    "sha256:ad4268113c27d02f08ac1151f9651d6e475c955f81c3a5ad522b79955ce11cf3",
                    PRODUCERS, CONSUMERS, MSG_SIZE, DURATION,
                    "quorum+persistent", "unlimited(no -r)", REPEATS,
                    peak[0], round(peak[1])])

    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")

    emit(f"  artifacts in {RESULTS}/")
    emit("    inflight_sweep.csv, inflight_sweep_raw.csv, confirms_onoff.csv,")
    emit("    run_metadata.csv, summary.txt")


if __name__ == "__main__":
    main()
