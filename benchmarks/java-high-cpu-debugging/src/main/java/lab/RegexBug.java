package lab;

import java.util.concurrent.ThreadLocalRandom;

/**
 * Bug 1: unanchored regex full-match on a hot path.
 *
 * "regex-bad" mirrors a classic log-scanning mistake: someone wants "does this line
 * contain ERROR" and writes {@code line.matches(".*ERROR.*")}. String.matches()
 * already requires the whole input to match, so the leading/trailing ".*" are
 * redundant -- but they aren't free. Java's regex engine (Pattern/Matcher) is a
 * backtracking NFA, not a DFA: for every line that does NOT contain "ERROR" (the
 * common case in real logs), it has to try anchoring the literal "ERROR" at every
 * possible offset before it can conclude failure, backtracking the greedy ".*" one
 * character at a time. That's O(n^2)-ish work per line instead of the O(n) a plain
 * substring scan needs.
 *
 * "regex-fixed" does the same semantic check (contains "ERROR" anywhere in the line)
 * with {@code String.contains()} -- a single linear scan, no backtracking, no engine
 * overhead at all.
 *
 * Both variants run a tight loop for a fixed wall-clock duration and report
 * throughput (lines/sec) -- that's where the contrast shows up; process CPU load
 * stays similar (both keep one core continuously busy), but "bad" does far fewer
 * lines per second for the same CPU budget.
 */
final class RegexBug {
    private static final int DURATION_SEC = Integer.getInteger("lab.durationSec", 35);
    private static final String[] LEVELS = {"INFO", "DEBUG", "WARN", "TRACE"};
    private static final String[] SERVICES =
            {"checkout-svc", "auth-svc", "inventory-svc", "payments-svc", "search-svc"};

    static void run(boolean fixed, String resultsDir) throws Exception {
        String mode = fixed ? "regex-fixed" : "regex-bad";
        String csv = resultsDir + "/regex_cpu.csv";
        CpuSampler sampler = new CpuSampler(csv, mode, 1000);
        sampler.start();

        // pre-generate a pool of synthetic log lines so string construction cost
        // doesn't dominate either variant's measurement
        String[] pool = new String[2000];
        ThreadLocalRandom rnd = ThreadLocalRandom.current();
        for (int i = 0; i < pool.length; i++) {
            pool[i] = syntheticLine(rnd);
        }

        long endAt = System.currentTimeMillis() + DURATION_SEC * 1000L;
        long total = 0;
        long matched = 0;
        int idx = 0;

        while (System.currentTimeMillis() < endAt) {
            String line = pool[idx];
            idx = (idx + 1 == pool.length) ? 0 : idx + 1;

            boolean isMatch = fixed
                    ? line.contains("ERROR")                 // O(n) single pass
                    : line.matches(".*ERROR.*");              // unanchored, backtracking-heavy full match

            if (isMatch) matched++;
            total++;
        }

        sampler.stop();

        double elapsedS = DURATION_SEC;
        double opsPerSec = total / elapsedS;
        Utils.appendThroughputRow(resultsDir, mode, "lines_matched_per_sec", total, elapsedS, opsPerSec,
                "matched=" + matched);
        System.out.printf("[%s] lines=%d matched=%d throughput=%.1f lines/sec%n", mode, total, matched, opsPerSec);
    }

    private static String syntheticLine(ThreadLocalRandom rnd) {
        String level = LEVELS[rnd.nextInt(LEVELS.length)];
        String svc = SERVICES[rnd.nextInt(SERVICES.length)];
        // ~5% real ERROR lines, like a healthy-ish service log
        boolean isError = rnd.nextInt(100) < 5;
        String msg = isError
                ? "ERROR request failed with status=500 upstream timeout after 3000ms retries=3 circuit=open"
                : "request completed status=200 latency_ms=" + rnd.nextInt(500)
                        + " cache=hit pool=warm queue_depth=" + rnd.nextInt(20);
        return "2026-07-20T12:34:56.789Z " + level + " " + svc + " [thread-" + rnd.nextInt(64) + "] " + msg
                + " trace_id=" + Long.toHexString(rnd.nextLong()) + " span_id=" + Long.toHexString(rnd.nextLong());
    }
}
