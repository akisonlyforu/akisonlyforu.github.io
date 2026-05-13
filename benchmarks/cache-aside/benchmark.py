#!/usr/bin/env python3
"""Reproducible PostgreSQL + Redis experiments for the cache-aside post."""

import argparse
import bisect
import csv
import heapq
import math
import os
import platform
import queue
import random
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import redis


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"
PG_DSN = os.environ.get(
    "CACHE_BENCH_PG_DSN",
    "dbname=cache_bench user=cache_bench password=cache_bench host=127.0.0.1 port=55432",
)
REDIS_URL = os.environ.get("CACHE_BENCH_REDIS_URL", "redis://127.0.0.1:56379/0")


def db_connect():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def redis_connect():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def wait_for_services(timeout=60.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            conn = db_connect()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM users")
                if cur.fetchone()[0] != 100000:
                    raise RuntimeError("users seed is incomplete")
            conn.close()
            redis_connect().ping()
            return
        except Exception as exc:  # services may still be starting
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("PostgreSQL/Redis did not become ready: %s" % last_error)


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


class BlockingPgPool:
    def __init__(self, size):
        self.connections = queue.Queue(maxsize=size)
        for _ in range(size):
            self.connections.put(db_connect())

    @contextmanager
    def connection(self):
        conn = self.connections.get()
        try:
            yield conn
        finally:
            self.connections.put(conn)

    def close(self):
        while not self.connections.empty():
            self.connections.get_nowait().close()


class DelayedDeleteQueue:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.items = []
        self.condition = threading.Condition()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name="delayed-delete")
        self.thread.start()

    def schedule(self, key, delay_seconds):
        with self.condition:
            heapq.heappush(self.items, (time.monotonic() + delay_seconds, key))
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                while not self.items and not self.stopping:
                    self.condition.wait()
                if self.stopping and not self.items:
                    return
                deadline, key = self.items[0]
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.condition.wait(timeout=remaining)
                    continue
                heapq.heappop(self.items)
            self.redis.delete(key)

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.thread.join()


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


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
    rows = [
        {"key": "run_at_utc", "value": datetime.now(timezone.utc).isoformat()},
        {"key": "platform", "value": platform.platform()},
        {"key": "python", "value": platform.python_version()},
        {"key": "docker", "value": command_version(["docker", "--version"])},
        {"key": "docker_compose", "value": command_version(["docker", "compose", "version"])},
        {"key": "postgres_image", "value": "postgres:16"},
        {"key": "redis_image", "value": "redis:7"},
        {"key": "seed_rows", "value": "100000"},
        {"key": "race_sampler_interval_ms", "value": str(args.race_sample_ms)},
        {"key": "race_duration_s", "value": str(args.race_duration)},
        {"key": "race_writer_interval_ms", "value": str(args.race_writer_interval_ms)},
        {"key": "race_expiry_to_write_gap_ms", "value": str(args.race_expiry_to_write_gap_ms)},
        {"key": "stampede_reader_count", "value": str(args.stampede_readers)},
        {"key": "stampede_loader_delay_ms", "value": str(args.stampede_loader_ms)},
        {"key": "jitter_key_count", "value": str(args.jitter_keys)},
        {"key": "jitter_base_ttl_ms", "value": str(args.jitter_ttl_ms)},
        {"key": "jitter_percent", "value": "10"},
        {"key": "baseline_request_count", "value": str(args.baseline_requests)},
        {"key": "baseline_zipf_alpha", "value": str(args.baseline_alpha)},
    ]
    write_csv(results_dir / "run_metadata.csv", ["key", "value"], rows)


def reset_hot_user():
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET version = 0, payload = 'value-0', updated_at = now() WHERE id = 1"
        )
    conn.close()


RACE_CAS_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PSETEX', KEYS[2], ARGV[2], ARGV[3])
  return 1
