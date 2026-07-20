"""Measure the p99.9 latency spike a full replica resync causes on a Redis primary,
and whether repl-diskless-sync softens it. We're on macOS Docker Desktop: Redis runs
inside the LinuxKit VM, and THP (Transparent Huge Pages) is a HOST KERNEL setting we
cannot toggle from a container or from macOS. So this harness does NOT run a THP-on
vs THP-off comparison (that needs a Linux host you control). What it DOES do, for
real, under whatever THP state this Docker Desktop VM actually has (observed and
recorded, not assumed):

  1. STEADY   - continuous write load against the primary, no replica attached.
  2. DISK     - attach a fresh replica with repl-diskless-sync=no -> forces a disk-based
                FULLRESYNC (fork -> RDB file -> transfer) while writes continue.
  3. DISKLESS - same trigger, repl-diskless-sync=yes -> fork -> RDB streamed straight to
                the replica's socket, no temp file. This is the fix that needs no host
                access, and arm 2 vs arm 3 is the post's main measured contrast.
  4. CONTROL  - (optional) same disk-sync trigger, but the primary sees a READ-ONLY
                workload during the sync. COW faults are only paid when the PARENT
                (primary) writes to pages after fork, so this should NOT spike. Kept
                under results/attempts/ as a documented negative control.

Every arm reuses one primary (continuous write pressure never stops except for the
control arm's window) and attaches a *fresh* ephemeral replica container per arm, so
every attach is a guaranteed FULLRESYNC (a brand-new container shares no replication
history with the primary).

Env overrides: REDIS_HOST, PRIMARY_PORT, REPLICA_PORT, RESULTS_DIR, DOCKER_NETWORK,
PRIMARY_CONTAINER, REDIS_IMAGE, DATASET_KEYS, VALUE_BYTES, SEED, STEADY_SECONDS,
SYNC_TAIL_SECONDS, RUN_CONTROL (1/0).
"""
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import namedtuple

import redis

HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
PRIMARY_PORT = int(os.environ.get("PRIMARY_PORT", "6396"))
REPLICA_PORT = int(os.environ.get("REPLICA_PORT", "6397"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results"))
NETWORK = os.environ.get("DOCKER_NETWORK", "redisthp-net")
PRIMARY_CONTAINER = os.environ.get("PRIMARY_CONTAINER", "redisthp-primary")
IMAGE = os.environ.get(
    "REDIS_IMAGE",
    "redis:7.4.0@sha256:6725a7dc7a44a6486b9d0a5172b10ccaf0c2ea600df87c0b93450d0e7769297f",
)

DATASET_KEYS = int(os.environ.get("DATASET_KEYS", "500000"))
VALUE_BYTES = int(os.environ.get("VALUE_BYTES", "350"))
SEED = int(os.environ.get("SEED", "20260720"))
STEADY_SECONDS = float(os.environ.get("STEADY_SECONDS", "25"))
SYNC_TAIL_SECONDS = float(os.environ.get("SYNC_TAIL_SECONDS", "4"))
RUN_CONTROL = os.environ.get("RUN_CONTROL", "1") != "0"

SAMPLE_INTERVAL_S = float(os.environ.get("SAMPLE_INTERVAL_S", "0.004"))  # ~250/s probe target
BULK_WRITERS = int(os.environ.get("BULK_WRITERS", "4"))
BULK_BATCH = int(os.environ.get("BULK_BATCH", "40"))

Sample = namedtuple("Sample", ["t_ms", "latency_ms", "phase", "arm"])


def rc(port):
    return redis.Redis(host=HOST, port=port, decode_responses=True, socket_timeout=10)


def sh(*args, check=True, capture=True):
    return subprocess.run(list(args), check=check, capture_output=capture, text=True)


def pct(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def random_value(rng, n):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(rng.choice(alphabet) for _ in range(n))


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def load_dataset(p, n, value_bytes, seed):
    print(f"loading {n} keys x {value_bytes} bytes (seed={seed}) ...")
    rng = random.Random(seed)
    p.flushall()
    t0 = time.time()
    pipe = p.pipeline(transaction=False)
    batch = 1000
    for i in range(n):
        pipe.set(f"k:{i}", random_value(rng, value_bytes))
        if (i + 1) % batch == 0:
            pipe.execute()
            pipe = p.pipeline(transaction=False)
    pipe.execute()
    dt = time.time() - t0
    info = p.info("memory")
    print(f"  loaded in {dt:.1f}s, used_memory_human={info['used_memory_human']}")
    return info["used_memory_human"], info["used_memory"]


# --------------------------------------------------------------------------
# Load generator: one latency-sampling probe connection (single in-flight
# request, true round trip) + N bulk pipelined writer/reader connections that
# apply the actual write (or read) pressure. Both run for the whole run;
# main() flips `state.phase`/`state.arm`/`state.mode` as it moves through
# scenarios and the sampler/writers tag every row accordingly.
# --------------------------------------------------------------------------

class RunState:
    def __init__(self):
        self.phase = "warmup"
        self.arm = "none"
        self.mode = "write"       # "write" or "read" (for the negative control)
        self.stop = False
        self.samples = []
        self.lock = threading.Lock()
        self.ops_counter = 0
        self.t_start = time.time()

    def now_ms(self):
        return (time.time() - self.t_start) * 1000.0

    def record(self, latency_ms):
        with self.lock:
            self.samples.append(Sample(self.now_ms(), latency_ms, self.phase, self.arm))

    def snapshot_and_clear(self):
        with self.lock:
            out, self.samples = self.samples, []
        return out


def probe_loop(state, n_keys, seed):
    rng = random.Random(seed ^ 0xA5A5)
    conn = rc(PRIMARY_PORT)
    next_tick = time.perf_counter()
    while not state.stop:
        key = f"k:{rng.randrange(n_keys)}"
        t0 = time.perf_counter()
        try:
            if state.mode == "read":
                conn.get(key)
            else:
                conn.set(key, random_value(rng, VALUE_BYTES))
            dt_ms = (time.perf_counter() - t0) * 1000.0
            state.record(dt_ms)
        except redis.exceptions.RedisError as e:
            state.record(-1.0)  # visible marker for a failed/timed-out probe
        next_tick += SAMPLE_INTERVAL_S
        sleep_for = next_tick - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_tick = time.perf_counter()


def bulk_loop(state, n_keys, seed, worker_id):
    rng = random.Random(seed ^ (0xC0FFEE + worker_id))
    conn = rc(PRIMARY_PORT)
    while not state.stop:
        try:
            pipe = conn.pipeline(transaction=False)
            for _ in range(BULK_BATCH):
                key = f"k:{rng.randrange(n_keys)}"
                if state.mode == "read":
                    pipe.get(key)
                else:
                    pipe.set(key, random_value(rng, VALUE_BYTES))
            pipe.execute()
            with state.lock:
                state.ops_counter += BULK_BATCH
        except redis.exceptions.RedisError:
            time.sleep(0.05)


# --------------------------------------------------------------------------
# Docker helpers for ephemeral replica containers
# --------------------------------------------------------------------------

def start_fresh_replica(name):
    sh("docker", "rm", "-f", name, check=False)
    sh(
        "docker", "run", "-d", "--name", name,
        "--network", NETWORK,
        "-p", f"127.0.0.1:{REPLICA_PORT}:6379",
        IMAGE, "redis-server", "--save", "", "--appendonly", "no",
    )
    r = rc(REPLICA_PORT)
    end = time.time() + 15
    while time.time() < end:
        try:
            if r.ping():
                return r
        except redis.exceptions.RedisError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"replica container {name} never came up")


def stop_replica(name):
    sh("docker", "rm", "-f", name, check=False)


def wait_fullresync(replica_client, timeout=60):
    """Returns the wall-clock timestamp the link came up (sync complete)."""
    end = time.time() + timeout
    while time.time() < end:
        info = replica_client.info("replication")
        if info.get("role") == "slave" and info.get("master_link_status") == "up":
            return time.time()
        time.sleep(0.05)
    raise RuntimeError("replica never reached master_link_status=up (no FULLRESYNC completion)")


def anon_hugepages_kb():
    """Poll AnonHugePages from the primary's own smaps_rollup (PID 1 in-container).
    Returns (kb:int, raw_line:str) or (None, error_text) if not observable here."""
    try:
        out = sh("docker", "exec", PRIMARY_CONTAINER, "sh", "-c",
                  "cat /proc/1/smaps_rollup 2>&1")
        for line in out.stdout.splitlines():
            if line.startswith("AnonHugePages:"):
                kb = int(line.split()[1])
                return kb, out.stdout
        return None, out.stdout
    except subprocess.CalledProcessError as e:
        return None, (e.stdout or "") + (e.stderr or "")


def thp_state():
    """Observe (don't assume) THP state inside the container's kernel view, and
    from the macOS host if reachable (it won't be — recorded honestly either way)."""
    container_thp = "not observed"
    try:
        out = sh("docker", "exec", PRIMARY_CONTAINER, "sh", "-c",
                  "cat /sys/kernel/mm/transparent_hugepage/enabled 2>&1")
        container_thp = out.stdout.strip()
    except subprocess.CalledProcessError as e:
        container_thp = f"error: {(e.stdout or '').strip()} {(e.stderr or '').strip()}".strip()

    host_thp = "not observed"
    try:
        with open("/sys/kernel/mm/transparent_hugepage/enabled") as f:
            host_thp = f.read().strip()
    except FileNotFoundError:
        host_thp = "not present on this host (macOS has no such sysfs path; " \
                   "Redis runs inside Docker Desktop's LinuxKit VM, not directly on macOS)"
    except Exception as e:  # pragma: no cover - defensive
        host_thp = f"error reading host path: {e}"

    return container_thp, host_thp


# --------------------------------------------------------------------------
# Scenario runner
# --------------------------------------------------------------------------

def run_sync_arm(p, state, arm_name, diskless, results_prefix, results_dir):
    """Attach a fresh replica forcing a FULLRESYNC under the given repl-diskless-sync
    setting, sample latency throughout, and capture fork/mem/latency-doctor evidence."""
    replica_name = f"redisthp-replica-{arm_name}"
    p.execute_command("CONFIG", "SET", "repl-diskless-sync", "yes" if diskless else "no")
    p.execute_command("LATENCY", "RESET")
    fork_before = int(p.info("stats").get("latest_fork_usec", 0))

    state.arm = arm_name
    state.phase = "pre_sync"
    replica = start_fresh_replica(replica_name)

    peak_anon_kb, peak_anon_raw = -1, "no sample taken"
    t_sync_start = time.time()
    state.phase = "sync"
    replica.execute_command("REPLICAOF", PRIMARY_CONTAINER, "6379")

    # Tight poll loop (direct redis-py calls, no subprocess) to precisely bound the
    # window the forked RDB-save child is actually alive -- that's the real COW
    # pressure window (repl-diskless-sync just changes whether that child writes to
    # a file or streams straight to the replica socket). We also wait for link-up
    # here, and take a slower-cadence smaps snapshot (docker exec is expensive)
    # piggybacked on the same loop.
    bgsave_start_wall = None
    bgsave_end_wall = None
    seen_in_progress = False
    linked_at = None
    end_guard = time.time() + 60
    last_smaps_check = 0.0
    while time.time() < end_guard:
        now = time.time()
        try:
            persist = p.info("persistence")
            in_progress = bool(int(persist.get("rdb_bgsave_in_progress", 0)))
            if in_progress and bgsave_start_wall is None:
                bgsave_start_wall = now
                seen_in_progress = True
            if seen_in_progress and not in_progress and bgsave_start_wall is not None and bgsave_end_wall is None:
                bgsave_end_wall = now
        except redis.exceptions.RedisError:
            pass

        if now - last_smaps_check >= 0.15:
            last_smaps_check = now
            kb, raw = anon_hugepages_kb()
            if kb is not None and kb > peak_anon_kb:
                peak_anon_kb, peak_anon_raw = kb, raw

        try:
            info = replica.info("replication")
            if info.get("role") == "slave" and info.get("master_link_status") == "up":
                linked_at = now
                break
        except redis.exceptions.RedisError:
            pass
        time.sleep(0.015)
    if linked_at is None:
        raise RuntimeError(f"{arm_name}: replica never reached FULLRESYNC completion")
    if bgsave_start_wall is not None and bgsave_end_wall is None:
        bgsave_end_wall = linked_at  # child was still alive/exiting right at link-up

    sync_duration_s = linked_at - t_sync_start
    fork_child_alive_s = (bgsave_end_wall - bgsave_start_wall) if bgsave_start_wall else None

    # tail window: COW pressure can persist briefly after link-up while the
    # child process's copied pages get reclaimed / parent settles.
    state.phase = "sync_tail"
    time.sleep(SYNC_TAIL_SECONDS)
    for _ in range(5):
        kb, raw = anon_hugepages_kb()
        if kb is not None and kb > peak_anon_kb:
            peak_anon_kb, peak_anon_raw = kb, raw
        time.sleep(SYNC_TAIL_SECONDS / 5)

    fork_after = int(p.info("stats").get("latest_fork_usec", 0))
    doctor = p.execute_command("LATENCY", "DOCTOR")
    history_fork = p.execute_command("LATENCY", "HISTORY", "fork")

    doctor_path = os.path.join(results_dir, f"latency_doctor_{results_prefix}.txt")
    with open(doctor_path, "w") as f:
        f.write("=== LATENCY DOCTOR ===\n")
        f.write(str(doctor) + "\n\n")
        f.write("=== LATENCY HISTORY fork ===\n")
        f.write(str(history_fork) + "\n")

    smaps_path = os.path.join(results_dir, f"smaps_{results_prefix}.txt")
    with open(smaps_path, "w") as f:
        if peak_anon_kb >= 0:
            f.write(f"# peak AnonHugePages observed during {arm_name} sync window: {peak_anon_kb} kB\n")
            f.write("# raw /proc/1/smaps_rollup at that peak sample, untouched:\n\n")
            f.write(peak_anon_raw)
        else:
            f.write("AnonHugePages was not observable in this environment.\n")
            f.write("Raw attempt output:\n\n")
            f.write(peak_anon_raw)

    state.phase = "post_sync"
    stop_replica(replica_name)

    return {
        "arm": arm_name,
        "diskless": diskless,
        "sync_duration_s": sync_duration_s,
        "fork_usec_before": fork_before,
        "fork_usec_after": fork_after,
        "latest_fork_usec": fork_after,
        "peak_anon_hugepages_kb": peak_anon_kb,
        "t_sync_start_ms": None,  # filled by caller relative to run start
        "sync_start_wall": t_sync_start,
        "sync_end_wall": linked_at,
        "bgsave_start_wall": bgsave_start_wall,
        "bgsave_end_wall": bgsave_end_wall,
        "fork_child_alive_s": fork_child_alive_s,
    }


def summarize(samples, phase_filter=None, arm_filter=None, t_start_ms=None, t_end_ms=None):
    vals = [s.latency_ms for s in samples
            if s.latency_ms >= 0
            and (phase_filter is None or s.phase in phase_filter)
            and (arm_filter is None or s.arm == arm_filter)
            and (t_start_ms is None or s.t_ms >= t_start_ms)
            and (t_end_ms is None or s.t_ms <= t_end_ms)]
    if not vals:
        return None
    return {
        "n": len(vals),
        "p50": pct(vals, 50),
        "p99": pct(vals, 99),
        "p999": pct(vals, 99.9),
        "max": max(vals),
        "mean": statistics.mean(vals),
    }


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    attempts_dir = os.path.join(RESULTS_DIR, "attempts", "read-only-control")
    os.makedirs(attempts_dir, exist_ok=True)

    p = rc(PRIMARY_PORT)
    p.ping()
    server_info = p.info("server")
    redis_version = server_info["redis_version"]
    mem_allocator = p.info("memory").get("mem_allocator", "unknown")  # lives in the memory section, not server
    print(f"redis {redis_version}, allocator={mem_allocator}")

    container_thp, host_thp = thp_state()
    print(f"THP (container view): {container_thp}")
    print(f"THP (macOS host path): {host_thp}")

    used_memory_human, used_memory_bytes = load_dataset(p, DATASET_KEYS, VALUE_BYTES, SEED)

    state = RunState()
    threads = [threading.Thread(target=probe_loop, args=(state, DATASET_KEYS, SEED), daemon=True)]
    for i in range(BULK_WRITERS):
        threads.append(threading.Thread(target=bulk_loop, args=(state, DATASET_KEYS, SEED, i), daemon=True))
    for t in threads:
        t.start()

    # brief warmup so the pool of connections/threads is fully up before we measure
    time.sleep(2)

    # --- 1. STEADY -----------------------------------------------------
    state.phase, state.arm, state.mode = "steady", "steady", "write"
    print(f"steady-state window: {STEADY_SECONDS}s, no replica attached ...")
    time.sleep(STEADY_SECONDS)

    # --- 2. DISK-BASED FULL SYNC ----------------------------------------
    print("arm DISK: repl-diskless-sync=no, attaching fresh replica ...")
    disk_result = run_sync_arm(p, state, "disk_sync", diskless=False,
                                results_prefix="disk_sync", results_dir=RESULTS_DIR)
    print(f"  sync duration: {disk_result['sync_duration_s']:.2f}s, "
          f"fork_usec={disk_result['latest_fork_usec']}, "
          f"peak AnonHugePages={disk_result['peak_anon_hugepages_kb']} kB")

    state.phase, state.arm = "cooldown", "cooldown"
    time.sleep(3)

    # --- 3. DISKLESS FULL SYNC ------------------------------------------
    print("arm DISKLESS: repl-diskless-sync=yes, attaching fresh replica ...")
    diskless_result = run_sync_arm(p, state, "diskless_sync", diskless=True,
                                    results_prefix="diskless_sync", results_dir=RESULTS_DIR)
    print(f"  sync duration: {diskless_result['sync_duration_s']:.2f}s, "
          f"fork_usec={diskless_result['latest_fork_usec']}, "
          f"peak AnonHugePages={diskless_result['peak_anon_hugepages_kb']} kB")

    state.phase, state.arm = "cooldown", "cooldown"
    time.sleep(3)
    state.stop = True
    for t in threads:
        t.join(timeout=5)

    main_samples = state.snapshot_and_clear()

    # --- 4. (optional) NEGATIVE CONTROL: read-only workload, disk sync -
    control_result = None
    control_samples = []
    if RUN_CONTROL:
        print("attempts/: read-only control (disk-sync trigger, GET-only workload) ...")
        cstate = RunState()
        cstate.mode = "read"
        cthreads = [threading.Thread(target=probe_loop, args=(cstate, DATASET_KEYS, SEED + 1), daemon=True)]
        for i in range(BULK_WRITERS):
            cthreads.append(threading.Thread(target=bulk_loop, args=(cstate, DATASET_KEYS, SEED + 1, i), daemon=True))
        for t in cthreads:
            t.start()
        time.sleep(2)
        cstate.phase, cstate.arm = "steady", "control_steady"
        time.sleep(10)
        control_result = run_sync_arm(p, cstate, "control_readonly", diskless=False,
                                       results_prefix="control_readonly", results_dir=attempts_dir)
        cstate.stop = True
        for t in cthreads:
            t.join(timeout=5)
        control_samples = cstate.snapshot_and_clear()
        print(f"  control sync duration: {control_result['sync_duration_s']:.2f}s, "
              f"fork_usec={control_result['latest_fork_usec']}")

    # --------------------------------------------------------------
    # Write results
    # --------------------------------------------------------------
    import csv

    all_samples = main_samples
    with open(os.path.join(RESULTS_DIR, "latency_timeline.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "latency_ms", "phase", "arm",
                    "sync_marker"])
        markers = {
            disk_result["arm"]: (disk_result["sync_start_wall"] - state.t_start) * 1000.0,
            diskless_result["arm"]: (diskless_result["sync_start_wall"] - state.t_start) * 1000.0,
        }
        for s in all_samples:
            marker = ""
            m = markers.get(s.arm)
            if m is not None and abs(s.t_ms - m) < (SAMPLE_INTERVAL_S * 1000):
                marker = "sync_start"
            w.writerow([f"{s.t_ms:.1f}", f"{s.latency_ms:.4f}", s.phase, s.arm, marker])

    if RUN_CONTROL:
        with open(os.path.join(attempts_dir, "latency_timeline.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_ms", "latency_ms", "phase", "arm"])
            for s in control_samples:
                w.writerow([f"{s.t_ms:.1f}", f"{s.latency_ms:.4f}", s.phase, s.arm])

    steady_stats = summarize(main_samples, phase_filter={"steady"})
    disk_sync_stats = summarize(main_samples, phase_filter={"sync", "sync_tail"}, arm_filter="disk_sync")
    diskless_sync_stats = summarize(main_samples, phase_filter={"sync", "sync_tail"}, arm_filter="diskless_sync")
    control_steady_stats = summarize(control_samples, phase_filter={"steady"}) if RUN_CONTROL else None
    control_sync_stats = summarize(control_samples, phase_filter={"sync", "sync_tail"}, arm_filter="control_readonly") if RUN_CONTROL else None

    # Tighter window: only while the forked RDB-save child was actually alive
    # (the real COW-pressure window), bounded by bgsave_start/end wall clocks.
    def fork_window_stats(result):
        if not result.get("bgsave_start_wall") or not result.get("bgsave_end_wall"):
            return None
        t0 = (result["bgsave_start_wall"] - state.t_start) * 1000.0
        t1 = (result["bgsave_end_wall"] - state.t_start) * 1000.0
        return summarize(main_samples, arm_filter=result["arm"], t_start_ms=t0, t_end_ms=t1)

    disk_fork_stats = fork_window_stats(disk_result)
    diskless_fork_stats = fork_window_stats(diskless_result)

    with open(os.path.join(RESULTS_DIR, "latency_percentiles.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "n", "p50_ms", "p99_ms", "p999_ms", "max_ms", "mean_ms"])
        for name, stats in [
            ("steady", steady_stats),
            ("disk_sync", disk_sync_stats),
            ("diskless_sync", diskless_sync_stats),
            ("disk_sync_fork_child_alive_only", disk_fork_stats),
            ("diskless_sync_fork_child_alive_only", diskless_fork_stats),
        ]:
            if stats:
                w.writerow([name, stats["n"], f"{stats['p50']:.3f}", f"{stats['p99']:.3f}",
                            f"{stats['p999']:.3f}", f"{stats['max']:.3f}", f"{stats['mean']:.3f}"])
            else:
                w.writerow([name, 0, "", "", "", "", ""])

    if RUN_CONTROL:
        with open(os.path.join(attempts_dir, "latency_percentiles.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scenario", "n", "p50_ms", "p99_ms", "p999_ms", "max_ms", "mean_ms"])
            for name, stats in [("control_steady", control_steady_stats),
                                 ("control_readonly_sync", control_sync_stats)]:
                if stats:
                    w.writerow([name, stats["n"], f"{stats['p50']:.3f}", f"{stats['p99']:.3f}",
                                f"{stats['p999']:.3f}", f"{stats['max']:.3f}", f"{stats['mean']:.3f}"])
                else:
                    w.writerow([name, 0, "", "", "", "", ""])

    with open(os.path.join(RESULTS_DIR, "fork_and_mem.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "latest_fork_usec", "fork_ms", "peak_anon_hugepages_kb",
                     "sync_duration_s", "fork_child_alive_s"])
        for r in [disk_result, diskless_result]:
            fork_ms = r["latest_fork_usec"] / 1000.0
            alive = r.get("fork_child_alive_s")
            w.writerow([r["arm"], r["latest_fork_usec"], f"{fork_ms:.3f}",
                        r["peak_anon_hugepages_kb"], f"{r['sync_duration_s']:.3f}",
                        f"{alive:.3f}" if alive is not None else ""])

    if RUN_CONTROL and control_result:
        with open(os.path.join(attempts_dir, "fork_and_mem.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scenario", "latest_fork_usec", "fork_ms", "peak_anon_hugepages_kb",
                         "sync_duration_s"])
            fork_ms = control_result["latest_fork_usec"] / 1000.0
            w.writerow([control_result["arm"], control_result["latest_fork_usec"], f"{fork_ms:.3f}",
                        control_result["peak_anon_hugepages_kb"], f"{control_result['sync_duration_s']:.3f}"])

    measured_ops = state.ops_counter + len([s for s in main_samples if s.latency_ms >= 0])
    steady_wallclock_s = STEADY_SECONDS
    ops_rate = None
    steady_probe_n = steady_stats["n"] if steady_stats else 0
    # approximate combined ops/sec during the steady window from probe + bulk counters
    # (bulk counter is cumulative across the whole run; report probe-only rate as the
    # precise figure and note bulk throughput separately)
    probe_rate = steady_probe_n / steady_wallclock_s if steady_wallclock_s else 0

    with open(os.path.join(RESULTS_DIR, "run_metadata.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        w.writerow(["redis_version", redis_version])
        w.writerow(["mem_allocator", mem_allocator])
        w.writerow(["image", IMAGE])
        w.writerow(["dataset_keys", DATASET_KEYS])
        w.writerow(["value_bytes", VALUE_BYTES])
        w.writerow(["seed", SEED])
        w.writerow(["used_memory_human_after_load", used_memory_human])
        w.writerow(["used_memory_bytes_after_load", used_memory_bytes])
        w.writerow(["thp_enabled_container_view", container_thp])
        w.writerow(["thp_enabled_macos_host_path", host_thp])
        w.writerow(["probe_ops_per_sec_steady", f"{probe_rate:.1f}"])
        w.writerow(["bulk_writer_threads", BULK_WRITERS])
        w.writerow(["bulk_batch_size", BULK_BATCH])
        w.writerow(["bulk_ops_cumulative_all_phases", state.ops_counter])
        w.writerow(["steady_window_s", STEADY_SECONDS])
        w.writerow(["sync_tail_window_s", SYNC_TAIL_SECONDS])
        w.writerow(["disk_sync_duration_s", f"{disk_result['sync_duration_s']:.3f}"])
        w.writerow(["diskless_sync_duration_s", f"{diskless_result['sync_duration_s']:.3f}"])
        w.writerow(["timestamp_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())])
        w.writerow(["docker_desktop_note",
                    "Redis runs inside Docker Desktop's LinuxKit VM on macOS; THP state "
                    "is whatever that VM's kernel has, observed above, not chosen by us; "
                    "THP on/off A-vs-B comparison from HANDOFF.md was out of scope here "
                    "(needs a Linux host with writable /sys/kernel/mm/transparent_hugepage)."])

    # --------------------------------------------------------------
    # Self-verification (mechanics, not effect size -- we report a weak or
    # absent effect honestly rather than failing the run over it)
    # --------------------------------------------------------------
    problems = []
    if not steady_stats:
        problems.append("no steady-state samples captured")
    if not disk_sync_stats:
        problems.append("no disk-sync-window samples captured")
    if not diskless_sync_stats:
        problems.append("no diskless-sync-window samples captured")
    if disk_result["sync_duration_s"] <= 0:
        problems.append("disk sync duration was not positive")
    if diskless_result["sync_duration_s"] <= 0:
        problems.append("diskless sync duration was not positive")
    if disk_result["latest_fork_usec"] <= 0:
        problems.append("latest_fork_usec was not captured for disk arm")

    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    if steady_stats and disk_sync_stats:
        print(f"steady:      p50={steady_stats['p50']:.3f}ms p99={steady_stats['p99']:.3f}ms "
              f"p999={steady_stats['p999']:.3f}ms n={steady_stats['n']}")
        print(f"disk_sync:   p50={disk_sync_stats['p50']:.3f}ms p99={disk_sync_stats['p99']:.3f}ms "
              f"p999={disk_sync_stats['p999']:.3f}ms n={disk_sync_stats['n']}")
    if diskless_sync_stats:
        print(f"diskless_sync: p50={diskless_sync_stats['p50']:.3f}ms p99={diskless_sync_stats['p99']:.3f}ms "
              f"p999={diskless_sync_stats['p999']:.3f}ms n={diskless_sync_stats['n']}")
    if steady_stats and disk_sync_stats:
        ratio = disk_sync_stats["p999"] / steady_stats["p999"] if steady_stats["p999"] else float("inf")
        verdict = "SPIKE REPRODUCED" if ratio >= 2.0 else "WEAK/NO SPIKE (reported honestly, not forced)"
        print(f"disk_sync p999 / steady p999 = {ratio:.2f}x -> {verdict}")
    if disk_sync_stats and diskless_sync_stats:
        soften = disk_sync_stats["p999"] - diskless_sync_stats["p999"]
        print(f"disk_sync p999 - diskless_sync p999 = {soften:+.3f}ms "
              f"({'diskless softened it' if soften > 0 else 'no softening observed, reported as-is'})")
    print(f"fork cost (disk arm): {disk_result['latest_fork_usec']} usec "
          f"({disk_result['latest_fork_usec']/1000.0:.3f} ms)")
    print(f"fork cost (diskless arm): {diskless_result['latest_fork_usec']} usec "
          f"({diskless_result['latest_fork_usec']/1000.0:.3f} ms)")
    print(f"fork-child-alive window (disk arm): "
          f"{disk_result['fork_child_alive_s']}")
    print(f"fork-child-alive window (diskless arm): "
          f"{diskless_result['fork_child_alive_s']}")
    if disk_fork_stats:
        print(f"disk_sync FORK-CHILD-ALIVE-ONLY window: p50={disk_fork_stats['p50']:.3f}ms "
              f"p99={disk_fork_stats['p99']:.3f}ms p999={disk_fork_stats['p999']:.3f}ms "
              f"n={disk_fork_stats['n']}")
    else:
        print("disk_sync FORK-CHILD-ALIVE-ONLY window: not resolvable (bgsave too fast to sample, or no samples in window)")
    if diskless_fork_stats:
        print(f"diskless_sync FORK-CHILD-ALIVE-ONLY window: p50={diskless_fork_stats['p50']:.3f}ms "
              f"p99={diskless_fork_stats['p99']:.3f}ms p999={diskless_fork_stats['p999']:.3f}ms "
              f"n={diskless_fork_stats['n']}")
    else:
        print("diskless_sync FORK-CHILD-ALIVE-ONLY window: not resolvable (bgsave too fast to sample, or no samples in window)")
    print(f"AnonHugePages peak (disk arm): {disk_result['peak_anon_hugepages_kb']} kB")
    print(f"AnonHugePages peak (diskless arm): {diskless_result['peak_anon_hugepages_kb']} kB")

    if problems:
        print("\nMECHANICAL PROBLEMS (hard failure):")
        for pr in problems:
            print(f"  - {pr}")
        print(f"\nartifacts written to {RESULTS_DIR} before exiting non-zero.")
        sys.exit(1)

    print(f"\nartifacts written to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
