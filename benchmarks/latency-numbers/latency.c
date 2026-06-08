/*
 * latency.c — native latency microbenchmarks for
 * "Latency Numbers Every Programmer Should Know", remeasured on this host.
 *
 * Runs natively (NOT in Docker) so cache/DRAM/SSD latencies are the real host
 * memory hierarchy, not a Linux VM. See README.
 *
 * Build:  clang -O2 -o latency latency.c -lpthread -lz
 * Usage:  ./latency <results_dir>
 *
 * Experiments:
 *   A  Memory latency ladder (random pointer chase) -> mem_latency.csv
 *   B  Sequential vs random cache-line access       -> seq_vs_random.csv
 *   C  Canonical-table rows (partial; the rest filled by run.sh / python)
 *        - mutex lock/unlock
 *        - branch mispredict
 *        - 1MB sequential read from RAM
 *        - SSD random 4KB read (F_NOCACHE)
 *        - SSD 1MB sequential read (F_NOCACHE)
 *        - localhost socket round trip
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

/* ---------- timing ---------- */
static inline uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* ---------- rng (xorshift, deterministic) ---------- */
static uint64_t rng_state = 0x9e3779b97f4a7c15ULL;
static inline uint64_t xrand(void) {
    uint64_t x = rng_state;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    rng_state = x;
    return x;
}

/* ---------- qsort comparator ---------- */
static int cmp_double(const void *a, const void *b) {
    double da = *(const double *)a, db = *(const double *)b;
    return (da > db) - (da < db);
}
static double median_d(double *v, int n) {
    qsort(v, n, sizeof(double), cmp_double);
    if (n & 1) return v[n/2];
    return 0.5 * (v[n/2 - 1] + v[n/2]);
}

/* volatile sink to defeat dead-code elimination */
static volatile uint64_t g_sink = 0;

/* ============================================================
 * Exp A: memory latency ladder — random-permutation pointer chase
 * ============================================================ */
static double measure_pointer_chase(size_t size_bytes, uint64_t target_accesses) {
    size_t n = size_bytes / sizeof(size_t);
    if (n < 2) n = 2;
    /* array holds indices forming one big random cycle */
    size_t *arr = (size_t *)malloc(n * sizeof(size_t));
    size_t *perm = (size_t *)malloc(n * sizeof(size_t));
    if (!arr || !perm) { fprintf(stderr, "OOM at %zu bytes\n", size_bytes); exit(1); }

    for (size_t i = 0; i < n; i++) perm[i] = i;
    /* Fisher-Yates shuffle of 1..n-1 to build a single Hamiltonian cycle:
       start at 0, visit perm order, link each to next, last back to 0. */
    for (size_t i = n - 1; i > 0; i--) {
        size_t j = xrand() % (i + 1);
        size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }
    /* chain: arr[perm[i]] = perm[i+1]; close the cycle */
    for (size_t i = 0; i < n; i++) {
        arr[perm[i]] = perm[(i + 1) % n];
    }
    free(perm);

    /* iterations: scale so each size runs a fair while, but cap work */
    uint64_t iters = target_accesses;

    /* warm up */
    size_t idx = 0;
    for (uint64_t i = 0; i < n; i++) idx = arr[idx];
    g_sink += idx;

    /* timed dependent chase */
    uint64_t t0 = now_ns();
    idx = 0;
    for (uint64_t i = 0; i < iters; i++) {
        idx = arr[idx];
    }
    uint64_t t1 = now_ns();
    g_sink += idx;

    free(arr);
    double ns = (double)(t1 - t0) / (double)iters;
    return ns;
}