end
return 0
"""

RACE_BUMP_LUA = """
redis.call('INCR', KEYS[1])
redis.call('DEL', KEYS[2])
return 1
"""


def run_race_once(
    strategy,
    build_delay_ms,
    duration,
    reader_count,
    writer_count,
    sample_interval_ms,
    writer_interval_ms,
    expiry_to_write_gap_ms,
):
    r = redis_connect()
    r.flushdb()
    reset_hot_user()
    cache_key = "race:user:1"
    version_key = "race:user:1:token"
    r.set(version_key, 0)
    r.psetex(cache_key, 1000, "0")
    cas_set = r.register_script(RACE_CAS_LUA)
    bump_and_delete = r.register_script(RACE_BUMP_LUA)
    delayed_deletes = DelayedDeleteQueue(r) if strategy == "double_delete" else None
    stop = threading.Event()
    start_gate = threading.Barrier(reader_count + writer_count + 2)
    reader_ops = AtomicCounter()
    writer_ops = AtomicCounter()
    ttl_ms = 60 if strategy == "short_ttl" else 1000
    build_delay = build_delay_ms / 1000.0
    second_delete_delay = max(0.010, build_delay * 1.5 + 0.005)
    writer_interval = writer_interval_ms / 1000.0
    expiry_to_write_gap = expiry_to_write_gap_ms / 1000.0

    def reader():
        conn = db_connect()
        start_gate.wait()
        try:
            while not stop.is_set():
                if r.get(cache_key) is None:
                    token = r.get(version_key) if strategy == "version_cas" else None
                    with conn.cursor() as cur:
                        cur.execute("SELECT version FROM users WHERE id = 1")
                        value = str(cur.fetchone()[0])
                    if build_delay:
                        time.sleep(build_delay)
                    if strategy == "version_cas":
                        cas_set(keys=[version_key, cache_key], args=[token, ttl_ms, value])
                    else:
                        r.psetex(cache_key, ttl_ms, value)
                reader_ops.increment()
        finally:
            conn.close()

    def writer():
        conn = db_connect()
        start_gate.wait()
        try:
            while not stop.is_set():
                # Reproduce the unlucky overlap: a key expires just before a write.
                # Readers can now fetch V1 while the writer is about to commit V2.
                r.delete(cache_key)
                if stop.wait(expiry_to_write_gap):
                    break
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET version = version + 1,
                            payload = 'value-' || (version + 1),
                            updated_at = now()
                        WHERE id = 1
                        """
                    )
                if strategy == "version_cas":
                    bump_and_delete(keys=[version_key, cache_key])
                else:
                    r.delete(cache_key)
                    if delayed_deletes:
                        delayed_deletes.schedule(cache_key, second_delete_delay)
                writer_ops.increment()
                if stop.wait(writer_interval):
                    break
        finally:
            conn.close()

    sampler_result = {}

    def sampler():
        conn = db_connect()  # autocommit: every SELECT sees committed state
        interval = sample_interval_ms / 1000.0
        observed = 0.0
        stale = 0.0
        samples = 0
        stale_samples = 0
        previous_time = time.monotonic()
        previous_state = None
        start_gate.wait()
        try:
            while not stop.is_set():
                loop_started = time.monotonic()
                elapsed = loop_started - previous_time
                if previous_state is not None:
                    observed += elapsed
                    if previous_state:
                        stale += elapsed
                with conn.cursor() as cur:
                    cur.execute("SELECT version FROM users WHERE id = 1")
                    db_before = int(cur.fetchone()[0])
                    cache_value = r.get(cache_key)
                    cur.execute("SELECT version FROM users WHERE id = 1")
                    db_after = int(cur.fetchone()[0])
                state = None
                if db_before == db_after and cache_value is not None:
                    state = int(cache_value) != db_after
                elif db_before == db_after:
                    state = False
                if state is not None:
                    samples += 1
                    stale_samples += int(state)
                previous_state = state
                previous_time = loop_started
                sleep_for = interval - (time.monotonic() - loop_started)
                if sleep_for > 0:
                    stop.wait(sleep_for)
        finally:
            now = time.monotonic()
            if previous_state is not None:
                elapsed = now - previous_time
                observed += elapsed
                if previous_state:
                    stale += elapsed
            sampler_result.update(
                observed_seconds=observed,
                stale_seconds=stale,
                samples=samples,
                stale_samples=stale_samples,
            )
            conn.close()

    threads = []
    for _ in range(reader_count):
        threads.append(threading.Thread(target=reader, name="race-reader"))
    for _ in range(writer_count):
        threads.append(threading.Thread(target=writer, name="race-writer"))
    threads.append(threading.Thread(target=sampler, name="race-sampler"))
    for thread in threads:
        thread.start()
    start_gate.wait()
    started = time.monotonic()
    time.sleep(duration)
    stop.set()
    for thread in threads:
        thread.join()
    if delayed_deletes:
        delayed_deletes.close()
    wall = time.monotonic() - started
    observed = sampler_result.get("observed_seconds", 0.0)
    stale = sampler_result.get("stale_seconds", 0.0)
    return {
        "strategy": strategy,
        "build_delay_ms": build_delay_ms,
        "stale_time_pct": round(100.0 * stale / observed, 3) if observed else 0.0,
        "stale_time_ms": round(stale * 1000.0, 3),
        "observed_time_ms": round(observed * 1000.0, 3),
        "valid_samples": sampler_result.get("samples", 0),
        "stale_samples": sampler_result.get("stale_samples", 0),
        "reader_ops": reader_ops.get(),
        "writer_ops": writer_ops.get(),
        "wall_time_ms": round(wall * 1000.0, 3),
    }


