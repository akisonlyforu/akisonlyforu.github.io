#!/usr/bin/env python3
"""Benchmark to compare jemalloc and libc allocators under identical Redis churn workloads."""

import argparse
import csv
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import redis

ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"

# Ports set in docker-compose.yml
URL_JEMALLOC = os.environ.get("REDIS_JE_URL", "redis://127.0.0.1:56380/0")
URL_LIBC = os.environ.get("REDIS_LIBC_URL", "redis://127.0.0.1:56381/0")

CONTAINER_JE = "redis-jemalloc-je"
CONTAINER_LIBC = "redis-jemalloc-libc"

SLEEP_SCALE = 0.1

def sleep_scaled(seconds):
    time.sleep(seconds * SLEEP_SCALE)

def redis_connect(url):
    return redis.Redis.from_url(url, decode_responses=True)

def wait_for_redis(url, timeout=60.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            client = redis_connect(url)
            client.ping()
            return client
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Redis at {url} did not become ready: {last_error}")

def verify_redis_database(client):
    db_size = client.dbsize()
    if db_size == 0:
        client.set("redis_jemalloc_bench_marker", "redis_jemalloc_bench_v1")
        return
    
    marker = client.get("redis_jemalloc_bench_marker")
    if marker != "redis_jemalloc_bench_v1":
        raise RuntimeError(
            "refusing destructive reset: database is not empty and lacks the marker key 'redis_jemalloc_bench_marker'"
        )

def get_container_cgroup_memory(container_name):
    # Try cgroup v2 memory.current
    try:
        res = subprocess.run(
            ["docker", "exec", container_name, "cat", "/sys/fs/cgroup/memory.current"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        pass
    
    # Try cgroup v1 memory.usage_in_bytes
    try:
        res = subprocess.run(
            ["docker", "exec", container_name, "cat", "/sys/fs/cgroup/memory/memory.usage_in_bytes"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def get_container_limit_via_docker(container_name):
    try:
        res = subprocess.run(
            ["docker", "inspect", container_name, "--format={{.HostConfig.Memory}}"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def get_cgroup_limit_internal(container_name):
    # Try container memory.max
    try:
        res = subprocess.run(
            ["docker", "exec", container_name, "cat", "/sys/fs/cgroup/memory.max"],
            capture_output=True, text=True, check=True, timeout=5
        )
        val = res.stdout.strip()
        if val != "max":
            return int(val)
    except Exception:
        pass
    
    # Try container cgroup v1 limit
    try:
        res = subprocess.run(
            ["docker", "exec", container_name, "cat", "/sys/fs/cgroup/memory/memory.limit_in_bytes"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

def format_human(bytes_val):
    if bytes_val is None:
        return "0B"
    try:
        bytes_val = float(bytes_val)
    except ValueError:
        return str(bytes_val)
    for unit in ['B', 'K', 'M', 'G', 'T']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f}{unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f}P"

def sample_info_metrics(client, container_name, phase_name):
    try:
        info = client.info("memory")
    except Exception:
        return None
    
    cg_mem = get_container_cgroup_memory(container_name)
    cg_lim = get_container_limit_via_docker(container_name)
    if cg_lim == 0:
        cg_lim = get_cgroup_limit_internal(container_name)
        
    metrics = {
        "phase": phase_name,
        "used_memory": info.get("used_memory"),
        "used_memory_human": info.get("used_memory_human"),
        "used_memory_rss": info.get("used_memory_rss"),
        "used_memory_rss_human": info.get("used_memory_rss_human"),
        "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
        "mem_allocator": info.get("mem_allocator"),
        "maxmemory": info.get("maxmemory"),
        "maxmemory_human": info.get("maxmemory_human"),
        "maxmemory_policy": info.get("maxmemory_policy"),
        "evicted_keys": info.get("evicted_keys"),
        "allocator_allocated": info.get("allocator_allocated"),
        "allocator_active": info.get("allocator_active"),
        "allocator_resident": info.get("allocator_resident"),
        "allocator_frag_ratio": info.get("allocator_frag_ratio"),
        "allocator_frag_bytes": info.get("allocator_frag_bytes"),
        "number_of_keys": client.dbsize(),
        "cgroup_memory": cg_mem,
        "cgroup_limit": cg_lim,
    }
    return metrics

class TimelineSampler:
    def __init__(self, client, container_name, interval=0.1):
        self.client = client
        self.container_name = container_name
        self.interval = interval
        self.records = []
        self.active_phase = "init"
        self.stop_event = threading.Event()
        self.thread = None
        self.start_time = None

    def start(self):
        self.start_time = time.monotonic()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2.0)

    def _run(self):
        while not self.stop_event.is_set():
            loop_start = time.monotonic()
            elapsed = loop_start - self.start_time
            metrics = sample_info_metrics(self.client, self.container_name, self.active_phase)
            if metrics:
                metrics["time_sec"] = f"{elapsed:.3f}"
                self.records.append(metrics)
            else:
                break
            
            sleep_time = self.interval - (time.monotonic() - loop_start)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

def run_allocator_test(url, container, name, args):
    print(f"\n--- Running workload against {name} build ({url}) ---", flush=True)
    client = wait_for_redis(url)
    verify_redis_database(client)
    
    if args.reset:
        print(f"Flushing database on {name}...", flush=True)
        client.flushall()
        client.set("redis_jemalloc_bench_marker", "redis_jemalloc_bench_v1")
        sleep_scaled(2.0)
        
    info = client.info("memory")
    allocator = info.get("mem_allocator", "unknown")
    print(f"{name} linked allocator: {allocator}", flush=True)
    
    # Establish timeline sampler
    sampler = TimelineSampler(client, container, interval=0.05)
    sampler.start()
    
    raw_before = None
    raw_settled = None
    settled_snap = None
    
    try:
        # Phase 0: Settle empty
        sampler.active_phase = "empty_settle"
        sleep_scaled(2.0)
        
        # Load workload
        sampler.active_phase = "loading"
        print(f"Loading {args.keys} initial keys...", flush=True)
        
        random.seed(args.seed)
        sizes = [64, 128, 256, 512, 1024]
        
        # We pre-generate values of fixed sizes to make writes fast
        val_map = {sz: "a" * sz for sz in sizes}
        
        pipe = client.pipeline(transaction=False)
        for i in range(args.keys):
            sz = random.choice(sizes)
            pipe.set(f"key:{i}", val_map[sz])
            if i % 10000 == 0:
                pipe.execute()
        pipe.execute()
        
        # Phase 1: Settle loaded
        sampler.active_phase = "loaded_settle"
        sleep_scaled(3.0)
        raw_before = client.info("memory")
        
        # Phase 2: Churn
        sampler.active_phase = "churn"
        print(f"Running {args.rounds} churn rounds...", flush=True)
        
        # We execute deterministic set/overwrite/delete churn
        total_keys = args.keys
        for round_idx in range(args.rounds):
            # Overwrite 20% of keys with larger/different size class
            overwrite_count = int(total_keys * 0.20)
            overwrite_indices = random.sample(range(total_keys), overwrite_count)
            
            pipe = client.pipeline(transaction=False)
            for idx in overwrite_indices:
                sz = random.choice(sizes)
                pipe.set(f"key:{idx}", val_map[sz])
            pipe.execute()
            
            # Delete 20% of keys
            delete_count = int(total_keys * 0.20)
            delete_indices = random.sample(range(total_keys), delete_count)
            pipe = client.pipeline(transaction=False)
            for idx in delete_indices:
                pipe.delete(f"key:{idx}")
            pipe.execute()
            
            # Insert 20% fresh keys
            pipe = client.pipeline(transaction=False)
            for i in range(delete_count):
                sz = random.choice(sizes)
                # write to key indices that were deleted or fresh namespaces
                pipe.set(f"key:new:{round_idx}:{i}", val_map[sz])
            pipe.execute()
            
            sleep_scaled(0.5)
            
        # Phase 3: Settle settled
        sampler.active_phase = "churn_settle"
        print("Settle churn footprint...", flush=True)
        sleep_scaled(5.0)
        
        raw_settled = client.info("memory")
        settled_snap = sample_info_metrics(client, container, "settled")
        
    finally:
        sampler.stop()
        
    return raw_before, raw_settled, settled_snap, sampler.records

def run_experiment(args):
    # Verify both containers are up
    try:
        je_client = wait_for_redis(URL_JEMALLOC)
        lc_client = wait_for_redis(URL_LIBC)
    except Exception as exc:
        print(f"Error connecting to containers: {exc}", flush=True)
        print("Please ensure 'docker compose up -d --wait' has been run in the harness folder.", flush=True)
        sys.exit(1)
        
    # Verify allocators actually differ
    je_info = je_client.info("memory")
    lc_info = lc_client.info("memory")
    
    je_alloc = je_info.get("mem_allocator", "unknown")
    lc_alloc = lc_info.get("mem_allocator", "unknown")
    
    print(f"Jemalloc container allocator: {je_alloc}")
    print(f"Libc container allocator: {lc_alloc}")
    
    if "jemalloc" not in je_alloc:
        print(f"ERROR: Jemalloc build does not report jemalloc allocator. Found: {je_alloc}", flush=True)
        sys.exit(1)
        
    if "libc" not in lc_alloc:
        print(f"ERROR: Libc build does not report libc allocator. Found: {lc_alloc}", flush=True)
        sys.exit(1)
        
    print("Allocator validation PASSED. Proceeding to run identical churn workload on both builds...", flush=True)
    
    # Run jemalloc build
    je_before, je_settled, je_snap, je_timeline = run_allocator_test(URL_JEMALLOC, CONTAINER_JE, "jemalloc", args)
    
    # Run libc build
    lc_before, lc_settled, lc_snap, lc_timeline = run_allocator_test(URL_LIBC, CONTAINER_LIBC, "libc", args)
    
    # Run defrag probe on jemalloc build
    print("\n--- Testing activedefrag on Jemalloc build ---", flush=True)
    je_defrag_raw = None
    je_client.config_set("active-defrag-ignore-bytes", "1048576") # 1MB
    je_client.config_set("active-defrag-threshold-lower", "5") # 5%
    je_client.config_set("activedefrag", "yes")
    
    # Poll defrag settle
    for sec in range(15):
        sleep_scaled(1.0)
        info = je_client.info("memory")
        print(f"Defrag sec {sec}: RSS: {info.get('used_memory_rss_human')}, Ratio: {info.get('mem_fragmentation_ratio')}", flush=True)
        if float(info.get('mem_fragmentation_ratio', 0.0)) < 2.5:
            break
            
    je_defrag_raw = je_client.info("memory")
    je_client.config_set("activedefrag", "no")
    
    # Run defrag probe on libc build (expect it to return an error or be a no-op)
    print("\n--- Testing activedefrag on Libc build ---", flush=True)
    lc_defrag_err = None
    try:
        lc_client.config_set("activedefrag", "yes")
        print("Libc build accepted CONFIG SET activedefrag yes", flush=True)
        lc_client.config_set("activedefrag", "no")
    except redis.exceptions.ResponseError as err:
        lc_defrag_err = str(err)
        print(f"Libc build rejected activedefrag: {lc_defrag_err}", flush=True)
        
    # Stage files in temporary directory first
    print("\nWriting evidence files...", flush=True)
    temp_dir = Path(tempfile.mkdtemp())
    
    # 1. run_metadata.csv
    metadata = {
        "redis_version": je_info.get("redis_version", "unknown"),
        "source_tag": args.redis_version,
        "seed": args.seed,
        "keys": args.keys,
        "rounds": args.rounds,
        "jemalloc_allocator": je_alloc,
        "libc_allocator": lc_alloc,
        "libc_defrag_error": lc_defrag_err or "no error raised",
    }
    metadata_fields = list(metadata.keys())
    write_csv(temp_dir / "run_metadata.csv", metadata_fields, [metadata])
    
    # 2. comparison.csv
    # Fields must be ordered and common
    common_fields = [
        "phase", "used_memory", "used_memory_human", "used_memory_rss", "used_memory_rss_human",
        "mem_fragmentation_ratio", "mem_allocator", "maxmemory_human", "evicted_keys", "number_of_keys",
        "cgroup_memory", "cgroup_limit"
    ]
    
    # Extract fields into comparison rows
    def get_comp_row(phase_name, snap):
        return {k: snap[k] for k in common_fields}
        
    je_comp = get_comp_row("jemalloc", je_snap)
    lc_comp = get_comp_row("libc", lc_snap)
    write_csv(temp_dir / "comparison.csv", common_fields, [je_comp, lc_comp])
    
    # 3. memory_timeline_je.csv
    if je_timeline:
        timeline_fields = list(je_timeline[0].keys())
        write_csv(temp_dir / "memory_timeline_je.csv", timeline_fields, je_timeline)
        
    # 4. memory_timeline_libc.csv
    if lc_timeline:
        timeline_fields = list(lc_timeline[0].keys())
        write_csv(temp_dir / "memory_timeline_libc.csv", timeline_fields, lc_timeline)
        
    # Raw dumps helper
    def write_txt(filename, data):
        if data:
            with (temp_dir / filename).open("w", encoding="utf-8") as f:
                for k, v in data.items():
                    f.write(f"{k}:{v}\n")
                    
    write_txt("info_memory_jemalloc.txt", je_settled)
    write_txt("info_memory_libc.txt", lc_settled)
    write_txt("info_memory_je_defrag.txt", je_defrag_raw)
    
    # Move files to results
    results_dir = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    
    for file in temp_dir.iterdir():
        dest = results_dir / file.name
        if file.is_file():
            if dest.exists():
                dest.unlink()
            file.rename(dest)
            
    print(f"Results written to {results_dir}", flush=True)
    print("Files inside results_dir:", [f.name for f in results_dir.iterdir()], flush=True)
    for f in results_dir.iterdir():
        if f.is_file():
            print(f"=== FILE_CONTENT START: {f.name} ===", flush=True)
            print(f.read_text(encoding='utf-8'), flush=True)
            print(f"=== FILE_CONTENT END: {f.name} ===", flush=True)
            
    # Self-verification checks
    failures = []
    
    # Check used_memory is close between the two builds (tolerance 10%)
    je_used = float(je_snap["used_memory"])
    lc_used = float(lc_snap["used_memory"])
    diff_pct = abs(je_used - lc_used) / max(je_used, lc_used)
    if diff_pct > 0.10:
        failures.append(f"used_memory differed significantly between allocators: JE {format_human(je_used)} vs LIBC {format_human(lc_used)} ({diff_pct*100:.2f}%)")
        
    if je_snap["mem_allocator"] == lc_snap["mem_allocator"]:
        failures.append(f"allocators did not differ: both reported {je_snap['mem_allocator']}")
        
    if failures:
        print("\nSelf-verification FAILURES:", flush=True)
        for fail in failures:
            print(f"FAIL: {fail}", flush=True)
        sys.exit(1)
    else:
        print("\nSelf-verification PASS: allocator differences captured cleanly!", flush=True)
        sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Redis allocator comparison benchmark.")
    parser.add_argument("command", choices=["all", "je", "libc"], default="all", nargs="?", help="Build to run.")
    parser.add_argument("--reset", action="store_true", help="Flush Redis DB before running.")
    parser.add_argument("--keys", type=int, default=50000, help="Number of churn keys.")
    parser.add_argument("--rounds", type=int, default=5, help="Number of churn rounds.")
    parser.add_argument("--seed", type=int, default=42, help="Seed value.")
    parser.add_argument("--redis-version", default="7.4.0", help="Redis version tag.")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS, help="Output results directory.")
    args = parser.parse_args()
    
    run_experiment(args)

if __name__ == "__main__":
    main()
