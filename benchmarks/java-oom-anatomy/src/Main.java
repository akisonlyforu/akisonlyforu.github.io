import java.lang.management.BufferPoolMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.MemoryUsage;
import java.io.PrintWriter;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * java-oom-anatomy: four ways a JVM reaches java.lang.OutOfMemoryError, each a
 * DIFFERENT failure wearing one name.
 *
 *   leak       -> OutOfMemoryError: Java heap space            (retained allocation)
 *   healthy    -> no OOM                                       (same rate, released)
 *   gcoverhead -> OutOfMemoryError: GC overhead limit exceeded (Parallel GC, near-full)
 *   metaspace  -> OutOfMemoryError: Metaspace                  (class-metadata leak)
 *   directbuffer -> OutOfMemoryError: Direct buffer memory     (off-heap NIO buffers)
 *
 * Usage: java <jvm-flags> Main <mode> <resultsDir>
 * Tunables via env: BLOCK_KB, SLEEP_MS, HEALTHY_ITER, GCO_FILL_PCT, GCO_NODE_BYTES,
 * META_SAMPLE_EVERY, META_SLEEP_MS.
 */
public class Main {

    static int envInt(String k, int def) {
        String v = System.getenv(k);
        try { return v == null ? def : Integer.parseInt(v.trim()); }
        catch (NumberFormatException e) { return def; }
    }

    static long heapUsedMb() {
        MemoryUsage u = ManagementFactory.getMemoryMXBean().getHeapMemoryUsage();
        return u.getUsed() / (1024 * 1024);
    }

    static MemoryPoolMXBean pool(String name) {
        for (MemoryPoolMXBean p : ManagementFactory.getMemoryPoolMXBeans()) {
            if (name.equals(p.getName())) return p;
        }
        return null;
    }

    static long usedMb(MemoryPoolMXBean p) {
        return p == null ? -1 : p.getUsage().getUsed() / (1024 * 1024);
    }

    static BufferPoolMXBean bufferPool(String name) {
        for (BufferPoolMXBean b : ManagementFactory.getPlatformMXBeans(BufferPoolMXBean.class)) {
            if (name.equals(b.getName())) return b;
        }
        return null;
    }

    // ---- E: direct buffer memory ----------------------------------------------
    // Off-heap NIO buffers. The Java wrapper object is a few dozen bytes; the actual
    // bytes live in native memory that is only released when the wrapper becomes
    // unreachable and its Cleaner runs. Retain the wrappers and that native memory
    // can never come back -- and none of it appears in a heap dump, because none of
    // it is on the heap. Capped by -XX:MaxDirectMemorySize, not by -Xmx.
    static final List<ByteBuffer> BUFFERS = new ArrayList<>();

