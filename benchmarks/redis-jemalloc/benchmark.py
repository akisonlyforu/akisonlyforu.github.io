#!/usr/bin/env python3
"""Compare Redis built with jemalloc and libc under identical keyspace churn."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import redis


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"
MARKER_KEY = "redis_jemalloc_bench_marker"
MARKER_VALUE = "redis_jemalloc_bench_v1"
KEY_PREFIX = "redis-je-bench:"

URL_JEMALLOC = os.environ.get("REDIS_JE_URL", "redis://127.0.0.1:56380/0")
URL_LIBC = os.environ.get("REDIS_LIBC_URL", "redis://127.0.0.1:56381/0")
CONTAINER_JEMALLOC = "redis-jemalloc-je"
CONTAINER_LIBC = "redis-jemalloc-libc"

VALUE_SIZES = (64, 128, 256, 512, 1024)
TIMELINE_FIELDS = (
    "phase",
    "used_memory",
    "used_memory_human",
    "used_memory_rss",
    "used_memory_rss_human",
    "mem_fragmentation_ratio",
    "mem_allocator",
    "maxmemory",
    "maxmemory_human",
    "maxmemory_policy",
    "evicted_keys",
    "allocator_allocated",
    "allocator_active",
    "allocator_resident",
    "allocator_frag_ratio",
    "allocator_frag_bytes",
    "number_of_keys",
    "process_rss",
    "cgroup_memory",
    "cgroup_limit",
    "time_sec",
)
COMPARISON_FIELDS = tuple(field for field in TIMELINE_FIELDS if field != "time_sec")


@dataclass(frozen=True)
class ChurnRound:
    overwrites: tuple[tuple[str, int], ...]
    deletes: tuple[str, ...]
    inserts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class WorkloadPlan:
    initial: tuple[tuple[str, int], ...]
    rounds: tuple[ChurnRound, ...]
    fingerprint: str
    operation_count: int


def redis_connect(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True)


def wait_for_redis(url: str, timeout: float = 90.0) -> redis.Redis:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = redis_connect(url)
            client.ping()
            return client
        except Exception as exc:  # Redis may still be starting.
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Redis at {url} did not become ready: {last_error}")


def command_output(command: Sequence[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def verify_and_reset(client: redis.Redis, reset: bool) -> None:
    if not reset:
        raise RuntimeError("the benchmark is destructive; rerun with --reset")

    size = client.dbsize()
    marker = client.get(MARKER_KEY) if size else None
    if size and marker != MARKER_VALUE:
        raise RuntimeError(
            f"refusing destructive reset: database has {size} keys and lacks {MARKER_KEY}"
        )

    client.flushall()
    client.set(MARKER_KEY, MARKER_VALUE)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def raw_info_memory(client: redis.Redis) -> str:
    """Read INFO memory before redis-py applies its response parser."""
    connection = client.connection_pool.get_connection()
    try:
        connection.send_command("INFO", "memory")
        response = connection.read_response()
    finally:
        client.connection_pool.release(connection)
    if isinstance(response, bytes):
        response = response.decode("utf-8")
    return str(response).replace("\r\n", "\n")


def write_raw_info(path: Path, raw_info: str) -> None:
    path.write_text(raw_info, encoding="utf-8")


def container_memory(container_name: str) -> tuple[int, int, int]:
    """Return process RSS, cgroup current, and configured cgroup limit in bytes."""
    script = (
        "grep '^VmRSS:' /proc/1/status; "
        "if [ -f /sys/fs/cgroup/memory.current ]; then "
        "printf 'CgroupCurrent: '; cat /sys/fs/cgroup/memory.current; "
        "elif [ -f /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then "
        "printf 'CgroupCurrent: '; cat /sys/fs/cgroup/memory/memory.usage_in_bytes; fi"
    )
    try:
        output = subprocess.check_output(
            ["docker", "exec", container_name, "sh", "-c", script],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        output = ""

    process_rss = 0
    cgroup_current = 0
    for line in output.splitlines():
        if line.startswith("VmRSS:"):
            process_rss = int(line.split()[1]) * 1024
        elif line.startswith("CgroupCurrent:"):
            cgroup_current = int(line.split(":", 1)[1].strip())

    try:
        cgroup_limit = int(
            subprocess.check_output(
                ["docker", "inspect", container_name, "--format={{.HostConfig.Memory}}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
        )
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        cgroup_limit = 0

    return process_rss, cgroup_current, cgroup_limit


def data_key_count(client: redis.Redis) -> int:
    marker_present = 1 if client.exists(MARKER_KEY) else 0
    return max(0, client.dbsize() - marker_present)


def sample_metrics(client: redis.Redis, container_name: str, phase: str) -> dict:
    memory = client.info("memory")
    stats = client.info("stats")
    process_rss, cgroup_current, cgroup_limit = container_memory(container_name)
    row = {
        "phase": phase,
        "used_memory": memory.get("used_memory", 0),
        "used_memory_human": memory.get("used_memory_human", "unknown"),
        "used_memory_rss": memory.get("used_memory_rss", 0),
        "used_memory_rss_human": memory.get("used_memory_rss_human", "unknown"),
        "mem_fragmentation_ratio": memory.get("mem_fragmentation_ratio", 0),
        "mem_allocator": memory.get("mem_allocator", "unknown"),
        "maxmemory": memory.get("maxmemory", 0),
        "maxmemory_human": memory.get("maxmemory_human", "unknown"),
        "maxmemory_policy": memory.get("maxmemory_policy", "unknown"),
        "evicted_keys": stats.get("evicted_keys", 0),
        "allocator_allocated": memory.get("allocator_allocated", ""),
        "allocator_active": memory.get("allocator_active", ""),
        "allocator_resident": memory.get("allocator_resident", ""),
        "allocator_frag_ratio": memory.get("allocator_frag_ratio", ""),
        "allocator_frag_bytes": memory.get("allocator_frag_bytes", ""),
        "number_of_keys": data_key_count(client),
        "process_rss": process_rss,
        "cgroup_memory": cgroup_current,
        "cgroup_limit": cgroup_limit,
    }
    return row


class TimelineSampler:
    def __init__(
        self,
        client: redis.Redis,
        container_name: str,
        interval: float,
    ) -> None:
        self.client = client
        self.container_name = container_name
        self.interval = interval
        self.records: list[dict] = []
        self.phase = "init"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.start_time = 0.0

    def start(self) -> None:
        self.start_time = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(2.0, self.interval * 4))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                row = sample_metrics(self.client, self.container_name, self.phase)
            except redis.RedisError:
                break
            row["time_sec"] = f"{started - self.start_time:.3f}"
            self.records.append(row)
            remaining = self.interval - (time.monotonic() - started)
            if remaining > 0:
                self.stop_event.wait(remaining)


def build_plan(keys: int, rounds: int, churn_fraction: float, seed: int) -> WorkloadPlan:
    if keys < 1:
        raise ValueError("--keys must be positive")
    if not 0 < churn_fraction < 1:
        raise ValueError("--churn-fraction must be between 0 and 1")

    rng = random.Random(seed)
    active = [f"{KEY_PREFIX}base:{index}" for index in range(keys)]
    sizes_by_key: dict[str, int] = {}
    hasher = hashlib.sha256()
    operation_count = 0

    initial: list[tuple[str, int]] = []
    for key in active:
        size = rng.choice(VALUE_SIZES)
        sizes_by_key[key] = size
        initial.append((key, size))
        hasher.update(f"SET\0{key}\0{size}\n".encode())
        operation_count += 1

    churn_rounds: list[ChurnRound] = []
    count = max(1, int(keys * churn_fraction))
    for round_index in range(rounds):
        overwrite_keys = rng.sample(active, count)
        overwrites: list[tuple[str, int]] = []
        for key in overwrite_keys:
            choices = [size for size in VALUE_SIZES if size != sizes_by_key[key]]
            size = rng.choice(choices)
            sizes_by_key[key] = size
            overwrites.append((key, size))
            hasher.update(f"SET\0{key}\0{size}\n".encode())

        delete_keys = rng.sample(active, count)
        delete_set = set(delete_keys)
        for key in delete_keys:
            hasher.update(f"DEL\0{key}\n".encode())
            sizes_by_key.pop(key, None)

        active = [key for key in active if key not in delete_set]
        inserts: list[tuple[str, int]] = []
        for insert_index in range(count):
            key = f"{KEY_PREFIX}fresh:{round_index}:{insert_index}"
            size = rng.choice(VALUE_SIZES)
            active.append(key)
            sizes_by_key[key] = size
            inserts.append((key, size))
            hasher.update(f"SET\0{key}\0{size}\n".encode())

        operation_count += len(overwrites) + len(delete_keys) + len(inserts)
        churn_rounds.append(
            ChurnRound(tuple(overwrites), tuple(delete_keys), tuple(inserts))
        )

    if len(active) != keys:
        raise AssertionError("workload plan did not preserve the key population")

    return WorkloadPlan(
        initial=tuple(initial),
        rounds=tuple(churn_rounds),
        fingerprint=hasher.hexdigest(),
        operation_count=operation_count,
    )


def execute_sets(
    client: redis.Redis,
    operations: Sequence[tuple[str, int]],
    values: dict[int, str],
    batch_size: int,
) -> None:
    for offset in range(0, len(operations), batch_size):
        pipe = client.pipeline(transaction=False)
        for key, size in operations[offset : offset + batch_size]:
            pipe.set(key, values[size])
        pipe.execute()


def execute_deletes(client: redis.Redis, keys: Sequence[str], batch_size: int) -> None:
    for offset in range(0, len(keys), batch_size):
        client.delete(*keys[offset : offset + batch_size])


def settle(client: redis.Redis, seconds: float) -> None:
    client.info("memory")  # Unrecorded warm-up read.
    time.sleep(seconds)


def run_workload(
    client: redis.Redis,
    url: str,
    container_name: str,
    allocator_name: str,
    plan: WorkloadPlan,
    args: argparse.Namespace,
    include_churn: bool,
) -> dict:
    print(f"\n--- {allocator_name}: {url} ---", flush=True)
    verify_and_reset(client, args.reset)
    client.config_set("activedefrag", "no")

    allocator = client.info("memory").get("mem_allocator", "unknown")
    print(f"allocator={allocator}; loading {args.keys:,} keys", flush=True)
    sampler = TimelineSampler(client, container_name, args.sample_interval)
    values = {size: "x" * size for size in VALUE_SIZES}
    sampler.start()

    try:
        sampler.phase = "loading"
        execute_sets(client, plan.initial, values, args.batch_size)
        sampler.phase = "loaded_settle"
        settle(client, args.settle_seconds)

        if include_churn:
            for round_number, churn_round in enumerate(plan.rounds, start=1):
                sampler.phase = f"churn_{round_number}_overwrite"
                execute_sets(client, churn_round.overwrites, values, args.batch_size)
                sampler.phase = f"churn_{round_number}_delete"
                execute_deletes(client, churn_round.deletes, args.batch_size)
                sampler.phase = f"churn_{round_number}_insert"
                execute_sets(client, churn_round.inserts, values, args.batch_size)

            sampler.phase = "churn_settle"
            settle(client, args.settle_seconds)

        snapshot = sample_metrics(client, container_name, allocator_name)
        raw_info = raw_info_memory(client)
    finally:
        sampler.stop()

    return {
        "snapshot": snapshot,
        "raw_info": raw_info,
        "timeline": sampler.records,
        "fingerprint": plan.fingerprint,
        "operations": plan.operation_count if include_churn else len(plan.initial),
    }


def probe_defrag(
    jemalloc_client: redis.Redis,
    libc_client: redis.Redis,
    args: argparse.Namespace,
) -> dict:
    before = sample_metrics(jemalloc_client, CONTAINER_JEMALLOC, "before_defrag")
    jemalloc_client.config_set("active-defrag-ignore-bytes", "1048576")
    jemalloc_client.config_set("active-defrag-threshold-lower", "1")
    jemalloc_client.config_set("active-defrag-threshold-upper", "100")
    jemalloc_client.config_set("active-defrag-cycle-min", "25")
    jemalloc_client.config_set("active-defrag-cycle-max", "75")
    jemalloc_client.config_set("activedefrag", "yes")

    deadline = time.monotonic() + args.defrag_seconds
    observed_running = False
    while time.monotonic() < deadline:
        info = jemalloc_client.info("memory")
        observed_running = observed_running or bool(info.get("active_defrag_running", 0))
        time.sleep(0.5)

    after = sample_metrics(jemalloc_client, CONTAINER_JEMALLOC, "after_defrag")
    raw_after = raw_info_memory(jemalloc_client)
    jemalloc_client.config_set("activedefrag", "no")

    libc_result = "accepted"
    try:
        libc_client.config_set("activedefrag", "yes")
        libc_client.config_set("activedefrag", "no")
    except redis.exceptions.ResponseError as exc:
        libc_result = f"rejected: {exc}"

    return {
        "before": before,
        "after": after,
        "raw_after": raw_after,
        "observed_running": observed_running,
        "libc_result": libc_result,
    }


def write_results(
    results_dir: Path,
    jemalloc_run: dict,
    libc_run: dict,
    metadata: dict[str, object],
    defrag: dict | None,
) -> None:
    results_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="redis-jemalloc-results-", dir=results_dir.parent
    ) as temp_name:
        temp_dir = Path(temp_name)
        write_csv(
            temp_dir / "comparison.csv",
            COMPARISON_FIELDS,
            [jemalloc_run["snapshot"], libc_run["snapshot"]],
        )
        write_csv(
            temp_dir / "memory_timeline_je.csv",
            TIMELINE_FIELDS,
            jemalloc_run["timeline"],
        )
        write_csv(
            temp_dir / "memory_timeline_libc.csv",
            TIMELINE_FIELDS,
            libc_run["timeline"],
        )
        write_csv(
            temp_dir / "run_metadata.csv",
            ("key", "value"),
            ({"key": key, "value": value} for key, value in metadata.items()),
        )
        write_raw_info(temp_dir / "info_memory_jemalloc.txt", jemalloc_run["raw_info"])
        write_raw_info(temp_dir / "info_memory_libc.txt", libc_run["raw_info"])
        if defrag is not None:
            write_raw_info(temp_dir / "info_memory_je_defrag.txt", defrag["raw_after"])

        results_dir.mkdir(parents=True, exist_ok=True)
        for path in temp_dir.iterdir():
            os.replace(path, results_dir / path.name)


def validate_runs(
    jemalloc_run: dict,
    libc_run: dict,
    args: argparse.Namespace,
) -> list[str]:
    failures: list[str] = []
    je = jemalloc_run["snapshot"]
    libc = libc_run["snapshot"]

    if "jemalloc" not in str(je["mem_allocator"]):
        failures.append(f"jemalloc build reports {je['mem_allocator']}")
    if str(libc["mem_allocator"]) != "libc":
        failures.append(f"libc build reports {libc['mem_allocator']}")
    if jemalloc_run["fingerprint"] != libc_run["fingerprint"]:
        failures.append("workload fingerprints differ")
    if jemalloc_run["operations"] != libc_run["operations"]:
        failures.append("workload operation counts differ")

    for label, row in (("jemalloc", je), ("libc", libc)):
        if int(row["number_of_keys"]) != args.keys:
            failures.append(
                f"{label} has {row['number_of_keys']} data keys; expected {args.keys}"
            )
        if int(row["evicted_keys"]) != 0:
            failures.append(f"{label} evicted {row['evicted_keys']} keys")

    return failures


def run_pair(args: argparse.Namespace, include_churn: bool, results_dir: Path) -> None:
    jemalloc_client = wait_for_redis(URL_JEMALLOC)
    libc_client = wait_for_redis(URL_LIBC)
    plan = build_plan(
        args.keys,
        args.rounds if include_churn else 0,
        args.churn_fraction,
        args.seed,
    )

    je_server = jemalloc_client.info("server")
    libc_server = libc_client.info("server")
    je_allocator = jemalloc_client.info("memory").get("mem_allocator", "unknown")
    libc_allocator = libc_client.info("memory").get("mem_allocator", "unknown")
    if "jemalloc" not in str(je_allocator) or str(libc_allocator) != "libc":
        raise RuntimeError(
            f"allocator verification failed before the run: {je_allocator!r}, {libc_allocator!r}"
        )
    if je_server.get("redis_version") != libc_server.get("redis_version"):
        raise RuntimeError("the two containers report different Redis versions")
    if je_server.get("redis_version") != args.redis_version:
        raise RuntimeError(
            f"running Redis {je_server.get('redis_version')}, expected {args.redis_version}"
        )

    jemalloc_run = run_workload(
        jemalloc_client,
        URL_JEMALLOC,
        CONTAINER_JEMALLOC,
        "jemalloc",
        plan,
        args,
        include_churn,
    )
    libc_run = run_workload(
        libc_client,
        URL_LIBC,
        CONTAINER_LIBC,
        "libc",
        plan,
        args,
        include_churn,
    )
    defrag = probe_defrag(jemalloc_client, libc_client, args) if include_churn else None

    failures = validate_runs(jemalloc_run, libc_run, args)
    je = jemalloc_run["snapshot"]
    libc = libc_run["snapshot"]
    metadata: dict[str, object] = {
        "run_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "docker": command_output(["docker", "--version"]),
        "docker_compose": command_output(["docker", "compose", "version"]),
        "redis_version": je_server.get("redis_version", "unknown"),
        "source_tag": args.redis_version,
        "mode": "churn" if include_churn else "no-churn-control",
        "seed": args.seed,
        "keys": args.keys,
        "rounds": args.rounds if include_churn else 0,
        "churn_fraction": args.churn_fraction if include_churn else 0,
        "value_sizes": ";".join(str(size) for size in VALUE_SIZES),
        "batch_size": args.batch_size,
        "sample_interval_seconds": args.sample_interval,
        "settle_seconds": args.settle_seconds,
        "defrag_seconds": args.defrag_seconds if include_churn else 0,
        "workload_fingerprint_sha256": plan.fingerprint,
        "workload_operations_per_build": jemalloc_run["operations"],
        "jemalloc_allocator": je["mem_allocator"],
        "libc_allocator": libc["mem_allocator"],
        "used_memory_difference_pct": (
            abs(int(je["used_memory"]) - int(libc["used_memory"]))
            / max(int(je["used_memory"]), int(libc["used_memory"]))
            * 100
        ),
        "used_memory_tolerance_pct": args.used_tolerance * 100,
        "used_memory_within_tolerance": (
            abs(int(je["used_memory"]) - int(libc["used_memory"]))
            / max(int(je["used_memory"]), int(libc["used_memory"]))
            <= args.used_tolerance
        ),
        "jemalloc_rss_bytes": je["used_memory_rss"],
        "libc_rss_bytes": libc["used_memory_rss"],
        "jemalloc_fragmentation_ratio": je["mem_fragmentation_ratio"],
        "libc_fragmentation_ratio": libc["mem_fragmentation_ratio"],
    }
    if defrag is not None:
        metadata.update(
            {
                "jemalloc_defrag_observed_running": defrag["observed_running"],
                "jemalloc_defrag_rss_before": defrag["before"]["used_memory_rss"],
                "jemalloc_defrag_rss_after": defrag["after"]["used_memory_rss"],
                "libc_defrag_result": defrag["libc_result"],
            }
        )
    metadata["self_verification"] = "PASS" if not failures else "FAIL: " + "; ".join(failures)

    write_results(results_dir, jemalloc_run, libc_run, metadata, defrag)
    print(f"\nEvidence written to {results_dir}", flush=True)
    print(
        f"jemalloc: used={je['used_memory_human']} rss={je['used_memory_rss_human']} "
        f"ratio={je['mem_fragmentation_ratio']}",
        flush=True,
    )
    print(
        f"libc:     used={libc['used_memory_human']} rss={libc['used_memory_rss_human']} "
        f"ratio={libc['mem_fragmentation_ratio']}",
        flush=True,
    )
    used_difference = abs(int(je["used_memory"]) - int(libc["used_memory"])) / max(
        int(je["used_memory"]), int(libc["used_memory"])
    )
    if used_difference > args.used_tolerance:
        print(
            f"RESULT: used_memory differs by {used_difference:.2%}; workload identity still "
            "verified by fingerprint, operation count, and final key count",
            flush=True,
        )

    if failures:
        raise RuntimeError("self-verification failed after evidence was written: " + "; ".join(failures))
    print("Self-verification PASS", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("all", "control"))
    parser.add_argument("--reset", action="store_true", help="required destructive reset gate")
    parser.add_argument("--keys", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--churn-fraction", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--defrag-seconds", type=float, default=10.0)
    parser.add_argument("--used-tolerance", type=float, default=0.05)
    parser.add_argument("--redis-version", default="7.4.0")
    parser.add_argument("--results-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.reset:
        print("ERROR: all benchmark commands require --reset", file=sys.stderr)
        return 2

    include_churn = args.command == "all"
    if args.results_dir is not None:
        results_dir = args.results_dir
    elif include_churn:
        results_dir = DEFAULT_RESULTS
    else:
        results_dir = DEFAULT_RESULTS / "attempts" / "no-churn-control"

    try:
        run_pair(args, include_churn, results_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
