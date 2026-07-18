#!/usr/bin/env python3
"""Reproduce Redis RSS fragmentation and OOM killer behavior under cgroups."""

import argparse
import csv
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import redis

ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results"
REDIS_URL = os.environ.get("REDIS_OOM_BENCH_URL", "redis://127.0.0.1:56379/0")

# Image version: redis:7.4
# Allocator: jemalloc-5.3.0

SLEEP_SCALE = 0.1

def sleep_scaled(seconds):
    time.sleep(seconds * SLEEP_SCALE)

def redis_connect():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)

def wait_for_redis(timeout=60.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            client = redis_connect()
            client.ping()
            return client
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("Redis did not become ready: %s" % last_error)

def verify_redis_database(client):
    db_size = client.dbsize()
    if db_size == 0:
        client.set("redis_oom_bench_marker", "redis_oom_bench_v1")
        return
    
    marker = client.get("redis_oom_bench_marker")
    if marker != "redis_oom_bench_v1":
        raise RuntimeError(
            "refusing destructive reset: database is not empty and lacks the marker key 'redis_oom_bench_marker'"
        )

def get_container_cgroup_memory():
    # Try cgroup v2 memory.current
    try:
        res = subprocess.run(
            ["docker", "exec", "redis-oom-redis", "cat", "/sys/fs/cgroup/memory.current"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        pass
    
    # Try cgroup v1 memory.usage_in_bytes
    try:
        res = subprocess.run(
            ["docker", "exec", "redis-oom-redis", "cat", "/sys/fs/cgroup/memory/memory.usage_in_bytes"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def get_container_limit_via_docker():
    try:
        res = subprocess.run(
            ["docker", "inspect", "redis-oom-redis", "--format={{.HostConfig.Memory}}"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def get_cgroup_limit_internal():
    # Try container memory.max
    try:
        res = subprocess.run(
            ["docker", "exec", "redis-oom-redis", "cat", "/sys/fs/cgroup/memory.max"],
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
            ["docker", "exec", "redis-oom-redis", "cat", "/sys/fs/cgroup/memory/memory.limit_in_bytes"],
            capture_output=True, text=True, check=True, timeout=5
        )
        return int(res.stdout.strip())
    except Exception:
        return 0

def check_container_oom_status():
    try:
        res = subprocess.run(
            ["docker", "inspect", "redis-oom-redis", "--format={{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Status}}"],
            capture_output=True, text=True, check=True, timeout=5
        )
        parts = res.stdout.strip().split()
        if len(parts) >= 3:
            oom_killed = parts[0] == "true"
            exit_code = int(parts[1])
            status = parts[2]
            return oom_killed, exit_code, status
    except Exception:
        pass
    return False, 0, "unknown"

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

def sample_info_metrics(client, phase_name):
    try:
        info = client.info("memory")
    except Exception:
        # Redis might be dead (OOM)
        return None
    
    cg_mem = get_container_cgroup_memory()
    cg_lim = get_container_limit_via_docker()
    if cg_lim == 0:
        cg_lim = get_cgroup_limit_internal()
        
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
    def __init__(self, client, interval=0.1):
        self.client = client
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
            metrics = sample_info_metrics(self.client, self.active_phase)
            if metrics:
                metrics["time_sec"] = f"{elapsed:.3f}"
                self.records.append(metrics)
            else:
                # Redis connection failed (OOMed)
                cg_mem = get_container_cgroup_memory()
                cg_lim = get_container_limit_via_docker()
                self.records.append({
                    "time_sec": f"{elapsed:.3f}",
                    "phase": "OOM_EVENT",
                    "used_memory": 0,
                    "used_memory_human": "0B",
                    "used_memory_rss": 0,
                    "used_memory_rss_human": "0B",
                    "mem_fragmentation_ratio": 0.0,
                    "mem_allocator": "unknown",
                    "maxmemory": 0,
                    "maxmemory_human": "0B",
                    "maxmemory_policy": "unknown",
                    "evicted_keys": 0,
                    "allocator_allocated": 0,
                    "allocator_active": 0,
                    "allocator_resident": 0,
                    "allocator_frag_ratio": 0.0,
                    "allocator_frag_bytes": 0,
                    "number_of_keys": 0,
                    "cgroup_memory": cg_mem,
                    "cgroup_limit": cg_lim,
                })
                break
            
            sleep_time = self.interval - (time.monotonic() - loop_start)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

def run_experiment(args):
    # Establish connection
    client = wait_for_redis()
    
    # Defense check
    verify_redis_database(client)
    
    if args.reset:
        print("Performing database flushall...", flush=True)
        client.flushall()
        client.set("redis_oom_bench_marker", "redis_oom_bench_v1")
    
    # Configure Redis base limit for the test
    client.config_set("maxmemory", args.maxmemory)
    client.config_set("maxmemory-policy", "allkeys-lru")
    client.config_set("activedefrag", "no")
    
    # Gather run metadata
    redis_info = client.info("server")
    memory_info = client.info("memory")
    
    metadata = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "redis_version": redis_info.get("redis_version"),
        "mem_allocator": memory_info.get("mem_allocator"),
        "cgroup_limit": get_container_limit_via_docker(),
        "keys_count_arg": args.keys,
        "value_bytes_arg": args.value_bytes,
        "maxmemory_arg": args.maxmemory,
        "delete_batch_arg": args.delete_batch,
    }
    
    print(f"Harness started. Redis version: {metadata['redis_version']}, Allocator: {metadata['mem_allocator']}", flush=True)
    
    snapshots = []
    
    # Start timeline sampler
    sampler = TimelineSampler(client, interval=0.05)
    sampler.start()
    
    try:
        # Phase 0: Settle empty
        sampler.active_phase = "empty_settle"
        sleep_scaled(2.0)
        
        # Phase 1: Loading
        sampler.active_phase = "loading"
        print(f"Loading {args.keys} keys of size {args.value_bytes} bytes...", flush=True)
        val_str = "a" * args.value_bytes
        
        pipe = client.pipeline(transaction=False)
        for i in range(args.keys):
            pipe.set(f"key:{i}", val_str)
            if i % 10000 == 0:
                pipe.execute()
        pipe.execute()
        
        # Phase 2: Loaded settled (Before)
        sampler.active_phase = "loaded_settle"
        print("Settle loaded keys...", flush=True)
        sleep_scaled(5.0)
        
        before_raw = client.info("memory")
        before_snap = sample_info_metrics(client, "before")
        snapshots.append(before_snap)
        
        # Phase 3: Bulk Delete
        sampler.active_phase = "bulk_delete"
        print("Bulk deleting keys...", flush=True)
        # Delete in batches to avoid protocol limits, but fast
        for idx in range(0, args.keys, 10000):
            batch = [f"key:{i}" for i in range(idx, min(idx + 10000, args.keys))]
            client.delete(*batch)
            
        # Seed 10,000 keys back immediately to give active defrag something to scan
        print("Seeding 10,000 keys back to enable defrag scanning...", flush=True)
        pipe = client.pipeline(transaction=False)
        for i in range(10000):
            pipe.set(f"key_back:{i}", val_str)
        pipe.execute()
        
        # Phase 4: Settle deleted (After)
        sampler.active_phase = "deleted_settle"
        print("Settle post-delete footprint...", flush=True)
        sleep_scaled(5.0)
        
        after_raw = client.info("memory")
        after_snap = sample_info_metrics(client, "after")
        snapshots.append(after_snap)
        
        # Phase 5: Active Defrag
        sampler.active_phase = "defrag"
        print("Enabling active defrag with aggressive configuration...", flush=True)
        client.config_set("active-defrag-ignore-bytes", "1048576") # 1MB
        client.config_set("active-defrag-threshold-lower", "5") # 5%
        client.config_set("activedefrag", "yes")
        
        # Poll defrag
        defrag_settled = False
        for sec in range(25):
            sleep_scaled(1.0)
            info = client.info("memory")
            print(f"Defrag sec {sec}: RSS: {info.get('used_memory_rss_human')}, Ratio: {info.get('mem_fragmentation_ratio')}", flush=True)
            if float(info.get('mem_fragmentation_ratio', 0.0)) < 6.0:
                print("Defrag settled successfully.", flush=True)
                defrag_settled = True
                break
                
        defrag_raw = client.info("memory")
        defrag_snap = sample_info_metrics(client, "defrag")
        snapshots.append(defrag_snap)
        
        # Restore active defrag settings
        client.config_set("activedefrag", "no")
        
        # Phase 6: Reset for Incremental Delete
        sampler.active_phase = "reset_incremental"
        print("Resetting database for incremental unlink delete...", flush=True)
        client.flushall()
        client.set("redis_oom_bench_marker", "redis_oom_bench_v1")
        sleep_scaled(3.0)
        
        # Phase 7: Loading incremental
        sampler.active_phase = "loading_incremental"
        print(f"Loading keys for incremental test...", flush=True)
        pipe = client.pipeline(transaction=False)
        for i in range(args.keys):
            pipe.set(f"key:{i}", val_str)
            if i % 10000 == 0:
                pipe.execute()
        pipe.execute()
        
        # Phase 8: Settling loaded incremental
        sampler.active_phase = "loaded_settle_incremental"
        sleep_scaled(5.0)
        
        # Phase 9: Incremental Delete
        sampler.active_phase = "incremental_delete"
        print(f"Deleting keys incrementally with UNLINK in batches of {args.delete_batch}...", flush=True)
        for idx in range(0, args.keys, args.delete_batch):
            batch = [f"key:{i}" for i in range(idx, min(idx + args.delete_batch, args.keys))]
            client.unlink(*batch)
            sleep_scaled(0.01) # brief pause to let allocator decay pages
            
        # Phase 10: Settling after incremental delete
        sampler.active_phase = "incremental_settle"
        sleep_scaled(5.0)
        
        incremental_raw = client.info("memory")
        incremental_snap = sample_info_metrics(client, "incremental")
        snapshots.append(incremental_snap)
        
        # Phase 11: OOM Kill Simulation
        sampler.active_phase = "oom_simulation"
        print("Starting OOM Simulation...", flush=True)
        print("Resetting database...", flush=True)
        client.flushall()
        client.set("redis_oom_bench_marker", "redis_oom_bench_v1")
        sleep_scaled(3.0)
        
        # Load keys to reach initial footprint
        print("Loading initial keys for OOM footprint...", flush=True)
        pipe = client.pipeline(transaction=False)
        for i in range(args.keys):
            pipe.set(f"key:{i}", val_str)
            if i % 10000 == 0:
                pipe.execute()
        pipe.execute()
        sleep_scaled(3.0)
        
        # Bulk delete them (RSS stays high)
        print("Bulk deleting keys to create fragmentation...", flush=True)
        for idx in range(0, args.keys, 10000):
            batch = [f"key:{i}" for i in range(idx, min(idx + 10000, args.keys))]
            client.delete(*batch)
        sleep_scaled(3.0)
        
        # Disable maxmemory to force RSS/cgroup memory to climb past container limit
        print("Disabling maxmemory to force RSS to exceed cgroup limit...", flush=True)
        client.config_set("maxmemory", "0")
        
        # Write unique keys until ConnectionError (OOM)
        print("Writing unique keys to trigger OOM...", flush=True)
        oom_triggered = False
        try:
            for i in range(1000000):
                client.set(f"oom_key_{i}", val_str)
                if i % 10000 == 0:
                    cg_mem = get_container_cgroup_memory()
                    print(f"Seeded {i} unique keys, cgroup memory: {format_human(cg_mem)}", flush=True)
        except redis.exceptions.ConnectionError:
            print("Connection lost! Redis container has likely OOMed.", flush=True)
            oom_triggered = True
            
        sleep_scaled(2.0) # Settle docker state update
        
        # Verify OOM killed
        oom_killed, exit_code, container_status = check_container_oom_status()
        print(f"Container status: OOMKilled={oom_killed}, ExitCode={exit_code}, Status={container_status}", flush=True)
        
        metadata["oom_killed"] = oom_killed
        metadata["container_exit_code"] = exit_code
        metadata["container_status"] = container_status
        metadata["oom_triggered_exception"] = oom_triggered
        
    finally:
        sampler.stop()
        
        # Stage files in temporary directory first
        print("Writing evidence files...", flush=True)
        temp_dir = Path(tempfile.mkdtemp())
        
        # 1. run_metadata.csv
        metadata_fields = list(metadata.keys())
        write_csv(temp_dir / "run_metadata.csv", metadata_fields, [metadata])
        
        # 2. memory_snapshots.csv
        if snapshots:
            snap_fields = list(snapshots[0].keys())
            write_csv(temp_dir / "memory_snapshots.csv", snap_fields, snapshots)
            
        # 3. memory_timeline.csv
        if sampler.records:
            timeline_fields = list(sampler.records[0].keys())
            write_csv(temp_dir / "memory_timeline.csv", timeline_fields, sampler.records)
            
        # Write info dumps if gathered
        def write_txt(filename, data):
            if data:
                with (temp_dir / filename).open("w", encoding="utf-8") as f:
                    for k, v in data.items():
                        f.write(f"{k}:{v}\n")
                        
        if 'before_raw' in locals():
            write_txt("info_memory_before.txt", before_raw)
        if 'after_raw' in locals():
            write_txt("info_memory_after.txt", after_raw)
        if 'defrag_raw' in locals():
            write_txt("info_memory_defrag.txt", defrag_raw)
        if 'incremental_raw' in locals():
            write_txt("info_memory_incremental.txt", incremental_raw)
            
        # Move files to results
        results_dir = args.results_dir
        results_dir.mkdir(parents=True, exist_ok=True)
        
        for file in temp_dir.iterdir():
            dest = results_dir / file.name
            if file.is_file():
                if dest.exists():
                    dest.unlink()
                file.rename(dest)
            elif file.is_dir():
                # attempts or other dirs
                pass
                
        print(f"Results written to {results_dir}", flush=True)
        print("Files inside results_dir:", [f.name for f in results_dir.iterdir()], flush=True)
        for f in results_dir.iterdir():
            if f.is_file():
                print(f"=== FILE_CONTENT START: {f.name} ===", flush=True)
                print(f.read_text(encoding='utf-8'), flush=True)
                print(f"=== FILE_CONTENT END: {f.name} ===", flush=True)
        
        # Self-verification assertions
        failures = []
        
        # Check RSS stays high after bulk delete
        if 'before_snap' in locals() and 'after_snap' in locals():
            used_mem_after = after_snap["used_memory"]
            rss_after = after_snap["used_memory_rss"]
            frag_after = after_snap["mem_fragmentation_ratio"]
            
            # used_memory should fall sharply (under 10MB)
            if used_mem_after > 15 * 1024 * 1024:
                failures.append(f"used_memory didn't drop sharply: {format_human(used_mem_after)}")
            # RSS should stay high (near the before RSS)
            rss_before = before_snap["used_memory_rss"]
            if rss_after < rss_before * 0.7:
                failures.append(f"used_memory_rss dropped too much: {format_human(rss_after)} vs before {format_human(rss_before)}")
            # fragmentation ratio should spike
            if frag_after < 5.0:
                failures.append(f"mem_fragmentation_ratio didn't spike: {frag_after}")
                
            # evicted keys should be 0
            evicted = after_snap["evicted_keys"]
            if evicted != 0:
                failures.append(f"evicted_keys is not 0: {evicted}")
                
        # Check defrag recovered RSS
        if 'defrag_snap' in locals() and 'after_snap' in locals():
            rss_defrag = defrag_snap["used_memory_rss"]
            rss_after = after_snap["used_memory_rss"]
            frag_defrag = defrag_snap["mem_fragmentation_ratio"]
            if rss_defrag >= rss_after * 0.95:
                failures.append(f"activedefrag didn't recover RSS: {format_human(rss_defrag)} vs after {format_human(rss_after)}")
            if frag_defrag > 6.0:
                failures.append(f"defrag fragmentation ratio did not settle: {frag_defrag}")
                
        # Check incremental delete peak RSS is lower
        # To verify this, we inspect the timeline records during the incremental phase
        if sampler.records:
            inc_records = [r for r in sampler.records if r["phase"] == "incremental_delete"]
            if inc_records:
                peak_inc_rss = max(r["used_memory_rss"] for r in inc_records)
                rss_after = after_snap["used_memory_rss"]
                if peak_inc_rss >= rss_after * 0.95:
                    failures.append(f"incremental delete peak RSS wasn't lower: {format_human(peak_inc_rss)} vs bulk peak {format_human(rss_after)}")
                    
        # Check OOM killed occurred
        if not oom_killed:
            failures.append("OOM Kill did not occur on the Redis container during the OOM simulation phase.")
            
        if failures:
            print("\nSelf-verification FAILURES:", flush=True)
            for fail in failures:
                print(f"FAIL: {fail}", flush=True)
            sys.exit(1)
        else:
            print("\nSelf-verification PASS: all memory behaviors and cgroup OOM rules reproduced perfectly!", flush=True)
            sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Reproduce Redis RSS fragmentation and OOM.")
    parser.add_argument("command", choices=["all", "reset"], help="Sub-command to execute")
    parser.add_argument("--keys", type=int, default=220000, help="Number of keys to load")
    parser.add_argument("--value-bytes", type=int, default=200, help="Size of each value in bytes")
    parser.add_argument("--maxmemory", default="83886080", help="maxmemory config (default 80mb)")
    parser.add_argument("--delete-batch", type=int, default=1000, help="Batch size for incremental UNLINK delete")
    parser.add_argument("--reset", action="store_true", help="Flush Redis DB before running")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS, help="Directory to write results to")
    
    args = parser.parse_args()
    
    if args.command == "reset":
        client = redis_connect()
        client.flushall()
        client.set("redis_oom_bench_marker", "redis_oom_bench_v1")
        print("Redis benchmark database flushed cleanly.")
        return
        
    if args.command == "all":
        run_experiment(args)

if __name__ == "__main__":
    main()
