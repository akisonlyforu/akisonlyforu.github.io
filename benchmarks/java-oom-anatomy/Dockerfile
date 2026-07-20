# Digest-pinned Eclipse Temurin 21 JDK (linux, resolved 2026-07-20).
# eclipse-temurin:21-jdk @ sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a
# JDK (not JRE) on purpose: the harness needs javac at build time and jcmd at run
# time to poll a live class histogram out of the leaking JVM.
FROM eclipse-temurin@sha256:da9d3a4f7650db39b918fc5a2c3da76556fb8cc8e5f3767cdea0bb409286951a

WORKDIR /app
COPY src/Main.java src/Leak.java /app/
RUN javac -d /app Leak.java && javac -d /app Main.java

# JVM flags, mode and results dir are all supplied at run time by benchmark.py.
ENTRYPOINT ["java"]
