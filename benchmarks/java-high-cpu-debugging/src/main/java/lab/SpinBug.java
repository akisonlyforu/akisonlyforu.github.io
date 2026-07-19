package lab;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Bug 2: busy-spin consumer polling a mostly-empty queue.
 *
 * Kafka-consumer-idle-poll style: one consumer thread per (mostly empty) queue.
 *
 * "spin-bad" calls the non-blocking {@code queue.poll()} in a hot {@code while(true)}
 * with no sleep/backoff -- when the queue is empty (nearly always, here) that call
 * returns immediately and the loop spins right back around, burning its core at
 * ~100% doing nothing.
 *
 * "spin-fixed" calls the blocking {@code queue.poll(50, TimeUnit.MILLISECONDS)}
 * instead -- when the queue is empty the thread parks (LockSupport.park under the
 * hood) and consumes ~0 CPU until either an item arrives or the timeout elapses.
 *
 * Uses one consumer thread per available core (10 on this host) so the aggregate
 * process CPU load in the bad case actually approaches 100% instead of just 1/N of
 * it -- this is the most dramatic contrast of the three bugs by design.
 */
final class SpinBug {
    private static final int DURATION_SEC = Integer.getInteger("lab.durationSec", 35);

    static void run(boolean fixed, String resultsDir) throws Exception {
        String mode = fixed ? "spin-fixed" : "spin-bad";
        String csv = resultsDir + "/spin_cpu.csv";
        CpuSampler sampler = new CpuSampler(csv, mode, 1000);
        sampler.start();

        int nThreads = Math.max(1, Runtime.getRuntime().availableProcessors());
        List<BlockingQueue<Integer>> queues = new ArrayList<>();
        for (int i = 0; i < nThreads; i++) {
            queues.add(new ArrayBlockingQueue<>(1000));
        }

        AtomicBoolean running = new AtomicBoolean(true);
        AtomicLong totalPolls = new AtomicLong();
        AtomicLong itemsProcessed = new AtomicLong();

        List<Thread> consumers = new ArrayList<>();
        for (int i = 0; i < nThreads; i++) {
            BlockingQueue<Integer> q = queues.get(i);
            Thread t = new Thread(() -> {
                long polls = 0;
                long processed = 0;
                while (running.get()) {
                    try {
                        Integer item = fixed ? q.poll(50, TimeUnit.MILLISECONDS) : q.poll();
                        polls++;
                        if (item != null) processed++;
                    } catch (InterruptedException e) {
                        break;
                    }
                }
                totalPolls.addAndGet(polls);
                itemsProcessed.addAndGet(processed);
            }, "consumer-" + i);
            t.start();
            consumers.add(t);
        }

        // low-rate producer mimicking a mostly-idle topic: one item every ~500ms per queue
        Thread producer = new Thread(() -> {
            int i = 0;
            while (running.get()) {
                try {
                    queues.get(i % queues.size()).offer(i);
                    i++;
                    Thread.sleep(500);
                } catch (InterruptedException e) {
                    break;
                }
            }
        }, "producer");
        producer.setDaemon(true);
        producer.start();

        Thread.sleep(DURATION_SEC * 1000L);
        running.set(false);
        producer.interrupt();
        for (Thread t : consumers) {
            t.join();
        }

        sampler.stop();

        double elapsedS = DURATION_SEC;
        double pollsPerSec = totalPolls.get() / elapsedS;
        Utils.appendThroughputRow(resultsDir, mode, "polls_per_sec", totalPolls.get(), elapsedS, pollsPerSec,
                "threads=" + nThreads + " items_processed=" + itemsProcessed.get());
        System.out.printf("[%s] threads=%d polls=%d items=%d throughput=%.1f polls/sec%n",
                mode, nThreads, totalPolls.get(), itemsProcessed.get(), pollsPerSec);
    }
}