def run_race(args, results_dir):
    rows = []
    for delay in args.race_delays:
        for strategy in ("naive", "short_ttl", "double_delete", "version_cas"):
            print("race: strategy=%s delay=%sms" % (strategy, delay), flush=True)
            rows.append(
                run_race_once(
                    strategy,
                    delay,
                    args.race_duration,
                    args.race_readers,
                    args.race_writers,
                    args.race_sample_ms,
                    args.race_writer_interval_ms,
                    args.race_expiry_to_write_gap_ms,
                )
            )
    write_csv(
        results_dir / "race.csv",
        [
            "strategy",
            "build_delay_ms",
            "stale_time_pct",
            "stale_time_ms",
            "observed_time_ms",
            "valid_samples",
            "stale_samples",
            "reader_ops",
            "writer_ops",
            "wall_time_ms",
        ],
        rows,
    )
    return rows


LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def run_stampede_once(strategy, readers, loader_delay_ms):
    r = redis_connect()
    r.flushdb()
    cache_key = "stampede:user:1"
    lock_key = "stampede:user:1:lock"
    pool = BlockingPgPool(32)
    db_hits = AtomicCounter()
    barrier = threading.Barrier(readers + 1)
    release_lock = r.register_script(LOCK_RELEASE_LUA)

    def loader():
        db_hits.increment()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_sleep(%s), version FROM users WHERE id = 1",
                    (loader_delay_ms / 1000.0,),
                )
                return str(cur.fetchone()[1])

    def request():
        barrier.wait()
        started = time.perf_counter()
        value = r.get(cache_key)
        if value is None and strategy == "naive":
            value = loader()
            r.set(cache_key, value, ex=30)
        elif value is None:
            token = uuid.uuid4().hex
            if r.set(lock_key, token, nx=True, px=2000):
                try:
                    value = r.get(cache_key)
                    if value is None:
                        value = loader()
                        r.set(cache_key, value, ex=30)
                finally:
                    release_lock(keys=[lock_key], args=[token])
            else:
                deadline = time.monotonic() + 2.0
                while value is None and time.monotonic() < deadline:
                    time.sleep(0.001)
                    value = r.get(cache_key)
                if value is None:
                    value = loader()
                    r.set(cache_key, value, ex=30)
        return (time.perf_counter() - started) * 1000.0

    with ThreadPoolExecutor(max_workers=readers) as executor:
        futures = [executor.submit(request) for _ in range(readers)]
        r.psetex(cache_key, 200, "warm")
        time.sleep(0.240)
        wall_started = time.perf_counter()
        barrier.wait()
        latencies = [future.result() for future in futures]
        wall_ms = (time.perf_counter() - wall_started) * 1000.0
    pool.close()
    return {
        "strategy": strategy,
        "readers": readers,
        "db_hits": db_hits.get(),
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p99_ms": round(percentile(latencies, 0.99), 3),
        "max_ms": round(max(latencies), 3),
        "wall_time_ms": round(wall_ms, 3),
        "loader_delay_ms": loader_delay_ms,
    }


def run_stampede(args, results_dir):
    rows = []
    for strategy in ("naive", "single_flight"):
        print("stampede: strategy=%s readers=%s" % (strategy, args.stampede_readers), flush=True)
        rows.append(
            run_stampede_once(strategy, args.stampede_readers, args.stampede_loader_ms)
        )
    write_csv(
        results_dir / "stampede.csv",
        [
            "strategy",
            "readers",
            "db_hits",
            "p50_ms",
            "p99_ms",
            "max_ms",
            "wall_time_ms",
            "loader_delay_ms",
        ],
        rows,
    )
    return rows


