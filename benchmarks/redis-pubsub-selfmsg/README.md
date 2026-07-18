# redis pub/sub self-message proof

This is the harness behind [Your Redis Pub/Sub Node Hears Its Own Messages](../../collections/_posts/2025-02-24-redis-pubsub-hears-itself.md).

It is **not** a performance benchmark. It's a behavior proof: two nodes subscribe
to the same channel, `node-A` publishes one event, and the demo prints exactly
which node processed it. You watch `node-A` react to its own message, then watch
an origin-id filter stop that while `node-B` still gets the event.

## Run it

You need Docker with Compose v2, or any local Redis, plus Python 3.9+.

```bash
cd benchmarks/redis-pubsub-selfmsg
docker compose up -d --wait          # Redis on 127.0.0.1:6390

python3 -m venv /tmp/pubsub-venv && source /tmp/pubsub-venv/bin/activate
pip install -r requirements.txt

REDIS_PORT=6390 python demo.py | tee results/output.txt
docker compose down -v
```

`results/output.txt` is the exact captured run used in the post. Override the
connection with `REDIS_HOST` / `REDIS_PORT`; any Redis works, the semantics are
the same everywhere because the self-delivery is inherent to pub/sub fanout, not
a version-specific detail.
