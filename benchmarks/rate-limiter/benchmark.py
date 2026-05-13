#!/usr/bin/env python3
"""Reproducible Redis experiments for the rate-limiting post."""

import argparse
import csv
import multiprocessing
import os
import platform
import queue
import subprocess
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import redis

from limiters import (
    FIXED_WINDOW_LUA,
    FixedWindow,
    FixedWindowLua,
    FixedWindowTwoKeyNaive,
    SlidingWindowCounter,
    SlidingWindowLog,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"
PRIMARY_URL = os.environ.get("RATE_LIMIT_PRIMARY_URL", "redis://127.0.0.1:56380/0")
REPLICA_URL = os.environ.get("RATE_LIMIT_REPLICA_URL", "redis://127.0.0.1:56381/0")
REPLICA_HOST = os.environ.get("RATE_LIMIT_REPLICA_PRIMARY_HOST", "redis-primary")


def redis_connect(url):
    return redis.Redis.from_url(url, decode_responses=True)


def wait_for_replica(primary, replica, timeout=30.0):
    deadline = time.monotonic() + timeout
    token = "sync-%s" % time.monotonic_ns()
    primary.set("benchmark:replica-probe", token)
    last_error = None
    while time.monotonic() < deadline:
        try:
            info = replica.info("replication")
            if info.get("role") == "slave" and info.get("master_link_status") == "up":
                if replica.get("benchmark:replica-probe") == token:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.02)
    raise RuntimeError("Redis replica did not catch up: %s" % last_error)


def attach_replica(primary, replica):
    replica.execute_command("REPLICAOF", REPLICA_HOST, 6379)
    wait_for_replica(primary, replica)


def wait_for_services(timeout=60.0):
    primary = redis_connect(PRIMARY_URL)
    replica = redis_connect(REPLICA_URL)
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            primary.ping()
            replica.ping()
            role = replica.info("replication").get("role")
            if role != "slave":
                replica.execute_command("REPLICAOF", REPLICA_HOST, 6379)
            wait_for_replica(primary, replica, timeout=5.0)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError("Redis services did not become ready: %s" % last_error)