static void exp_A(const char *dir, FILE *summary) {
    char path[1024];
    snprintf(path, sizeof(path), "%s/mem_latency.csv", dir);
    FILE *f = fopen(path, "w");
    fprintf(f, "working_set_bytes,ns_per_access\n");

    size_t sizes[] = {
        4*1024, 8*1024, 16*1024, 32*1024, 64*1024, 128*1024, 256*1024,
        512*1024, 1*1024*1024, 2*1024*1024, 4*1024*1024, 8*1024*1024,
        16*1024*1024, 24*1024*1024, 32*1024*1024, 48*1024*1024,
        64*1024*1024, 128*1024*1024, 256*1024*1024
    };
    int nsizes = sizeof(sizes)/sizeof(sizes[0]);

    fprintf(stderr, "[A] memory latency ladder...\n");
    double small_ns = -1, large_ns = -1;
    for (int s = 0; s < nsizes; s++) {
        size_t sz = sizes[s];
        /* target: ~300M accesses for tiny sets, fewer for huge (still >0.2s) */
        uint64_t acc;
        if (sz <= 256*1024)      acc = 300000000ULL;
        else if (sz <= 4*1024*1024) acc = 200000000ULL;
        else if (sz <= 32*1024*1024) acc = 100000000ULL;
        else                     acc = 60000000ULL;

        /* median of 5 trials */
        double trials[5];
        for (int t = 0; t < 5; t++) trials[t] = measure_pointer_chase(sz, acc);
        double ns = median_d(trials, 5);

        fprintf(f, "%zu,%.4f\n", sz, ns);
        fprintf(stderr, "    %8zu KB -> %7.3f ns\n", sz/1024, ns);
        if (s == 0) small_ns = ns;
        large_ns = ns;
    }
    fclose(f);

    fprintf(summary, "== Exp A: memory latency ladder ==\n");
    fprintf(summary, "  L1 region (4KB):      %.3f ns/access\n", small_ns);
    fprintf(summary, "  DRAM region (256MB):  %.3f ns/access\n", large_ns);
    fprintf(summary, "  (full ladder in mem_latency.csv)\n\n");
}

/* ============================================================
 * Exp B: sequential vs random cache-line access over big buffer
 * ============================================================ */
static void exp_B(const char *dir, FILE *summary) {
    char path[1024];
    snprintf(path, sizeof(path), "%s/seq_vs_random.csv", dir);
    FILE *f = fopen(path, "w");
    fprintf(f, "access_pattern,ns_per_line,MB_per_s\n");

    const size_t BUF = 256ULL * 1024 * 1024;   /* 256 MB */
    const size_t LINE = 64;
    size_t nlines = BUF / LINE;
    uint8_t *buf = (uint8_t *)malloc(BUF);
    if (!buf) { fprintf(stderr, "OOM buf\n"); exit(1); }
    memset(buf, 1, BUF);   /* touch pages */

    fprintf(stderr, "[B] sequential vs random...\n");

    /* --- sequential: touch one byte per cache line, in order --- */
    uint64_t sum = 0;
    /* warm */
    for (size_t i = 0; i < nlines; i++) sum += buf[i*LINE];
    g_sink += sum;

    double seq_trials[3];
    for (int t = 0; t < 3; t++) {
        sum = 0;
        uint64_t t0 = now_ns();
        for (size_t i = 0; i < nlines; i++) sum += buf[i*LINE];
        uint64_t t1 = now_ns();
        g_sink += sum;
        seq_trials[t] = (double)(t1 - t0) / (double)nlines;
    }
    double seq_ns = median_d(seq_trials, 3);
    double seq_mbps = (double)BUF / (seq_ns * nlines) * 1e9 / (1024.0*1024.0);

    /* --- random: build permutation of line indices --- */
    uint32_t *order = (uint32_t *)malloc(nlines * sizeof(uint32_t));
    for (size_t i = 0; i < nlines; i++) order[i] = (uint32_t)i;
    for (size_t i = nlines - 1; i > 0; i--) {
        size_t j = xrand() % (i + 1);
        uint32_t tmp = order[i]; order[i] = order[j]; order[j] = tmp;
    }
    /* warm */
    sum = 0;
    for (size_t i = 0; i < nlines; i++) sum += buf[(size_t)order[i]*LINE];
    g_sink += sum;

    double rnd_trials[3];
    for (int t = 0; t < 3; t++) {
        sum = 0;
        uint64_t t0 = now_ns();
        for (size_t i = 0; i < nlines; i++) sum += buf[(size_t)order[i]*LINE];
        uint64_t t1 = now_ns();
        g_sink += sum;
        rnd_trials[t] = (double)(t1 - t0) / (double)nlines;
    }
    double rnd_ns = median_d(rnd_trials, 3);
    double rnd_mbps = (double)BUF / (rnd_ns * nlines) * 1e9 / (1024.0*1024.0);

    fprintf(f, "sequential,%.4f,%.1f\n", seq_ns, seq_mbps);
    fprintf(f, "random,%.4f,%.1f\n", rnd_ns, rnd_mbps);
    fclose(f);
    free(order);
    free(buf);

    fprintf(stderr, "    seq: %.3f ns/line (%.0f MB/s)  rnd: %.3f ns/line (%.0f MB/s)\n",
            seq_ns, seq_mbps, rnd_ns, rnd_mbps);
    fprintf(summary, "== Exp B: sequential vs random (256MB, 64B lines) ==\n");
    fprintf(summary, "  sequential: %.3f ns/line, %.0f MB/s\n", seq_ns, seq_mbps);
    fprintf(summary, "  random:     %.3f ns/line, %.0f MB/s\n", rnd_ns, rnd_mbps);
    fprintf(summary, "  prefetcher speedup (rnd/seq ns): %.1fx\n\n", rnd_ns/seq_ns);
}