def run_jitter_once(strategy, key_count, base_ttl_ms, duration, reader_count):
    r = redis_connect()
    r.flushdb()
    seed = random.Random(20260718 if strategy == "fixed" else 20260719)
    pipeline = r.pipeline(transaction=False)
    for user_id in range(1, key_count + 1):
        if strategy == "fixed":
            ttl_ms = base_ttl_ms
        else:
            ttl_ms = int(base_ttl_ms * seed.uniform(0.90, 1.10))
        pipeline.psetex("jitter:user:%s" % user_id, ttl_ms, "0")
    pipeline.execute()

    bucket_ms = 100
    bucket_count = int(math.ceil(duration * 1000.0 / bucket_ms))
    loader_buckets = [0 for _ in range(bucket_count)]
    bucket_lock = threading.Lock()
    operations = AtomicCounter()
    batch_cursor = AtomicCounter()
    stop = threading.Event()
    start_gate = threading.Barrier(reader_count + 1)
    pool = BlockingPgPool(min(32, reader_count))
    started_holder = {}
    batch_size = 100

    def reader(worker_id):
        start_gate.wait()
        while not stop.is_set():
            batch_end = batch_cursor.increment(batch_size)
            batch_start = batch_end - batch_size
            user_ids = [index % key_count + 1 for index in range(batch_start, batch_end)]
            keys = ["jitter:user:%s" % user_id for user_id in user_ids]
            values = r.mget(keys)
            missing_ids = [
                user_id for user_id, value in zip(user_ids, values) if value is None
            ]
            if missing_ids:
                elapsed_ms = (time.monotonic() - started_holder["started"]) * 1000.0
                bucket = int(elapsed_ms // bucket_ms)
                if 0 <= bucket < bucket_count:
                    with bucket_lock:
                        loader_buckets[bucket] += len(missing_ids)
                with pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id, version FROM users WHERE id = ANY(%s)",
                            (missing_ids,),
                        )
                        loaded = cur.fetchall()
                refill = r.pipeline(transaction=False)
                for user_id, value in loaded:
                    refill.psetex("jitter:user:%s" % user_id, 10000, value)
                refill.execute()
            operations.increment(len(user_ids))

    threads = [
        threading.Thread(target=reader, args=(worker_id,), name="jitter-reader")
        for worker_id in range(reader_count)
    ]
    for thread in threads:
        thread.start()
    started_holder["started"] = time.monotonic()
    start_gate.wait()
    time.sleep(duration)
    stop.set()
    for thread in threads:
        thread.join()
    pool.close()
    rows = []
    for index, calls in enumerate(loader_buckets):
        rows.append(
            {
                "strategy": strategy,
                "bucket_start_ms": index * bucket_ms,
                "bucket_end_ms": (index + 1) * bucket_ms,
                "db_loader_calls": calls,
            }
        )
    summary = {
        "strategy": strategy,
        "key_count": key_count,
        "base_ttl_ms": base_ttl_ms,
        "reader_ops": operations.get(),
        "total_db_loader_calls": sum(loader_buckets),
        "peak_db_loader_calls_per_100ms": max(loader_buckets),
    }
    return rows, summary


def run_jitter(args, results_dir):
    all_rows = []
    summaries = []
    for strategy in ("fixed", "jittered"):
        print("jitter: strategy=%s keys=%s" % (strategy, args.jitter_keys), flush=True)
        rows, summary = run_jitter_once(
            strategy,
            args.jitter_keys,
            args.jitter_ttl_ms,
            args.jitter_duration,
            args.jitter_readers,
        )
        all_rows.extend(rows)
        summaries.append(summary)
    write_csv(
        results_dir / "ttl_jitter_timeseries.csv",
        ["strategy", "bucket_start_ms", "bucket_end_ms", "db_loader_calls"],
        all_rows,
    )
    write_csv(
        results_dir / "ttl_jitter_summary.csv",
        [
            "strategy",
            "key_count",
            "base_ttl_ms",
            "reader_ops",
            "total_db_loader_calls",
            "peak_db_loader_calls_per_100ms",
        ],
        summaries,
    )
    return summaries


def zipf_cdf(size, alpha):
    weights = [1.0 / (rank ** alpha) for rank in range(1, size + 1)]
    total = sum(weights)
    cumulative = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)
    cumulative[-1] = 1.0
    return cumulative


def zipf_ids(count, size, alpha, seed):
    rng = random.Random(seed)
    cumulative = zipf_cdf(size, alpha)
    return [bisect.bisect_left(cumulative, rng.random()) + 1 for _ in range(count)]


