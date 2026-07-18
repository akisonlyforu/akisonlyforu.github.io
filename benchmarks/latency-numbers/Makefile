CC ?= clang
CFLAGS ?= -O2 -Wall
LDFLAGS ?= -lpthread -lz

latency: latency.c
	$(CC) $(CFLAGS) -o latency latency.c $(LDFLAGS)

clean:
	rm -f latency

.PHONY: clean