/* ============================================================
 * Canonical rows measured in C
 * ============================================================ */

/* mutex lock/unlock (uncontended) */
static double measure_mutex(void) {
    pthread_mutex_t m;
    pthread_mutex_init(&m, NULL);
    const uint64_t N = 50000000ULL;
    /* warm */
    for (int i = 0; i < 1000; i++) { pthread_mutex_lock(&m); pthread_mutex_unlock(&m); }
    double trials[5];
    for (int t = 0; t < 5; t++) {
        uint64_t t0 = now_ns();
        for (uint64_t i = 0; i < N; i++) {
            pthread_mutex_lock(&m);
            g_sink++;
            pthread_mutex_unlock(&m);
        }
        uint64_t t1 = now_ns();
        trials[t] = (double)(t1 - t0) / (double)N;
    }
    pthread_mutex_destroy(&m);
    return median_d(trials, 5);
}

/* Branch-heavy inner loop kept as a REAL data-dependent branch.
 * At -O2 on arm64 clang converts the naive `if (x>=128) acc+=x;` into a
 * branchless predicated add (csel), so no branch is ever mispredicted and the
 * sorted/unsorted delta is 0. To actually measure misprediction we force a
 * genuine conditional branch with optnone + a volatile-ish side effect the
 * compiler will not flatten. Two distinct code paths, one taken on >=128.
 */
static uint64_t g_branch_taken = 0, g_branch_ntaken = 0;
__attribute__((noinline, optnone))
static uint64_t branch_loop(const uint8_t *data, size_t n, int reps) {
    uint64_t acc = 0;
    for (int r = 0; r < reps; r++) {
        for (size_t i = 0; i < n; i++) {
            if (data[i] >= 128) {
                acc += data[i];
                g_branch_taken++;
            } else {
                acc ^= data[i];
                g_branch_ntaken++;
            }
        }
    }
    return acc;
}

/* branch mispredict: sorted vs unsorted conditional over same data.
   Cost delta approximates per-mispredict penalty amortized over branches. */
static double measure_branch_mispredict(void) {
    const size_t N = 1 << 16;      /* 65536 elements */
    const int REPS = 4000;
    uint8_t *data = (uint8_t *)malloc(N);
    for (size_t i = 0; i < N; i++) data[i] = (uint8_t)(xrand() & 0xff);

    /* unsorted: ~50% mispredict on the >=128 branch */
    double unsorted_ns_per_branch, sorted_ns_per_branch;
    /* warm */
    g_sink += branch_loop(data, N, 5);
    {
        uint64_t t0 = now_ns();
        uint64_t acc = branch_loop(data, N, REPS);
        uint64_t t1 = now_ns();
        g_sink += acc;
        unsorted_ns_per_branch = (double)(t1 - t0) / ((double)N * REPS);
    }
    /* sort -> branch becomes highly predictable */
    /* simple counting sort for bytes */
    {
        uint64_t counts[256] = {0};
        for (size_t i = 0; i < N; i++) counts[data[i]]++;
        size_t idx = 0;
        for (int v = 0; v < 256; v++)
            for (uint64_t c = 0; c < counts[v]; c++) data[idx++] = (uint8_t)v;
    }
    g_sink += branch_loop(data, N, 5);
    {
        uint64_t t0 = now_ns();
        uint64_t acc = branch_loop(data, N, REPS);
        uint64_t t1 = now_ns();
        g_sink += acc;
        sorted_ns_per_branch = (double)(t1 - t0) / ((double)N * REPS);
    }
    free(data);
    /* delta per branch; ~half of branches mispredict in unsorted case,
       so cost-per-mispredict ~= 2 * delta. Report the delta itself as the
       per-branch penalty attributable to misprediction (conservative). */
    double delta = unsorted_ns_per_branch - sorted_ns_per_branch;
    if (delta < 0) delta = 0;
    return delta * 2.0;   /* ~cost per actual mispredict */
}

