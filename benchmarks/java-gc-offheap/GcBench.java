import java.io.IOException;
import java.io.PrintWriter;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

/**
 * GcBench: reproduce the on-heap vs off-heap G1GC concurrent-marking phenomenon.
 *
 * A large population of LONG-LIVED objects pins the G1 old generation above the
 * IHOP threshold. Because the data is live, concurrent marking reclaims almost
 * nothing and G1 keeps re-initiating concurrent-mark cycles. Moving the same
 * logical data OFF-HEAP (one direct ByteBuffer slab) empties the old generation,
 * so concurrent-mark cycles collapse. The trade is that each off-heap lookup pays
 * a byte-copy/decode cost instead of a pointer dereference.
 *
 * Modes:
 *   onheap  - byte[][] slab of N live byte[] objects (strongly referenced for the run).
 *             The lookup is a true arr[key] pointer dereference.
 *   offheap - one ByteBuffer.allocateDirect(N * payload) slab, addressed by offset.
 *             The lookup copies/decodes payload bytes out of the direct buffer.
 *
 * Both modes run the IDENTICAL workload: a fixed number of iterations that each
 * allocate short-lived garbage (to drive young GC / GC pressure) and perform one
 * lookup of a deterministic pseudo-random key, folding the payload bytes into a
 * checksum so the JIT cannot elide the work. Per-lookup latency is sampled with
 * System.nanoTime() and dumped as percentiles.
 *
 * Args: <onheap|offheap> <resultsDir>
 * Tunables via env: BENCH_N, BENCH_PAYLOAD, BENCH_ITERS, BENCH_GARBAGE, BENCH_SEED, BENCH_SAMPLES
 */
public final class GcBench {

    static long envLong(String name, long dflt) {
        String v = System.getenv(name);
        if (v == null || v.isEmpty()) return dflt;
        return Long.parseLong(v.trim());
    }

    // deterministic xorshift64 (same sequence for both modes)
    static long xorshift(long x) {
        x ^= x << 13;
        x ^= x >>> 7;
        x ^= x << 17;
        return x;
    }

