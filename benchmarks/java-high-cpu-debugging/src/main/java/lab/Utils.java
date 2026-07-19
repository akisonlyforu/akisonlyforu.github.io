package lab;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;

/** Shared helpers for the bug workloads. */
final class Utils {
    private Utils() {}

    /** Appends one throughput/latency summary row (shared across all 6 modes). */
    static synchronized void appendThroughputRow(String resultsDir, String mode, String opsLabel,
            long totalOps, double elapsedS, double opsPerSec, String note) throws IOException {
        Path path = Paths.get(resultsDir, "throughput.csv");
        boolean writeHeader = !Files.exists(path);
        try (BufferedWriter w = Files.newBufferedWriter(path, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
            if (writeHeader) {
                w.write("mode,ops_label,total_ops,elapsed_s,ops_per_sec,note");
                w.newLine();
            }
            // fixed-point, not Double.toString()'s default -- large throughputs (e.g. spin-bad's
            // ~1e9 polls/sec) would otherwise render as "1.234E9" in the CSV
            String opsPerSecFmt = String.format(java.util.Locale.ROOT, "%.3f", opsPerSec);
            w.write(mode + "," + opsLabel + "," + totalOps + "," + elapsedS + "," + opsPerSecFmt + "," + note);
            w.newLine();
        }
    }
}
