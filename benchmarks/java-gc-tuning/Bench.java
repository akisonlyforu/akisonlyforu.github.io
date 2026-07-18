/*
 * A tiny "service" allocation workload for comparing JVM garbage collectors.
 *
 * It keeps a bounded in-memory cache (the live working set that sits in old gen)
 * and runs a request loop. Each request allocates a few KB of short-lived garbage,
 * touches a cache entry, and every Nth request replaces a cache entry with a fresh
 * payload -- that replacement is what drives promotion into the old generation and,
 * eventually, old/mixed/full collections.
 *
 * Every request is timed with System.nanoTime(). Stop-the-world GC pauses land on
 * top of whatever request happens to be running, so they show up as latency spikes
 * in the tail. Percentiles come from a fixed microsecond-bucket histogram (no
 * per-request allocation, so the measurement itself doesn't perturb the heap).
 *
 * Output:
 *   - stdout: one parseable METRICS line (request-latency percentiles + throughput)
 *   - stderr: the GC log (-Xlog:gc), so the Python driver can parse Pause lines and
 *             cross-check the in-app tail against real STW pause times.
 *
 * All knobs are environment variables so the driver can sweep a matrix:
 *   SEED, LIVE_ENTRIES, PAYLOAD_KB, GARBAGE_KB, CHURN_EVERY, OPS, WARMUP_OPS
 */
public class Bench {

    // deterministic, allocation-free xorshift64 so the workload is identical run-to-run
    private static long xs;
    private static long rnd() {
        xs ^= xs << 13;
        xs ^= xs >>> 7;
        xs ^= xs << 17;
        return xs;
    }
    private static int rndIdx(int bound) {
        long r = rnd();
        if (r < 0) r = -(r + 1); // avoid Long.MIN_VALUE overflow, stay non-negative
        return (int) (r % bound);
    }

    private static long envL(String k, long def) {
        String v = System.getenv(k);
        if (v == null || v.isEmpty()) return def;
        return Long.parseLong(v.trim());
    }

    // one simulated request: allocate garbage, touch a cache entry, occasionally
    // replace one (that replacement is the promotion source). Returns a checksum so
    // the JIT can't optimise the allocations away.
    private static long request(byte[][] cache, int payloadBytes, int garbageBytes,
                                long op, long churnEvery) {
        byte[] g = new byte[garbageBytes];
        int idx = rndIdx(cache.length);
        g[0] = (byte) idx;
        g[garbageBytes - 1] = (byte) op;
        long sum = g[0] + g[garbageBytes - 1];

        byte[] c = cache[idx];
        sum += c[0] + c[payloadBytes - 1];

        if (op % churnEvery == 0) {
            byte[] fresh = new byte[payloadBytes];
            fresh[0] = (byte) op;
            fresh[payloadBytes - 1] = (byte) idx;
            cache[idx] = fresh; // the old payload is now garbage in old gen
            sum += fresh[0];
        }
        return sum;
    }

    // exact percentile from the microsecond histogram
    private static double pct(long[] hist, long total, double q) {
        long target = (long) Math.ceil(q * total);
        long acc = 0;
        for (int i = 0; i < hist.length; i++) {
            acc += hist[i];
            if (acc >= target) return i; // microseconds
        }
        return hist.length;
    }

    public static void main(String[] args) {
        long seed        = envL("SEED", 42);
        int liveEntries  = (int) envL("LIVE_ENTRIES", 3000);
        int payloadKb    = (int) envL("PAYLOAD_KB", 50);
        int garbageKb    = (int) envL("GARBAGE_KB", 8);
        long churnEvery  = envL("CHURN_EVERY", 8);
        long measureOps  = envL("OPS", 5_000_000L);
        long warmupOps   = envL("WARMUP_OPS", 1_000_000L);

        xs = (seed == 0) ? 0x9E3779B97F4A7C15L : seed;

        int payloadBytes = payloadKb * 1024;
        int garbageBytes = garbageKb * 1024;

        // build the live working set (this is what occupies the old generation)
        byte[][] cache = new byte[liveEntries][];
        for (int i = 0; i < liveEntries; i++) {
            cache[i] = new byte[payloadBytes];
            cache[i][0] = (byte) i;
            cache[i][payloadBytes - 1] = (byte) i;
        }
        long liveBytes = (long) liveEntries * payloadBytes;

        // fixed microsecond-resolution histogram, capped at 2s. 2M+1 longs = ~16MB,
        // allocated once, never touched by the GC-sensitive inner loop.
        final int CAP = 2_000_000;
        long[] hist = new long[CAP + 1];
        long maxNs = 0;
        long checksum = 0;

        // warmup: reach JIT steady state and a filled/churned heap before measuring
        for (long op = 0; op < warmupOps; op++) {
            checksum += request(cache, payloadBytes, garbageBytes, op, churnEvery);
        }

        // measure
        long startWall = System.nanoTime();
        for (long op = 0; op < measureOps; op++) {
            long t0 = System.nanoTime();
            checksum += request(cache, payloadBytes, garbageBytes, op, churnEvery);
            long dt = System.nanoTime() - t0;
            if (dt > maxNs) maxNs = dt;
            int us = (int) (dt / 1000);
            if (us > CAP) us = CAP;
            hist[us]++;
        }
        long endWall = System.nanoTime();
        double wallS = (endWall - startWall) / 1e9;

        double p50  = pct(hist, measureOps, 0.50);
        double p99  = pct(hist, measureOps, 0.99);
        double p999 = pct(hist, measureOps, 0.999);
        double maxUs = maxNs / 1000.0;
        double thr = measureOps / wallS;
        double allocMb = ((double) measureOps * garbageBytes
                + (double) (measureOps / churnEvery) * payloadBytes) / (1024.0 * 1024.0);

        System.out.printf(
            "METRICS ops=%d wall_s=%.3f throughput_ops_s=%.0f "
            + "p50_us=%.1f p99_us=%.1f p999_us=%.1f max_us=%.1f "
            + "live_mb=%.1f alloc_mb=%.0f checksum=%d%n",
            measureOps, wallS, thr, p50, p99, p999, maxUs,
            liveBytes / (1024.0 * 1024.0), allocMb, checksum);
    }
}