/* read 1MB sequentially from RAM */
static double measure_read_1mb_ram(void) {
    const size_t SZ = 1024 * 1024;
    uint8_t *buf = (uint8_t *)malloc(SZ);
    memset(buf, 2, SZ);
    /* warm */
    volatile uint64_t s = 0;
    for (size_t i = 0; i < SZ; i++) s += buf[i];
    g_sink += s;
    double trials[11];
    for (int t = 0; t < 11; t++) {
        uint64_t acc = 0;
        uint64_t t0 = now_ns();
        for (size_t i = 0; i < SZ; i++) acc += buf[i];
        uint64_t t1 = now_ns();
        g_sink += acc;
        trials[t] = (double)(t1 - t0);   /* total ns for 1MB */
    }
    free(buf);
    return median_d(trials, 11);
}

/* SSD random 4KB read with F_NOCACHE. Returns ns; sets *fellback. */
static double measure_ssd_random_4k(const char *dir, int *fellback) {
    *fellback = 0;
    char path[1024];
    snprintf(path, sizeof(path), "%s/.ssd_testfile.bin", dir);
    const size_t FILESZ = 2ULL * 1024 * 1024 * 1024;  /* 2 GB */
    const size_t BLK = 4096;

    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { *fellback = 1; return -1; }
    /* create file */
    if (ftruncate(fd, FILESZ) != 0) {
        /* write it out to ensure real blocks */
    }
    {
        uint8_t *chunk = (uint8_t *)malloc(1024*1024);
        memset(chunk, 0xab, 1024*1024);
        lseek(fd, 0, SEEK_SET);
        size_t written = 0;
        while (written < FILESZ) {
            ssize_t w = write(fd, chunk, 1024*1024);
            if (w <= 0) break;
            written += w;
        }
        free(chunk);
        fsync(fd);
    }
    /* try to disable OS caching */
    int nocache_ok = (fcntl(fd, F_NOCACHE, 1) == 0);
    if (!nocache_ok) *fellback = 1;
    /* also purge our just-written pages by reopening */
    close(fd);
    fd = open(path, O_RDONLY);
    if (fd < 0) { *fellback = 1; return -1; }
    if (fcntl(fd, F_NOCACHE, 1) != 0) *fellback = 1;

    void *rbuf = NULL;
    if (posix_memalign(&rbuf, 4096, BLK) != 0) { close(fd); *fellback=1; return -1; }

    size_t nblocks = FILESZ / BLK;
    const int N = 20000;
    /* warm a few */
    for (int i = 0; i < 50; i++) {
        off_t off = (off_t)(xrand() % nblocks) * BLK;
        pread(fd, rbuf, BLK, off);
    }
    double *lat = (double *)malloc(N * sizeof(double));
    for (int i = 0; i < N; i++) {
        off_t off = (off_t)(xrand() % nblocks) * BLK;
        uint64_t t0 = now_ns();
        ssize_t r = pread(fd, rbuf, BLK, off);
        uint64_t t1 = now_ns();
        if (r != (ssize_t)BLK) { /* ignore */ }
        lat[i] = (double)(t1 - t0);
        g_sink += ((uint8_t*)rbuf)[0];
    }
    double med = median_d(lat, N);
    free(lat);
    free(rbuf);
    close(fd);
    unlink(path);
    return med;
}

