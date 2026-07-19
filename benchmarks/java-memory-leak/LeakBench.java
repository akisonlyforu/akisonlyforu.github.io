import java.io.IOException;
import java.io.PrintWriter;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import javax.management.Notification;
import javax.management.NotificationEmitter;
import javax.management.NotificationListener;
import javax.management.openmbean.CompositeData;
import com.sun.management.GarbageCollectionNotificationInfo;
import com.sun.management.GcInfo;

/**
 * LeakBench: reproduce a classic production JVM memory leak.
 *
 * The bug: a resource-holding object -- a "JobExecutor" that owns an HTTP-client-like
 * buffer plus a ThreadPoolExecutor with prestarted core worker threads -- was changed
 * by a refactor from a startup singleton to a per-request `new JobExecutor()` that is
 * never shut down. Because the pool's worker threads are alive, they are GC roots, so
 * each per-request executor's whole object graph stays reachable. Heap-after-GC climbs
 * monotonically until the JVM dies with OutOfMemoryError.
 *
 * Retention chain (why bare `new JobExecutor()` with no app reference still leaks):
 *
 *   live worker Thread
 *     -> Runnable target = ThreadPoolExecutor$Worker   (non-static inner class of TPE)
 *       -> enclosing ThreadPoolExecutor                (Worker.this$0)
 *         -> TPE.threadFactory = NamedFactory          (non-static inner class of JobExecutor)
 *           -> enclosing JobExecutor                   (NamedFactory.this$0)
 *             -> JobExecutor.buffer (byte[])           <-- the leaked payload
 *
 * As long as a core thread is alive (allowCoreThreadTimeOut(false) + prestarted), that
 * whole chain is a live GC root. No application-level reference to the JobExecutor is
 * needed. FIXED mode builds ONE JobExecutor at startup and reuses it, so the chain
 * exists exactly once and heap stays flat.
 *
 * Modes:
 *   leaky - each request does `new JobExecutor()`, uses it once, never shutdown().
 *   fixed - one shared JobExecutor built at startup, reused for every request.
 *
 * Args: <leaky|fixed> <resultsDir>
 * Tunables via env: BUFFER_KB, POOL_CORE, REPORT_EVERY, REQ_SLEEP_MS, MAX_REQUESTS,
 *                   THREAD_PREFIX.
 */
public final class LeakBench {

    // Honest "how many piled up" counter. Incremented in the constructor; NEVER
    // decremented -- in leaky mode the instances are never released, which is exactly
    // what a heap dump of the real incident showed.
    static final AtomicInteger INSTANCES = new AtomicInteger(0);

    static volatile String THREAD_PREFIX = "jobexec-pool-";

    // Latest heap-used-after-GC, captured from GC notifications (see installGcListener).
    static final AtomicLong lastHeapAfterGcBytes = new AtomicLong(0);
    static final AtomicLong gcCount = new AtomicLong(0);

    // Cached from the most recent emitRow so the death marker can be printed without
    // re-calling getAllStackTraces() (which allocates and would re-trigger OOM).
    static volatile int lastThreads = 0;
    static volatile int lastInstances = 0;
    static volatile double lastHeapMb = 0.0;

    static final MemoryMXBean MEM_BEAN = ManagementFactory.getMemoryMXBean();

    // When true (default), each report interval triggers System.gc() and then samples
    // MemoryMXBean heap-used. That yields the RETAINED LIVE SET after a full GC, which
    // is the honest "is this a leak" measurement and works even in fixed mode, where the
    // workload allocates so little that a natural GC may never fire.
    static volatile boolean forceGc = true;

    static long envLong(String name, long dflt) {
        String v = System.getenv(name);
        if (v == null || v.isEmpty()) return dflt;
        return Long.parseLong(v.trim());
    }

    static String envStr(String name, String dflt) {
        String v = System.getenv(name);
        return (v == null || v.isEmpty()) ? dflt : v;
    }

    /**
     * The leaked resource holder. Owns an HTTP-client-like buffer and a small,
     * prestarted thread pool whose live worker threads act as GC roots.
     */
    static final class JobExecutor {
        final int id;
        final byte[] buffer;                 // stands in for an HTTP client's buffers/state
        final ThreadPoolExecutor pool;

