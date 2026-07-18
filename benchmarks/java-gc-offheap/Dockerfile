# Digest-pinned Eclipse Temurin 21 JDK (linux, resolved 2026-07-18).
# eclipse-temurin:21-jdk @ sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a
FROM eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a

WORKDIR /app
COPY GcBench.java /app/GcBench.java
RUN javac GcBench.java

# mode (onheap|offheap) and results dir are supplied at run time by benchmark.py
ENTRYPOINT ["java"]