/* SSD 1MB sequential read with F_NOCACHE. */
static double measure_ssd_seq_1mb(const char *dir, int *fellback) {
    *fellback = 0;
    char path[1024];
    snprintf(path, sizeof(path), "%s/.ssd_seqfile.bin", dir);
    const size_t FILESZ = 512ULL * 1024 * 1024;  /* 512 MB */
    const size_t CHUNK = 1024 * 1024;

    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { *fellback = 1; return -1; }
    {
        uint8_t *chunk = (uint8_t *)malloc(CHUNK);
        memset(chunk, 0xcd, CHUNK);
        size_t written = 0;
        while (written < FILESZ) {
            ssize_t w = write(fd, chunk, CHUNK);
            if (w <= 0) break;
            written += w;
        }
        free(chunk);
        fsync(fd);
        free(NULL);
    }
    close(fd);
    fd = open(path, O_RDONLY);
    if (fd < 0) { *fellback = 1; return -1; }
    if (fcntl(fd, F_NOCACHE, 1) != 0) *fellback = 1;

    void *rbuf = NULL;
    if (posix_memalign(&rbuf, 4096, CHUNK) != 0) { close(fd); *fellback=1; return -1; }

    size_t nchunks = FILESZ / CHUNK;
    const int TRIALS = 100;
    double *lat = (double *)malloc(TRIALS * sizeof(double));
    int got = 0;
    off_t pos = 0;
    for (int t = 0; t < TRIALS; t++) {
        if ((size_t)(pos) + CHUNK > FILESZ) {
            pos = 0;
            /* reopen to drop any residual cache */
            close(fd);
            fd = open(path, O_RDONLY);
            fcntl(fd, F_NOCACHE, 1);
        }
        uint64_t t0 = now_ns();
        ssize_t r = pread(fd, rbuf, CHUNK, pos);
        uint64_t t1 = now_ns();
        pos += CHUNK;
        if (r == (ssize_t)CHUNK) { lat[got++] = (double)(t1 - t0); }
        g_sink += ((uint8_t*)rbuf)[0];
    }
    double med = got ? median_d(lat, got) : -1;
    free(lat);
    free(rbuf);
    close(fd);
    unlink(path);
    (void)nchunks;
    return med;
}

