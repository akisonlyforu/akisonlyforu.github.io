package lab;

import com.sun.management.OperatingSystemMXBean;

import java.io.BufferedWriter;
import java.io.IOException;
import java.lang.management.ManagementFactory;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Background daemon thread that samples process-wide CPU load roughly once a second
 * via {@code com.sun.management.OperatingSystemMXBean.getProcessCpuLoad()} (a value
 * in [0,1], the fraction of total available CPU capacity across all cores) and
 * appends a "mode,epoch_ms,cpu_load" row to a CSV shared by a bug's bad/fixed
 * variants (they run as separate processes but accumulate into the same file so the
 * two can be diffed directly).
 */
final class CpuSampler {
    private final Thread thread;
    private final AtomicBoolean running = new AtomicBoolean(true);

    CpuSampler(String csvPath, String mode, long periodMs) {
        thread = new Thread(() -> sample(csvPath, mode, periodMs), "cpu-sampler-" + mode);
        thread.setDaemon(true);
    }

    void start() {
        thread.start();
    }

    void stop() throws InterruptedException {
        running.set(false);
        thread.join();
    }

    private void sample(String csvPath, String mode, long periodMs) {
        OperatingSystemMXBean os =
                (OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();
        Path path = Paths.get(csvPath);
        boolean writeHeader;
        try {
            writeHeader = !Files.exists(path);
        } catch (Exception e) {
            writeHeader = true;
        }
        try (BufferedWriter w = Files.newBufferedWriter(path, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
            if (writeHeader) {
                w.write("mode,epoch_ms,cpu_load");
                w.newLine();
                w.flush();
            }
            while (running.get()) {
                double load = os.getProcessCpuLoad(); // [0,1] or -1 if not yet available
                long now = System.currentTimeMillis();
                w.write(mode + "," + now + "," + load);
                w.newLine();
                w.flush();
                Thread.sleep(periodMs);
            }
        } catch (IOException | InterruptedException e) {
            // best-effort sampler; nothing to recover from mid-run
        }
    }
}