        JobExecutor(int bufferBytes, int poolCore) {
            this.id = INSTANCES.incrementAndGet();   // count on construction, never decremented
            this.buffer = new byte[bufferBytes];
            // touch a couple of pages so the buffer is really resident, not lazily zero-backed
            this.buffer[0] = (byte) id;
            this.buffer[bufferBytes - 1] = (byte) (id >>> 8);

            // NamedFactory is a NON-STATIC inner class: every thread it produces keeps
            // an implicit reference back to THIS JobExecutor via the pool's threadFactory
            // field. That is the back-reference that makes the leak faithful.
            this.pool = new ThreadPoolExecutor(
                    poolCore, poolCore,
                    0L, TimeUnit.MILLISECONDS,
                    new ArrayBlockingQueue<>(16),
                    new NamedFactory());
            this.pool.allowCoreThreadTimeOut(false);   // core threads live forever
            this.pool.prestartAllCoreThreads();        // start them now, so they are GC roots immediately
        }

        // Non-static: holds JobExecutor.this implicitly -> back-reference to buffer.
        final class NamedFactory implements ThreadFactory {
            private final AtomicInteger n = new AtomicInteger(0);
            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, THREAD_PREFIX + id + "-w" + n.incrementAndGet());
                t.setDaemon(true);   // daemon so the JVM can still exit in fixed mode
                return t;
            }
        }

        // Simulate serving one request: hand a tiny task to the pool and wait for it.
        long handle(long token) {
            try {
                Future<Long> f = pool.submit(() -> {
                    // touch the buffer so the work genuinely depends on this instance's state
                    long acc = token;
                    acc += buffer[(int) (token % buffer.length)] & 0xff;
                    acc += buffer[0] & 0xff;
                    return acc;
                });
                return f.get();
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        }
    }

    static int liveThreadCount(String prefix) {
        int c = 0;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            if (t.getName().startsWith(prefix)) c++;
        }
        return c;
    }

    static void installGcListener() {
        for (GarbageCollectorMXBean gc : ManagementFactory.getGarbageCollectorMXBeans()) {
            if (!(gc instanceof NotificationEmitter)) continue;
            NotificationEmitter emitter = (NotificationEmitter) gc;
            NotificationListener listener = new NotificationListener() {
                @Override
                public void handleNotification(Notification n, Object handback) {
                    if (!GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION
                            .equals(n.getType())) return;
                    GarbageCollectionNotificationInfo info =
                            GarbageCollectionNotificationInfo.from((CompositeData) n.getUserData());
                    GcInfo gi = info.getGcInfo();
                    Map<String, MemoryUsage> after = gi.getMemoryUsageAfterGc();
                    long heapUsed = 0;
                    for (Map.Entry<String, MemoryUsage> e : after.entrySet()) {
                        String pool = e.getKey();
                        // sum the heap pools; skip the non-heap Metaspace / CodeCache pools
                        if (pool.contains("Eden") || pool.contains("Survivor")
                                || pool.contains("Old") || pool.contains("Tenured")
                                || pool.contains("Heap")) {
                            heapUsed += e.getValue().getUsed();
                        }
                    }
                    lastHeapAfterGcBytes.set(heapUsed);
                    gcCount.incrementAndGet();
                }
            };
            emitter.addNotificationListener(listener, null, null);
        }
    }

    public static void main(String[] args) throws IOException {
        if (args.length < 2) {
            System.err.println("usage: LeakBench <leaky|fixed> <resultsDir>");
            System.exit(2);
        }
        final String mode = args[0];
        final Path resultsDir = Path.of(args[1]);
        Files.createDirectories(resultsDir);

        final boolean leaky = mode.equals("leaky");
        final boolean fixed = mode.equals("fixed");
        if (!leaky && !fixed) {
            System.err.println("mode must be leaky or fixed");
            System.exit(2);
        }

        final int bufferBytes = (int) envLong("BUFFER_KB", 512) * 1024;
        final int poolCore = (int) envLong("POOL_CORE", 2);
        final long reportEvery = envLong("REPORT_EVERY", 5);
        final long reqSleepMs = envLong("REQ_SLEEP_MS", 250);
        // fixed mode stops after MAX_REQUESTS; leaky mode runs until OOM (MAX_REQUESTS
        // is just a safety ceiling so a mis-tuned run cannot loop forever).
        final long maxRequests = envLong("MAX_REQUESTS", 2_000_000);
        THREAD_PREFIX = envStr("THREAD_PREFIX", "jobexec-pool-");
        forceGc = envLong("FORCE_GC", 1) != 0;

        installGcListener();

        System.out.printf("[leak] mode=%s bufferKB=%d poolCore=%d reportEvery=%d "
                + "reqSleepMs=%d maxRequests=%d forceGc=%s threadPrefix=%s%n",
                mode, bufferBytes / 1024, poolCore, reportEvery, reqSleepMs,
                maxRequests, forceGc, THREAD_PREFIX);
        System.out.println("[leak] heap_used_after_gc_mb = MemoryMXBean heap used, "
                + "sampled immediately after a System.gc()-triggered full GC");

        Path csv = resultsDir.resolve(mode + ".csv");
        PrintWriter w = new PrintWriter(Files.newBufferedWriter(csv));
        w.println("requests_served,heap_used_after_gc_mb,live_thread_count,jobexecutor_instances,gc_count,elapsed_s");
        w.flush();

        // fixed mode: build ONE executor at startup and reuse it forever.
        JobExecutor shared = fixed ? new JobExecutor(bufferBytes, poolCore) : null;

        long requests = 0;
        long start = System.nanoTime();
        long checksum = 0;
        String deathCause = null;

        try {
            while (requests < maxRequests) {
                JobExecutor exec;
                if (leaky) {
                    // THE BUG: a fresh executor per request, used once, never shut down,
                    // no application-level reference kept. It leaks purely because its
                    // prestarted worker threads keep the whole graph reachable.
                    exec = new JobExecutor(bufferBytes, poolCore);
                } else {
                    exec = shared;
                }

                checksum += exec.handle(requests);
                requests++;

                if (requests % reportEvery == 0) {
                    emitRow(w, requests, start);
                }
                if (reqSleepMs > 0) {
                    try {
                        Thread.sleep(reqSleepMs);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                    }
                }
            }
        } catch (OutOfMemoryError oom) {
            deathCause = oom.getClass().getSimpleName() + ": " + oom.getMessage();
            // Best-effort final row + marker. May fail under heap pressure; the flushed
            // CSV already holds the climb up to this point, and the JVM will also print
            // the OutOfMemoryError to stderr, so the death is captured either way.
            try {
                emitRow(w, requests, start);
                w.flush();
            } catch (Throwable ignore) {
            }
            // Use cached counters (no getAllStackTraces) so this print does not OOM again.
            System.out.printf("[leak] DIED mode=%s requests=%d heap_after_gc_mb=%.1f "
                    + "live_threads=%d instances=%d gc_count=%d elapsed_s=%.1f cause=%s%n",
                    mode, requests, lastHeapMb, lastThreads, lastInstances, gcCount.get(),
                    (System.nanoTime() - start) / 1e9, deathCause);
            System.out.printf("[leak] checksum=%d%n", checksum);
            try {
                w.flush();
                w.close();
            } catch (Throwable ignore) {
            }
            System.exit(137);
        }

        // fixed mode reaches here (leaky never should unless MAX_REQUESTS hit).
        emitRow(w, requests, start);
        w.flush();
        w.close();
        System.out.printf("[leak] SURVIVED mode=%s requests=%d heap_after_gc_mb=%.1f "
                + "live_threads=%d instances=%d gc_count=%d elapsed_s=%.1f%n",
                mode, requests, lastHeapMb, lastThreads, lastInstances, gcCount.get(),
                (System.nanoTime() - start) / 1e9);
        System.out.printf("[leak] checksum=%d%n", checksum);
    }

    static void emitRow(PrintWriter w, long requests, long start) {
        // Force a full GC, then read the heap that survived it: the retained live set.
        long heapBytes;
        if (forceGc) {
            System.gc();
            heapBytes = MEM_BEAN.getHeapMemoryUsage().getUsed();
        } else {
            heapBytes = lastHeapAfterGcBytes.get();
        }
        double heapMb = heapBytes / (1024.0 * 1024.0);
        int threads = liveThreadCount(THREAD_PREFIX);
        int instances = INSTANCES.get();
        lastHeapMb = heapMb;
        lastThreads = threads;
        lastInstances = instances;
        double elapsed = (System.nanoTime() - start) / 1e9;
        w.printf("%d,%.2f,%d,%d,%d,%.2f%n", requests, heapMb, threads, instances,
                gcCount.get(), elapsed);
        w.flush();
        System.out.printf("[leak] served=%d heap_after_gc_mb=%.1f threads=%d instances=%d gc=%d t=%.1fs%n",
                requests, heapMb, threads, instances, gcCount.get(), elapsed);
    }
}