/* localhost socket round trip (socketpair ping-pong via 2 threads) */
struct pp_arg { int fd; int rounds; };
static void *pp_echo(void *a) {
    struct pp_arg *arg = (struct pp_arg *)a;
    char buf[1];
    for (int i = 0; i < arg->rounds; i++) {
        if (read(arg->fd, buf, 1) != 1) break;
        if (write(arg->fd, buf, 1) != 1) break;
    }
    return NULL;
}
static double measure_socket_rtt(void) {
    /* Use TCP loopback for a more realistic "socket" path than socketpair. */
    int listenfd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    int one = 1;
    setsockopt(listenfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (bind(listenfd, (struct sockaddr*)&addr, sizeof(addr)) != 0) return -1;
    listen(listenfd, 1);
    socklen_t alen = sizeof(addr);
    getsockname(listenfd, (struct sockaddr*)&addr, &alen);

    int clientfd = socket(AF_INET, SOCK_STREAM, 0);
    /* connect (nonblocking dance avoided: loopback connect is instant) */
    if (connect(clientfd, (struct sockaddr*)&addr, sizeof(addr)) != 0) return -1;
    int serverfd = accept(listenfd, NULL, NULL);
    close(listenfd);
    setsockopt(clientfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    setsockopt(serverfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    /* server echo thread */
    const int ROUNDS = 200000;
    struct pp_arg arg = { serverfd, ROUNDS + 1000 };
    pthread_t th;
    pthread_create(&th, NULL, pp_echo, &arg);

    char b = 'x';
    /* warm */
    for (int i = 0; i < 1000; i++) { write(clientfd, &b, 1); read(clientfd, &b, 1); }

    double *lat = (double *)malloc(ROUNDS * sizeof(double));
    for (int i = 0; i < ROUNDS; i++) {
        uint64_t t0 = now_ns();
        write(clientfd, &b, 1);
        read(clientfd, &b, 1);
        uint64_t t1 = now_ns();
        lat[i] = (double)(t1 - t0);
    }
    double med = median_d(lat, ROUNDS);
    close(clientfd);
    pthread_join(th, NULL);
    close(serverfd);
    free(lat);
    return med;
}

int main(int argc, char **argv) {
    const char *dir = (argc > 1) ? argv[1] : "./results";

    char sumpath[1024];
    snprintf(sumpath, sizeof(sumpath), "%s/summary_c.txt", dir);
    FILE *summary = fopen(sumpath, "w");
    if (!summary) { perror("summary"); return 1; }

    /* Exp A + B */
    exp_A(dir, summary);
    exp_B(dir, summary);

    /* Canonical rows measured in C -> partial csv, merged by run.sh */
    fprintf(stderr, "[C] canonical rows (mutex, branch, ram-1mb, ssd, socket)...\n");
    double mutex_ns  = measure_mutex();
    fprintf(stderr, "    mutex lock/unlock: %.2f ns\n", mutex_ns);
    double branch_ns = measure_branch_mispredict();
    fprintf(stderr, "    branch mispredict: %.2f ns\n", branch_ns);
    double ram1mb_ns = measure_read_1mb_ram();
    fprintf(stderr, "    read 1MB from RAM: %.0f ns\n", ram1mb_ns);

    int ssd_r_fb = 0, ssd_s_fb = 0;
    double ssd_rand_ns = measure_ssd_random_4k(dir, &ssd_r_fb);
    fprintf(stderr, "    ssd random 4K: %.0f ns (fallback=%d)\n", ssd_rand_ns, ssd_r_fb);
    double ssd_seq_ns  = measure_ssd_seq_1mb(dir, &ssd_s_fb);
    fprintf(stderr, "    ssd seq 1MB: %.0f ns (fallback=%d)\n", ssd_seq_ns, ssd_s_fb);

    double sock_ns = measure_socket_rtt();
    fprintf(stderr, "    socket RTT: %.0f ns\n", sock_ns);

    /* write C-measured canonical rows to a partial file for run.sh to merge */
    char cpath[1024];
    snprintf(cpath, sizeof(cpath), "%s/canonical_c_partial.csv", dir);
    FILE *cf = fopen(cpath, "w");
    fprintf(cf, "operation,measured_ns,jeff_dean_2012_ns,note\n");
    fprintf(cf, "Mutex lock/unlock,%.2f,17,uncontended pthread_mutex\n", mutex_ns);
    fprintf(cf, "Branch mispredict,%.2f,3,sorted-vs-unsorted delta x2\n", branch_ns);
    fprintf(cf, "Read 1MB sequentially from memory,%.0f,250000,malloc buffer sum\n", ram1mb_ns);
    fprintf(cf, "SSD random 4KB read,%.0f,16000,%s\n", ssd_rand_ns,
            ssd_r_fb ? "FALLBACK-page-cache-F_NOCACHE-failed" : "F_NOCACHE-real-SSD");
    fprintf(cf, "Read 1MB sequentially from SSD,%.0f,1000000,%s\n", ssd_seq_ns,
            ssd_s_fb ? "FALLBACK-page-cache-F_NOCACHE-failed" : "F_NOCACHE-real-SSD");
    fprintf(cf, "Localhost socket round trip,%.0f,500000,TCP-loopback-underestimates-datacenter-RTT\n", sock_ns);
    fclose(cf);

    fprintf(summary, "== Exp C rows measured in C ==\n");
    fprintf(summary, "  Mutex lock/unlock:            %.2f ns\n", mutex_ns);
    fprintf(summary, "  Branch mispredict:            %.2f ns\n", branch_ns);
    fprintf(summary, "  Read 1MB from RAM:            %.0f ns\n", ram1mb_ns);
    fprintf(summary, "  SSD random 4KB read:          %.0f ns (%s)\n", ssd_rand_ns,
            ssd_r_fb ? "FALLBACK page-cache" : "real SSD via F_NOCACHE");
    fprintf(summary, "  SSD 1MB sequential read:      %.0f ns (%s)\n", ssd_seq_ns,
            ssd_s_fb ? "FALLBACK page-cache" : "real SSD via F_NOCACHE");
    fprintf(summary, "  Localhost socket round trip:  %.0f ns (TCP loopback)\n\n", sock_ns);

    fclose(summary);
    fprintf(stderr, "done. sink=%llu\n", (unsigned long long)g_sink);
    return 0;
}
