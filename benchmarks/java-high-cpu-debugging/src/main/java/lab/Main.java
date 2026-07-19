package lab;

import java.nio.file.Files;
import java.nio.file.Paths;

/**
 * Entry point for the high-CPU reproduction lab. Dispatches on args[0] to one of six
 * fixed-duration workloads, each demonstrating a classic "CPU pegged, throughput
 * collapsed" bug plus its fix:
 *
 *   regex-bad / regex-fixed          unanchored ".*ERROR.*" backtracking vs contains()
 *   spin-bad  / spin-fixed           busy-spin queue.poll() vs blocking poll(timeout)
 *   hibernate-bad / hibernate-fixed  per-call AUTO-flush dirty-check scan vs FlushMode.COMMIT
 *
 * results dir resolution: args[1] if present, else env RESULTS_DIR, else "./results"
 * relative to the current working directory (so `java -cp target/*.jar lab.Main
 * regex-bad` run from the project root just works).
 */
public class Main {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            usage();
            System.exit(2);
        }
        String mode = args[0];
        String resultsDir = args.length >= 2
                ? args[1]
                : System.getenv().getOrDefault("RESULTS_DIR", "results");
        Files.createDirectories(Paths.get(resultsDir));

        System.out.println("mode=" + mode + " resultsDir=" + resultsDir + " pid="
                + ProcessHandle.current().pid());

        switch (mode) {
            case "regex-bad" -> RegexBug.run(false, resultsDir);
            case "regex-fixed" -> RegexBug.run(true, resultsDir);
            case "spin-bad" -> SpinBug.run(false, resultsDir);
            case "spin-fixed" -> SpinBug.run(true, resultsDir);
            case "hibernate-bad" -> HibernateBug.run(false, resultsDir);
            case "hibernate-fixed" -> HibernateBug.run(true, resultsDir);
            default -> {
                System.err.println("unknown mode: " + mode);
                usage();
                System.exit(2);
            }
        }
    }

    private static void usage() {
        System.err.println("usage: lab.Main <mode> [resultsDir]");
        System.err.println("modes: regex-bad regex-fixed spin-bad spin-fixed hibernate-bad hibernate-fixed");
        System.err.println("resultsDir also settable via RESULTS_DIR env var (default: ./results)");
    }
}
