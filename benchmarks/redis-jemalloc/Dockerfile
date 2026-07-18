FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS builder

ARG MALLOC
ARG REDIS_VERSION=7.4.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fL https://github.com/redis/redis/archive/${REDIS_VERSION}.tar.gz -o redis.tar.gz \
    && tar -xzf redis.tar.gz \
    && cd redis-${REDIS_VERSION} \
    && make MALLOC=${MALLOC} -j$(nproc) \
    && make install

FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/redis-server /usr/local/bin/redis-server
COPY --from=builder /usr/local/bin/redis-cli /usr/local/bin/redis-cli

EXPOSE 6379
CMD ["redis-server"]