def run_baseline_once(strategy, request_ids, concurrency):
    r = redis_connect()
    if strategy == "cached":
        r.flushdb()
    db_hits = AtomicCounter()
    cache_hits = AtomicCounter()
    chunks = [request_ids[index::concurrency] for index in range(concurrency)]

    def worker(ids):
        conn = db_connect()
        latencies = []
        try:
            for user_id in ids:
                started = time.perf_counter()
                value = None
                key = "baseline:user:%s" % user_id
                if strategy == "cached":
                    value = r.get(key)
                    if value is not None:
                        cache_hits.increment()
                if value is None:
                    db_hits.increment()
                    with conn.cursor() as cur:
                        cur.execute("SELECT payload FROM users WHERE id = %s", (user_id,))
                        value = cur.fetchone()[0]
                    if strategy == "cached":
                        r.set(key, value, ex=60)
                latencies.append((time.perf_counter() - started) * 1000.0)
            return latencies
        finally:
            conn.close()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        latency_groups = list(executor.map(worker, chunks))
    wall = time.perf_counter() - started
    latencies = [value for group in latency_groups for value in group]
    requests = len(request_ids)
    return {
        "strategy": strategy,
        "requests": requests,
        "concurrency": concurrency,
        "cache_hits": cache_hits.get(),
        "hit_rate_pct": round(100.0 * cache_hits.get() / requests, 3),
        "db_hits": db_hits.get(),
        "db_qps": round(db_hits.get() / wall, 3),
        "request_qps": round(requests / wall, 3),
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p99_ms": round(percentile(latencies, 0.99), 3),
        "wall_time_ms": round(wall * 1000.0, 3),
    }


def run_baseline(args, results_dir):
    request_ids = zipf_ids(
        args.baseline_requests,
        100000,
        args.baseline_alpha,
        seed=20260718,
    )
    rows = []
    for strategy in ("uncached", "cached"):
        print("baseline: strategy=%s requests=%s" % (strategy, len(request_ids)), flush=True)
        rows.append(run_baseline_once(strategy, request_ids, args.baseline_concurrency))
    write_csv(
        results_dir / "baseline.csv",
        [
            "strategy",
            "requests",
            "concurrency",
            "cache_hits",
            "hit_rate_pct",
            "db_hits",
            "db_qps",
            "request_qps",
            "p50_ms",
            "p99_ms",
            "wall_time_ms",
        ],
        rows,
    )
    return rows


def add_common_arguments(parser):
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--race-duration", type=float, default=3.0)
    parser.add_argument("--race-readers", type=int, default=16)
    parser.add_argument("--race-writers", type=int, default=1)
    parser.add_argument("--race-sample-ms", type=float, default=5.0)
    parser.add_argument("--race-writer-interval-ms", type=float, default=250.0)
    parser.add_argument("--race-expiry-to-write-gap-ms", type=float, default=2.0)
    parser.add_argument("--race-delays", type=int, nargs="+", default=[0, 5, 20])
    parser.add_argument("--stampede-readers", type=int, default=500)
    parser.add_argument("--stampede-loader-ms", type=float, default=10.0)
    parser.add_argument("--jitter-keys", type=int, default=5000)
    parser.add_argument("--jitter-ttl-ms", type=int, default=2000)
    parser.add_argument("--jitter-duration", type=float, default=3.2)
    parser.add_argument("--jitter-readers", type=int, default=24)
    parser.add_argument("--baseline-requests", type=int, default=20000)
    parser.add_argument("--baseline-concurrency", type=int, default=32)
    parser.add_argument("--baseline-alpha", type=float, default=1.2)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="experiment", required=True)
    for name in ("race", "stampede", "jitter", "baseline", "all"):
        subparser = subparsers.add_parser(name)
        add_common_arguments(subparser)
    return parser.parse_args()


def main():
    args = parse_args()
    results_dir = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    wait_for_services()
    write_metadata(results_dir, args)
    outputs = {}
    if args.experiment in ("race", "all"):
        outputs["race"] = run_race(args, results_dir)
    if args.experiment in ("stampede", "all"):
        outputs["stampede"] = run_stampede(args, results_dir)
    if args.experiment in ("jitter", "all"):
        outputs["jitter"] = run_jitter(args, results_dir)
    if args.experiment in ("baseline", "all"):
        outputs["baseline"] = run_baseline(args, results_dir)
    print("results: %s" % results_dir)
    for name, rows in outputs.items():
        print("%s: %s" % (name, rows))


if __name__ == "__main__":
    main()
