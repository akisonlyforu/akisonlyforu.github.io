# Redis allocator benchmark: jemalloc vs libc

This is the harness behind [Redis Brings Its Own malloc, and Here's Why](../../collections/_posts/2026-07-18-redis-brings-its-own-malloc.md). It compiles Redis 7.4.0 twice from the same checksum-pinned source archive and digest-pinned Debian base. The only build difference is `MALLOC=jemalloc` versus `MALLOC=libc`.

The default run loads 200,000 keys across five value sizes, then performs five deterministic churn rounds. Each round overwrites 20% of the live keys with another size class, deletes 20%, and inserts the same number under fresh names. Both builds execute the same 800,000-operation plan, identified by one SHA-256 fingerprint, and finish with 200,000 data keys and zero evictions.

The result did not support the expected jemalloc RSS win. jemalloc finished at 111.54M `used_memory`, 121.71M RSS, and a 1.09 fragmentation ratio. libc finished at 97.72M `used_memory`, 107.30M RSS, and a 1.10 ratio. The ratios were effectively tied, while jemalloc used 14.41M more RSS. The no-churn control showed almost the same split, so this workload did not isolate a churn-driven external-fragmentation advantage. It also disproved the harness's initial assumption that `used_memory` would be allocator-independent: the two builds differed by 12.39% despite identical commands and logical data.

`CONFIG SET activedefrag yes` was accepted by the jemalloc build and rejected by the libc build. The jemalloc run had only a 1.01 allocator fragmentation ratio, active defragmentation was never observed running, and RSS stayed at 121.71M during the ten-second probe. This run verifies allocator support, not RSS reclamation.

These are laptop measurements for comparing one controlled workload. They are not production sizing numbers, and the mechanism transfers more reliably than the absolute megabytes.

## Run it

You need Docker with Compose v2 and Python 3.9 or newer.

```bash
cd benchmarks/redis-jemalloc
docker compose up -d --build --wait

python3 -m venv /tmp/redis-je-bench-venv
source /tmp/redis-je-bench-venv/bin/activate
pip install -r requirements.txt

# Preserve the no-churn comparison under results/attempts/.
python benchmark.py control --reset

# Run the checked-in churn shape.
python benchmark.py all --reset

docker compose down -v
```

The jemalloc build binds to loopback on host port `56380`; libc uses `56381`. Override either connection without editing the harness:

```bash
export REDIS_JE_URL='redis://127.0.0.1:56380/0'
export REDIS_LIBC_URL='redis://127.0.0.1:56381/0'
```

`--reset` is mandatory. The harness refuses to flush a non-empty database unless it contains its `redis_jemalloc_bench_marker` identity key. The two Compose services are dedicated to this benchmark.

The workload controls are `--keys`, `--rounds`, `--churn-fraction`, `--seed`, `--batch-size`, `--sample-interval`, `--settle-seconds`, and `--defrag-seconds`. Changing them creates a different experiment shape. Use `python benchmark.py all --help` for details.

## Verification gates

Before collecting data, the harness verifies that both servers report Redis 7.4.0, the first reports `jemalloc-5.3.0`, and the second reports `libc`. After writing evidence, it exits non-zero if the workload fingerprints or operation counts differ, either build does not finish with the requested data-key count, or eviction occurs.

The first implementation also treated a greater-than-5% `used_memory` difference as proof of unequal work. Both the control and churn runs tripped that gate despite using the same in-memory plan and final key count. Those failed-gate results are preserved under `results/attempts/used-memory-assumption-*`. The final gate uses the workload fingerprint, operation count, and logical key count for identity, and records the `used_memory` divergence as a result.

## Results

The harness writes these files under `results/`:

- `comparison.csv` contains one settled row per allocator, including Redis memory fields, key count, process RSS, and cgroup memory.
- `memory_timeline_je.csv` and `memory_timeline_libc.csv` contain fixed-interval samples across loading, every churn phase, and settlement.
- `run_metadata.csv` records the machine and tool versions, source tag, allocator identities, complete workload shape, SHA-256 workload fingerprint, result summary, defrag outcome, and verification status.
- `info_memory_jemalloc.txt` and `info_memory_libc.txt` are raw `INFO memory` responses at the settled checkpoint.
- `info_memory_je_defrag.txt` is the raw jemalloc response after the defrag probe.
- `attempts/no-churn-control/` contains the same evidence shape without churn.
- `attempts/used-memory-assumption-control/` and `attempts/used-memory-assumption-churn/` preserve the runs that exposed the invalid identity assumption.