    static void directBuffer(Path resultsDir) throws Exception {
        int blockKb = envInt("DIRECT_BLOCK_KB", 512);
        int sleepMs = envInt("DIRECT_SLEEP_MS", 10);
        int block = blockKb * 1024;
        BufferPoolMXBean direct = bufferPool("direct");
        long t0 = System.nanoTime();
        Path csv = resultsDir.resolve("directbuffer.csv");
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(csv))) {
            w.println("sample_s,direct_used_mb,direct_count,heap_used_mb");
            w.flush();
            long n = 0;
            while (true) {
                ByteBuffer b = ByteBuffer.allocateDirect(block);
                b.put(0, (byte) 1);
                BUFFERS.add(b);              // <-- retain the wrapper: Cleaner never runs
                n++;
                double s = (System.nanoTime() - t0) / 1e9;
                long dMb = direct == null ? -1 : direct.getMemoryUsed() / (1024 * 1024);
                long dCount = direct == null ? -1 : direct.getCount();
                long heapMb = heapUsedMb();
                w.printf("%.2f,%d,%d,%d%n", s, dMb, dCount, heapMb);
                w.flush();
                System.out.printf("directbuffer: buffers=%d direct=%d MB heapUsed=%d MB t=%.1fs%n",
                        n, dMb, heapMb, s);
                if (sleepMs > 0) Thread.sleep(sleepMs);
            }
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("usage: Main <leak|healthy|gcoverhead|metaspace|directbuffer> <resultsDir>");
            System.exit(2);
        }
        String mode = args[0];
        Path resultsDir = Path.of(args[1]);
        System.out.println("mode=" + mode + " pid=" + ProcessHandle.current().pid());

        try {
            switch (mode) {
                case "leak":       leak(); break;
                case "healthy":    healthy(); break;
                case "gcoverhead": gcOverhead(); break;
                case "metaspace":  metaspace(resultsDir); break;
                case "directbuffer": directBuffer(resultsDir); break;
                default:
                    System.err.println("unknown mode: " + mode);
                    System.exit(2);
            }
        } catch (OutOfMemoryError oom) {
            // Under a truly exhausted heap the DEFAULT uncaught handler can itself
            // fail to allocate the strings needed to print its stack trace, so the
            // exact message gets swallowed. Classic fix: keep a small EMERGENCY
            // RESERVE alive, release ONLY that in the handler to buy just enough
            // headroom to report -- WITHOUT freeing the retained roots (freeing them
            // would trigger a big cleanup GC that pollutes the death-spiral tail of
            // the GC log). The OOM genuinely fired; this is a well-behaved uncaught
            // handler, the kind a real service logs from.
            RESERVE = null;
            System.err.println(oom);                    // exact: java.lang.OutOfMemoryError: <region>
            StackTraceElement[] st = oom.getStackTrace();
            if (st.length > 0) System.err.println("\tat " + st[0]);
            oom.printStackTrace();
            System.exit(1);
        }
    }

    // Released in the OOM handler to buy headroom to print the message/stack.
    static byte[] RESERVE = new byte[8 * 1024 * 1024];

    // ---- A: heap-space death spiral -------------------------------------------
    // Retain every allocated block in a static collection under a capped heap.
    // Post-GC live set climbs toward the ceiling; the JVM dies with "Java heap space".
    static final List<byte[]> ROOTS = new ArrayList<>();

    static void leak() throws InterruptedException {
        int blockKb = envInt("BLOCK_KB", 32);
        int sleepMs = envInt("SLEEP_MS", 3);
        int block = blockKb * 1024;
        long t0 = System.nanoTime();
        long count = 0;
        while (true) {
            byte[] b = new byte[block];
            b[0] = 1; b[block - 1] = 2;          // touch so it can't be optimized away
            ROOTS.add(b);                        // <-- the leak: never released
            count++;
            if (count % 200 == 0) {
                System.out.printf("leak: retained=%d blocks (%d MB nominal) heapUsed=%d MB t=%.1fs%n",
                        count, count * blockKb / 1024, heapUsedMb(), (System.nanoTime() - t0) / 1e9);
            }
            if (sleepMs > 0) Thread.sleep(sleepMs);
        }
    }

    // ---- B: healthy churn (the contrast) --------------------------------------
    // SAME block size + pace, but blocks are NOT retained. Post-GC heap stays flat.
    // Runs to completion; no OOM.
    static volatile byte[] sink;   // holds only the most recent block

    static void healthy() throws InterruptedException {
        int blockKb = envInt("BLOCK_KB", 32);
        int sleepMs = envInt("SLEEP_MS", 3);
        int iters = envInt("HEALTHY_ITER", 8000);
        int block = blockKb * 1024;
        long t0 = System.nanoTime();
        for (int i = 1; i <= iters; i++) {
            byte[] b = new byte[block];
            b[0] = 1; b[block - 1] = 2;
            sink = b;                            // released next iteration -> collectible
            if (i % 200 == 0) {
                System.out.printf("healthy: churned=%d/%d blocks (%d MB total) heapUsed=%d MB t=%.1fs%n",
                        i, iters, (long) i * blockKb / 1024, heapUsedMb(), (System.nanoTime() - t0) / 1e9);
            }
            if (sleepMs > 0) Thread.sleep(sleepMs);
        }
        System.out.printf("healthy: DONE churned %d blocks (%d MB total) in %.1fs, no OOM%n",
                iters, (long) iters * blockKb / 1024, (System.nanoTime() - t0) / 1e9);
    }

    // ---- C: GC overhead limit exceeded ----------------------------------------
    // Parallel GC, small heap. A near-full heap held live in a fixed-size RING; each
    // iteration overwrites one slot with a fresh object, so exactly ~one node of
    // garbage exists per allocation and the heap stays pinned at ~fillPct% forever.
    // Every collection must trace the whole live ring (expensive) and gets back only
    // that one node (nearly nothing): ~98% of wall time in GC, <2% of heap recovered
    // -- the exact ratio the GC overhead tripwire watches for, and it fires BEFORE
    // the heap is technically exhausted. This is the "legitimately near-full heap"
    // every long-lived service drifts into: caches, sessions, an oversized working
    // set that keeps getting touched.
    static Object[] RING;

    static void gcOverhead() {
        int fillPct = envInt("GCO_FILL_PCT", 90);
        int nodeBytes = envInt("GCO_NODE_BYTES", 512);
        long max = Runtime.getRuntime().maxMemory();
        long target = max / 100 * fillPct;
        long t0 = System.nanoTime();

        int slots = (int) (target / nodeBytes);
        RING = new Object[slots];
        System.out.printf("gcoverhead: ring slots=%d nodeBytes=%d target=%d MB of max %d MB%n",
                slots, nodeBytes, target / (1024 * 1024), max / (1024 * 1024));
        // Phase 1: fill the ring so the heap sits at ~fillPct%.
        for (int i = 0; i < slots; i++) {
            byte[] node = new byte[nodeBytes];
            node[0] = 1;
            RING[i] = node;
        }
        System.out.printf("gcoverhead: ring filled, heapUsed=%d MB, t=%.1fs -- entering steady overwrite%n",
                heapUsedMb(), (System.nanoTime() - t0) / 1e9);
        // Phase 2: overwrite one slot per iteration forever. Heap never grows; GC
        // reclaims ~one node per cycle against a fully-traced live set.
        long churn = 0;
        int i = 0;
        while (true) {
            byte[] node = new byte[nodeBytes];
            node[0] = 1;
            RING[i] = node;                      // previous occupant becomes the only garbage
            i++; if (i >= slots) i = 0;
            churn++;
            if (churn % 1000000 == 0) {
                System.out.printf("gcoverhead: overwrites=%d heapUsed=%d MB t=%.1fs%n",
                        churn, heapUsedMb(), (System.nanoTime() - t0) / 1e9);
            }
        }
    }

    // ---- D: Metaspace OOM (the one that isn't heap) ---------------------------
    // Load many distinct runtime klasses (same name, fresh classloader each time,
    // loaders retained). Heap stays flat while Metaspace climbs to its wall.
    static final class InMemoryLoader extends ClassLoader {
        final byte[] bytes;
        InMemoryLoader(byte[] bytes) { super(null); this.bytes = bytes; }
        Class<?> defineLeak() { return defineClass("Leak", bytes, 0, bytes.length); }
    }

    static final List<ClassLoader> LOADERS = new ArrayList<>();

    static void metaspace(Path resultsDir) throws Exception {
        int sampleEvery = envInt("META_SAMPLE_EVERY", 200);
        int sleepMs = envInt("META_SLEEP_MS", 1);
        byte[] bytes = Files.readAllBytes(Path.of("/app/Leak.class"));
        // MaxMetaspaceSize caps class metadata as a WHOLE, which HotSpot exposes as
        // two pools: "Metaspace" (non-class metadata) and "Compressed Class Space"
        // (the klass pointers). Record both, and their sum -- the sum is what walks
        // into the -XX:MaxMetaspaceSize wall.
        MemoryPoolMXBean meta = pool("Metaspace");
        MemoryPoolMXBean ccs = pool("Compressed Class Space");
        long t0 = System.nanoTime();
        Path csv = resultsDir.resolve("metaspace.csv");
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(csv))) {
            w.println("sample_s,metaspace_used_mb,compressed_class_used_mb,metadata_total_mb,heap_used_mb,classes_loaded");
            w.flush();
            long loaded = 0;
            while (true) {
                InMemoryLoader cl = new InMemoryLoader(bytes);
                Class<?> k = cl.defineLeak();
                k.getDeclaredMethods();          // force the klass to fully materialize
                LOADERS.add(cl);                 // <-- retain the loader (and its klass)
                loaded++;
                if (loaded % sampleEvery == 0) {
                    double s = (System.nanoTime() - t0) / 1e9;
                    long metaMb = usedMb(meta), ccsMb = usedMb(ccs), heapMb = heapUsedMb();
                    w.printf("%.2f,%d,%d,%d,%d,%d%n", s, metaMb, ccsMb, metaMb + ccsMb, heapMb, loaded);
                    w.flush();
                    System.out.printf("metaspace: classes=%d meta=%d MB ccs=%d MB total=%d MB heapUsed=%d MB t=%.1fs%n",
                            loaded, metaMb, ccsMb, metaMb + ccsMb, heapMb, s);
                }
                if (sleepMs > 0) Thread.sleep(sleepMs);
            }
        }
    }
}