    public static void main(String[] args) throws IOException {
        if (args.length < 2) {
            System.err.println("usage: GcBench <onheap|offheap> <resultsDir>");
            System.exit(2);
        }
        final String mode = args[0];
        final Path resultsDir = Path.of(args[1]);
        Files.createDirectories(resultsDir);

        final int N = (int) envLong("BENCH_N", 3_500_000);       // number of long-lived entries
        final int PAYLOAD = (int) envLong("BENCH_PAYLOAD", 192);  // bytes per entry
        final long ITERS = envLong("BENCH_ITERS", 40_000_000);    // workload iterations
        final int GARBAGE = (int) envLong("BENCH_GARBAGE", 2048); // short-lived alloc bytes / iter
        final long SEED = envLong("BENCH_SEED", 0x9E3779B97F4A7C15L);
        final int SAMPLES = (int) envLong("BENCH_SAMPLES", 2_000_000); // latency samples

        final boolean onheap = mode.equals("onheap");
        final boolean offheap = mode.equals("offheap");
        if (!onheap && !offheap) {
            System.err.println("mode must be onheap or offheap");
            System.exit(2);
        }

        System.out.printf("[GcBench] mode=%s N=%d payload=%d iters=%d garbage=%d seed=%#x samples=%d%n",
                mode, N, PAYLOAD, ITERS, GARBAGE, SEED, SAMPLES);
        long liveBytesEstimate = (long) N * PAYLOAD;
        System.out.printf("[GcBench] approx payload bytes of live set: %.1f MB%n",
                liveBytesEstimate / (1024.0 * 1024.0));

        // ---- Build the long-lived population -------------------------------
        byte[][] arr = null;
        ByteBuffer slab = null;

        long t0build = System.nanoTime();
        if (onheap) {
            arr = new byte[N][];
            for (int i = 0; i < N; i++) {
                byte[] rec = new byte[PAYLOAD];
                for (int j = 0; j < PAYLOAD; j++) {
                    rec[j] = (byte) (i * 31 + j);
                }
                arr[i] = rec;
            }
        } else {
            slab = ByteBuffer.allocateDirect(N * PAYLOAD);
            byte[] rec = new byte[PAYLOAD];
            for (int i = 0; i < N; i++) {
                for (int j = 0; j < PAYLOAD; j++) {
                    rec[j] = (byte) (i * 31 + j);
                }
                slab.position(i * PAYLOAD);
                slab.put(rec, 0, PAYLOAD);
            }
        }
        long buildMs = (System.nanoTime() - t0build) / 1_000_000L;
        System.out.printf("[GcBench] built population in %d ms%n", buildMs);

        // ---- Workload ------------------------------------------------------
        final long[] latencies = new long[SAMPLES];
        final long sampleEvery = Math.max(1, ITERS / SAMPLES);
        int sampleIdx = 0;

        byte[] scratch = new byte[PAYLOAD];
        long checksum = 0L;
        long garbageSink = 0L;
        long rng = SEED;

        long t0 = System.nanoTime();
        for (long iter = 0; iter < ITERS; iter++) {
            // (a) short-lived garbage to drive GC pressure
            byte[] garbage = new byte[GARBAGE];
            garbage[0] = (byte) iter;
            garbage[GARBAGE - 1] = (byte) (iter >>> 8);
            garbageSink += garbage[0] + garbage[GARBAGE - 1];

            // deterministic key
            rng = xorshift(rng);
            int key = (int) ((rng >>> 1) % N);

            boolean sample = (iter % sampleEvery) == 0 && sampleIdx < SAMPLES;
            long ls = sample ? System.nanoTime() : 0L;

            // (b) lookup: read the payload bytes and fold into checksum
            long local = 0L;
            if (onheap) {
                byte[] rec = arr[key];
                for (int j = 0; j < PAYLOAD; j++) {
                    local += rec[j];
                }
            } else {
                int base = key * PAYLOAD;
                for (int j = 0; j < PAYLOAD; j++) {
                    scratch[j] = slab.get(base + j);
                }
                for (int j = 0; j < PAYLOAD; j++) {
                    local += scratch[j];
                }
            }
            checksum += local;

            if (sample) {
                long le = System.nanoTime();
                latencies[sampleIdx++] = le - ls;
            }
        }
        long t1 = System.nanoTime();
        double wallSec = (t1 - t0) / 1e9;
        double ops = ITERS / wallSec;

        // escape-analysis-defeating output
        System.out.printf("[GcBench] checksum=%d garbageSink=%d%n", checksum, garbageSink);
        System.out.printf("[GcBench] wall=%.3fs throughput=%.0f ops/sec%n", wallSec, ops);

        // ---- Latency percentiles ------------------------------------------
        int n = sampleIdx;
        long[] sorted = Arrays.copyOf(latencies, n);
        Arrays.sort(sorted);
        long p50 = pct(sorted, 50.0);
        long p90 = pct(sorted, 90.0);
        long p99 = pct(sorted, 99.0);
        long p999 = pct(sorted, 99.9);
        long min = n > 0 ? sorted[0] : 0;
        long max = n > 0 ? sorted[n - 1] : 0;
        double mean = 0;
        for (int i = 0; i < n; i++) mean += sorted[i];
        mean = n > 0 ? mean / n : 0;

        System.out.printf("[GcBench] lookup ns p50=%d p90=%d p99=%d p999=%d min=%d max=%d mean=%.1f%n",
                p50, p90, p99, p999, min, max, mean);

        // ---- Write CSVs ----------------------------------------------------
        Path latCsv = resultsDir.resolve("latency_" + mode + ".csv");
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(latCsv))) {
            w.println("mode,samples,p50_ns,p90_ns,p99_ns,p999_ns,min_ns,max_ns,mean_ns");
            w.printf("%s,%d,%d,%d,%d,%d,%d,%d,%.1f%n",
                    mode, n, p50, p90, p99, p999, min, max, mean);
        }

        Path perfCsv = resultsDir.resolve("perf_" + mode + ".csv");
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(perfCsv))) {
            w.println("mode,iterations,wall_seconds,throughput_ops_sec,checksum,garbage_sink,"
                    + "n_entries,payload_bytes,garbage_bytes,build_ms");
            w.printf("%s,%d,%.6f,%.2f,%d,%d,%d,%d,%d,%d%n",
                    mode, ITERS, wallSec, ops, checksum, garbageSink,
                    N, PAYLOAD, GARBAGE, buildMs);
        }

        System.out.printf("[GcBench] wrote %s and %s%n", latCsv, perfCsv);

        // keep the population reachable to the very end
        if (onheap) {
            if (arr.length != N) throw new IllegalStateException("array shrank");
        } else {
            if (slab.capacity() != N * PAYLOAD) throw new IllegalStateException("slab shrank");
        }
    }

    static long pct(long[] sorted, double p) {
        int n = sorted.length;
        if (n == 0) return 0;
        int idx = (int) Math.ceil(p / 100.0 * n) - 1;
        if (idx < 0) idx = 0;
        if (idx >= n) idx = n - 1;
        return sorted[idx];
    }
}