class AtomicCounter:
    def __init__(self, value=0):
        self.value = value
        self.lock = threading.Lock()

    def increment(self, amount=1):
        with self.lock:
            self.value += amount
            return self.value

    def get(self):
        with self.lock:
            return self.value


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def command_version(command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return "unavailable"


def write_metadata(results_dir, args):
    primary = redis_connect(PRIMARY_URL)
    rows = [
        {"key": "run_at_utc", "value": datetime.now(timezone.utc).isoformat()},
        {"key": "platform", "value": platform.platform()},
        {"key": "python", "value": platform.python_version()},
        {"key": "docker", "value": command_version(["docker", "--version"])},
        {"key": "docker_compose", "value": command_version(["docker", "compose", "version"])},
        {"key": "redis_server", "value": primary.info("server").get("redis_version", "unknown")},
        {"key": "redis_image", "value": "redis:7"},
        {"key": "primary_port", "value": "56380"},
        {"key": "replica_port", "value": "56381"},
        {"key": "boundary_limit", "value": str(args.boundary_limit)},
        {"key": "boundary_window_ms", "value": str(args.boundary_window_ms)},
        {"key": "race_limit", "value": str(args.race_limit)},
        {"key": "race_window_ms", "value": str(args.race_window_ms)},
        {"key": "race_windows", "value": str(args.race_windows)},
        {"key": "race_threads", "value": str(args.race_threads)},
        {"key": "race_gap_ms", "value": "/".join(str(value) for value in args.race_gaps)},
        {"key": "replica_lag_ms", "value": "/".join(str(value) for value in args.replica_lags)},
        {"key": "replica_cycles", "value": str(args.replica_cycles)},
        {"key": "smoothness_window_ms", "value": str(args.smoothness_window_ms)},
        {"key": "smoothness_duration_ms", "value": str(args.smoothness_duration_ms)},
        {"key": "smoothness_bucket_ms", "value": str(args.smoothness_bucket_ms)},
        {"key": "throughput_duration_s", "value": str(args.throughput_duration)},
        {"key": "throughput_processes", "value": str(args.throughput_processes)},
        {"key": "throughput_pipeline_size", "value": str(args.throughput_pipeline_size)},
    ]
    write_csv(results_dir / "run_metadata.csv", ["key", "value"], rows)


def rolling_peak(timestamps, interval_ms):
    left = 0
    peak = 0
    for right, timestamp in enumerate(timestamps):
        while timestamps[left] <= timestamp - interval_ms:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def boundary_stream(load_shape, limit, window_ms):
    base = window_ms * 100
    if load_shape == "seam_aware":
        return [base + window_ms - 10] * limit + [base + window_ms + 10] * limit
    spacing = window_ms / float(limit)
    return [int(base + index * spacing) for index in range(limit * 2)]


def run_boundary(args, results_dir):
    client = redis_connect(PRIMARY_URL)
    rows = []
    for load_shape in ("seam_aware", "uniform"):
        offered = boundary_stream(load_shape, args.boundary_limit, args.boundary_window_ms)
        for name, limiter_type in (("fixed_window", FixedWindow), ("sliding_counter", SlidingWindowCounter)):
            client.flushdb()
            limiter = limiter_type(
                client,
                args.boundary_limit,
                args.boundary_window_ms,
                prefix="boundary:%s:%s" % (load_shape, name),
            )
            admitted = []
            for timestamp in offered:
                allowed, _, _ = limiter.allow("client", timestamp)
                if allowed:
                    admitted.append(timestamp)
            peak = rolling_peak(admitted, args.boundary_window_ms)
            peak_two_seconds = rolling_peak(admitted, 2000)
            rows.append(
                {
                    "load_shape": load_shape,
                    "limiter": name,
                    "offered": len(offered),
                    "admitted": len(admitted),
                    "peak_rolling_window_admits": peak,
                    "peak_rolling_2s_admits": peak_two_seconds,
                    "overshoot_pct": round(100.0 * max(0, peak - args.boundary_limit) / args.boundary_limit, 3),
                }
            )
    write_csv(
        results_dir / "boundary.csv",
        ["load_shape", "limiter", "offered", "admitted", "peak_rolling_window_admits", "peak_rolling_2s_admits", "overshoot_pct"],
        rows,
    )
    return rows


def run_race_once(args, strategy, gap_ms):
    client = redis_connect(PRIMARY_URL)
    client.flushdb()
    prefix = "race:%s:%s" % (strategy, gap_ms)
    if strategy == "naive_two_command":
        limiter = FixedWindowTwoKeyNaive(client, args.race_limit, args.race_window_ms, gap_ms=gap_ms, prefix=prefix)
    else:
        limiter = FixedWindowLua(client, args.race_limit, args.race_window_ms, prefix=prefix)
    admits = AtomicCounter()
    attempts = AtomicCounter()

    for cycle in range(args.race_windows):
        key = "hot:%s" % cycle
        timestamp_ms = int(time.monotonic() * 1000)
        client.set("%s:%s:count" % (prefix, key), args.race_limit, px=args.race_window_ms + 1000)
        client.set("%s:%s:reset" % (prefix, key), timestamp_ms - 1, px=args.race_window_ms + 1000)
        barrier = threading.Barrier(args.race_threads + 1)

        def collide():
            barrier.wait()
            allowed, _, _ = limiter.allow(key, timestamp_ms)
            attempts.increment()
            if allowed:
                admits.increment()

        threads = [threading.Thread(target=collide, name="race-client") for _ in range(args.race_threads)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        while True:
            allowed, _, _ = limiter.allow(key, timestamp_ms)
            attempts.increment()
            if not allowed:
                break
            admits.increment()
    budget = args.race_limit * args.race_windows
    admitted = admits.get()
    return {
        "strategy": strategy,
        "gap_ms": gap_ms,
        "threads": args.race_threads,
        "attempts": attempts.get(),
        "admitted": admitted,
        "allowed_budget": budget,
        "admitted_vs_budget_pct": round(100.0 * admitted / budget, 3),
        "overshoot": max(0, admitted - budget),
        "overshoot_pct": round(100.0 * max(0, admitted - budget) / budget, 3),
    }


def run_race(args, results_dir):
    sweep = []
    for gap_ms in args.race_gaps:
        for strategy in ("naive_two_command", "atomic_lua"):
            print("race: strategy=%s gap=%sms" % (strategy, gap_ms), flush=True)
            sweep.append(run_race_once(args, strategy, gap_ms))
    largest_gap = max(args.race_gaps)
    comparison = [row for row in sweep if row["gap_ms"] == largest_gap]
    fields = ["strategy", "gap_ms", "threads", "attempts", "admitted", "allowed_budget", "admitted_vs_budget_pct", "overshoot", "overshoot_pct"]
    write_csv(results_dir / "race_gap_sweep.csv", fields, sweep)
    write_csv(results_dir / "race.csv", fields, comparison)
    return sweep


def run_replica_case(args, strategy, lag_ms):
    primary = redis_connect(PRIMARY_URL)
    replica = redis_connect(REPLICA_URL)
    contradictions = 0
    decisions = 0
    rejected = 0
    for cycle in range(args.replica_cycles):
        attach_replica(primary, replica)
        primary.flushdb()
        limiter = FixedWindowLua(primary, args.replica_limit, 1000, prefix="replica-bench")
        current_ms = int(time.monotonic() * 1000)
        primary.set("replica-bench:client:count", args.replica_limit, px=10000)
        primary.set("replica-bench:client:reset", current_ms - 1, px=10000)
        wait_for_replica(primary, replica)
        replica.execute_command("REPLICAOF", "NO", "ONE")
        detached_at = time.monotonic()
        reattached = False
        requests_per_cycle = max(1, int(args.replica_observation_ms / args.replica_interval_ms))
        for _ in range(requests_per_cycle):
            elapsed_ms = (time.monotonic() - detached_at) * 1000.0
            if not reattached and elapsed_ms >= lag_ms:
                attach_replica(primary, replica)
                reattached = True
            timestamp_ms = int(time.monotonic() * 1000)
            if strategy == "decide_from_replica":
                stale_count = int(replica.get("replica-bench:client:count") or 0)
                response_rejected = stale_count >= args.replica_limit
                _, remaining, _ = limiter.allow("client", timestamp_ms)
            else:
                allowed, remaining, _ = limiter.allow("client", timestamp_ms)
                response_rejected = not allowed
            decisions += 1
            if response_rejected:
                rejected += 1
                if remaining >= args.replica_full_threshold:
                    contradictions += 1
            time.sleep(args.replica_interval_ms / 1000.0)
        if not reattached:
            replica.execute_command("REPLICAOF", REPLICA_HOST, 6379)
        wait_for_replica(primary, replica)
    return {
        "strategy": strategy,
        "injected_lag_ms": lag_ms,
        "decisions": decisions,
        "rejected": rejected,
        "contradictions": contradictions,
        "contradiction_pct": round(100.0 * contradictions / decisions, 3) if decisions else 0.0,
        "full_tank_threshold": args.replica_full_threshold,
    }


def run_replica(args, results_dir):
    rows = []
    for lag_ms in args.replica_lags:
        for strategy in ("decide_from_replica", "atomic_primary"):
            print("replica: strategy=%s lag=%sms" % (strategy, lag_ms), flush=True)
            rows.append(run_replica_case(args, strategy, lag_ms))
    write_csv(
        results_dir / "replica.csv",
        ["strategy", "injected_lag_ms", "decisions", "rejected", "contradictions", "contradiction_pct", "full_tank_threshold"],
        rows,
    )
    return rows


def offered_stream(duration_ms, bucket_ms, offered_per_bucket):
    timestamps = []
    for bucket_start in range(0, duration_ms, bucket_ms):
        for offset in range(offered_per_bucket):
            timestamps.append(bucket_start + int((offset + 0.5) * bucket_ms / offered_per_bucket))
    return timestamps


def run_limiter_stream(limiter, timestamps, bucket_ms, duration_ms):
    buckets = [0] * (duration_ms // bucket_ms)
    decisions = []
    for timestamp in timestamps:
        allowed, _, _ = limiter.allow("client", timestamp)
        decisions.append(allowed)
        if allowed:
            buckets[timestamp // bucket_ms] += 1
    return buckets, decisions


def run_smoothness(args, results_dir):
    client = redis_connect(PRIMARY_URL)
    timestamps = offered_stream(args.smoothness_duration_ms, args.smoothness_bucket_ms, args.smoothness_offered_per_bucket)
    series = []
    for name, limiter_type in (("fixed_window", FixedWindow), ("sliding_counter", SlidingWindowCounter)):
        client.flushdb()
        limiter = limiter_type(client, args.smoothness_limit, args.smoothness_window_ms, prefix="smooth:%s" % name)
        buckets, _ = run_limiter_stream(limiter, timestamps, args.smoothness_bucket_ms, args.smoothness_duration_ms)
        for index, admits in enumerate(buckets):
            series.append({"t_ms": index * args.smoothness_bucket_ms, "limiter": name, "admits": admits})
    client.flushdb()
    counter = SlidingWindowCounter(client, args.smoothness_limit, args.smoothness_window_ms, prefix="accuracy:counter")
    exact = SlidingWindowLog(client, args.smoothness_limit, args.smoothness_window_ms, prefix="accuracy:log")
    disagreements = 0
    counter_admits = 0
    log_admits = 0
    for timestamp in timestamps:
        counter_allowed, _, _ = counter.allow("client", timestamp)
        log_allowed, _, _ = exact.allow("client", timestamp)
        counter_admits += int(counter_allowed)
        log_admits += int(log_allowed)
        disagreements += int(counter_allowed != log_allowed)
    accuracy = [{
        "requests": len(timestamps),
        "counter_admits": counter_admits,
        "exact_log_admits": log_admits,
        "disagreements": disagreements,
        "disagreement_pct": round(100.0 * disagreements / len(timestamps), 3),
    }]
    write_csv(results_dir / "smoothness_timeseries.csv", ["t_ms", "limiter", "admits"], series)
    write_csv(results_dir / "sliding_accuracy.csv", ["requests", "counter_admits", "exact_log_admits", "disagreements", "disagreement_pct"], accuracy)
    return {"accuracy": accuracy, "series_rows": len(series)}


def throughput_worker(url, key_names, limit, window_ms, pipeline_size, duration, start_event, output):
    try:
        client = redis_connect(url)
        script_sha = client.script_load(FIXED_WINDOW_LUA)
        start_event.wait()
        started = time.monotonic()
        deadline = started + duration
        decisions = 0
        cursor = 0
        while time.monotonic() < deadline:
            timestamp_ms = int(time.monotonic() * 1000)
            pipeline = client.pipeline(transaction=False)
            for _ in range(pipeline_size):
                key = key_names[cursor % len(key_names)]
                cursor += 1
                pipeline.evalsha(
                    script_sha,
                    2,
                    "%s:count" % key,
                    "%s:reset" % key,
                    timestamp_ms,
                    window_ms,
                    limit,
                )
            pipeline.execute()
            decisions += pipeline_size
        output.put((decisions, time.monotonic() - started, None))
    except Exception as exc:
        output.put((0, 0.0, repr(exc)))


def shard_keys(shards, count):
    keys = [[] for _ in range(shards)]
    candidate = 0
    while any(len(items) < count for items in keys):
        key = "throughput:key:%s" % candidate
        shard = zlib.crc32(key.encode("utf-8")) % shards
        if len(keys[shard]) < count:
            keys[shard].append(key)
        candidate += 1
    return keys


def run_throughput_case(args, shards, distribution):
    urls = [PRIMARY_URL, REPLICA_URL][:shards]
    for url in urls:
        redis_connect(url).flushdb()
    keys_by_shard = shard_keys(shards, args.throughput_keys_per_shard)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    output = context.Queue()
    processes = []
    for process_id in range(args.throughput_processes):
        shard = process_id % shards if distribution == "spread" else 0
        keys = keys_by_shard[shard] if distribution == "spread" else ["throughput:hot"]
        process = context.Process(
            target=throughput_worker,
            args=(urls[shard], keys, 1000000000, 60000, args.throughput_pipeline_size, args.throughput_duration, start_event, output),
        )
        process.start()
        processes.append(process)
    start_event.set()
    totals = []
    for _ in processes:
        try:
            totals.append(output.get(timeout=args.throughput_duration + 15.0))
        except queue.Empty:
            totals.append((0, 0.0, "worker timeout"))
    for process in processes:
        process.join(timeout=2.0)
    errors = [error for _, _, error in totals if error]
    if errors:
        raise RuntimeError("throughput worker failed: %s" % errors[0])
    decisions = sum(value for value, _, _ in totals)
    wall_time = max(elapsed for _, elapsed, _ in totals)
    return {
        "shards": shards,
        "key_distribution": distribution,
        "processes": args.throughput_processes,
        "pipeline_size": args.throughput_pipeline_size,
        "decisions": decisions,
        "wall_time_s": round(wall_time, 3),
        "decisions_per_sec": round(decisions / wall_time, 3),
    }


def run_throughput(args, results_dir):
    primary = redis_connect(PRIMARY_URL)
    replica = redis_connect(REPLICA_URL)
    rows = []
    try:
        for distribution in ("spread", "hot"):
            print("throughput: shards=1 distribution=%s" % distribution, flush=True)
            rows.append(run_throughput_case(args, 1, distribution))
        replica.execute_command("REPLICAOF", "NO", "ONE")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and replica.info("replication").get("role") != "master":
            time.sleep(0.02)
        for distribution in ("spread", "hot"):
            print("throughput: shards=2 distribution=%s" % distribution, flush=True)
            rows.append(run_throughput_case(args, 2, distribution))
    finally:
        attach_replica(primary, replica)
    write_csv(
        results_dir / "throughput.csv",
        ["shards", "key_distribution", "processes", "pipeline_size", "decisions", "wall_time_s", "decisions_per_sec"],
        rows,
    )
    return rows


def add_common_arguments(parser):
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--boundary-limit", type=int, default=100)
    parser.add_argument("--boundary-window-ms", type=int, default=2000)
    parser.add_argument("--race-limit", type=int, default=100)
    parser.add_argument("--race-window-ms", type=int, default=200)
    parser.add_argument("--race-windows", type=int, default=30)
    parser.add_argument("--race-threads", type=int, default=8)
    parser.add_argument("--race-gaps", type=int, nargs="+", default=[0, 5, 10, 25])
    parser.add_argument("--replica-limit", type=int, default=100)
    parser.add_argument("--replica-lags", type=int, nargs="+", default=[0, 10, 25, 50])
    parser.add_argument("--replica-cycles", type=int, default=3)
    parser.add_argument("--replica-observation-ms", type=int, default=80)
    parser.add_argument("--replica-interval-ms", type=float, default=2.0)
    parser.add_argument("--replica-full-threshold", type=int, default=80)
    parser.add_argument("--smoothness-limit", type=int, default=50)
    parser.add_argument("--smoothness-window-ms", type=int, default=1000)
    parser.add_argument("--smoothness-duration-ms", type=int, default=30000)
    parser.add_argument("--smoothness-bucket-ms", type=int, default=100)
    parser.add_argument("--smoothness-offered-per-bucket", type=int, default=8)
    parser.add_argument("--throughput-duration", type=float, default=3.0)
    parser.add_argument("--throughput-processes", type=int, default=min(16, max(4, (os.cpu_count() or 4) * 2)))
    parser.add_argument("--throughput-pipeline-size", type=int, default=256)
    parser.add_argument("--throughput-keys-per-shard", type=int, default=1000)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="experiment", required=True)
    for name in ("boundary", "race", "replica", "smoothness", "throughput", "all"):
        subparser = subparsers.add_parser(name)
        add_common_arguments(subparser)
    return parser.parse_args()


def main():
    multiprocessing.freeze_support()
    args = parse_args()
    results_dir = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    wait_for_services()
    write_metadata(results_dir, args)
    outputs = {}
    if args.experiment in ("boundary", "all"):
        outputs["boundary"] = run_boundary(args, results_dir)
    if args.experiment in ("race", "all"):
        outputs["race"] = run_race(args, results_dir)
    if args.experiment in ("replica", "all"):
        outputs["replica"] = run_replica(args, results_dir)
    if args.experiment in ("smoothness", "all"):
        outputs["smoothness"] = run_smoothness(args, results_dir)
    if args.experiment in ("throughput", "all"):
        outputs["throughput"] = run_throughput(args, results_dir)
    print("results: %s" % results_dir)
    for name, rows in outputs.items():
        print("%s: %s" % (name, rows))


if __name__ == "__main__":
    main()
