"""Redis rate-limiters used by the benchmark harness."""

import time
import uuid


def now_ms():
    return int(time.time() * 1000)


class FixedWindow:
    """Aligned fixed-window counter: one Redis key per time bucket."""

    def __init__(self, redis_client, limit, window_ms, prefix="fixed"):
        self.redis = redis_client
        self.limit = limit
        self.window_ms = window_ms
        self.prefix = prefix

    def allow(self, key, timestamp_ms=None):
        timestamp_ms = now_ms() if timestamp_ms is None else int(timestamp_ms)
        bucket = timestamp_ms // self.window_ms
        redis_key = "%s:%s:%s" % (self.prefix, key, bucket)
        count = self.redis.incr(redis_key)
        if count == 1:
            self.redis.pexpire(redis_key, self.window_ms * 3)
        reset_ms = (bucket + 1) * self.window_ms
        return count <= self.limit, max(0, self.limit - count), reset_ms / 1000.0


class FixedWindowTwoKeyNaive:
    """Deliberately non-atomic counter/reset pair for the race experiment."""

    def __init__(self, redis_client, limit, window_ms, gap_ms=0, prefix="naive"):
        self.redis = redis_client
        self.limit = limit
        self.window_ms = window_ms
        self.gap_ms = gap_ms
        self.prefix = prefix

    def allow(self, key, timestamp_ms=None):
        timestamp_ms = now_ms() if timestamp_ms is None else int(timestamp_ms)
        counter_key = "%s:%s:count" % (self.prefix, key)
        reset_key = "%s:%s:reset" % (self.prefix, key)
        reset_value = self.redis.get(reset_key)
        reset_ms = int(reset_value) if reset_value is not None else 0
        if reset_ms <= timestamp_ms:
            if self.gap_ms:
                time.sleep(self.gap_ms / 1000.0)
            reset_ms = timestamp_ms + self.window_ms
            self.redis.set(counter_key, 0, px=self.window_ms + 1000)
            self.redis.set(reset_key, reset_ms, px=self.window_ms + 1000)
        count = self.redis.incr(counter_key)
        return count <= self.limit, max(0, self.limit - count), reset_ms / 1000.0


FIXED_WINDOW_LUA = """
local reset = tonumber(redis.call('GET', KEYS[2]))
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
if (not reset) or reset <= now then
  reset = now + window
  redis.call('SET', KEYS[1], 0, 'PX', window + 1000)
  redis.call('SET', KEYS[2], reset, 'PX', window + 1000)
end
local count = redis.call('INCR', KEYS[1])
local allowed = 0
if count <= limit then allowed = 1 end
return {allowed, count, reset}
"""


class FixedWindowLua:
    """Atomic counter/reset pair. The script decides and reports together."""

    def __init__(self, redis_client, limit, window_ms, prefix="lua"):
        self.redis = redis_client
        self.limit = limit
        self.window_ms = window_ms
        self.prefix = prefix
        self.script_sha = redis_client.script_load(FIXED_WINDOW_LUA)

    def allow(self, key, timestamp_ms=None):
        timestamp_ms = now_ms() if timestamp_ms is None else int(timestamp_ms)
        counter_key = "%s:%s:count" % (self.prefix, key)
        reset_key = "%s:%s:reset" % (self.prefix, key)
        allowed, count, reset_ms = self.redis.evalsha(
            self.script_sha,
            2,
            counter_key,
            reset_key,
            timestamp_ms,
            self.window_ms,
            self.limit,
        )
        return bool(allowed), max(0, self.limit - int(count)), int(reset_ms) / 1000.0


SLIDING_COUNTER_LUA = """
local previous = tonumber(redis.call('GET', KEYS[1])) or 0
local current = tonumber(redis.call('GET', KEYS[2])) or 0
local elapsed = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local weighted = previous * (window - elapsed) + current * window
local allowed = 0
if weighted < limit * window then
  current = redis.call('INCR', KEYS[2])
  redis.call('PEXPIRE', KEYS[2], window * 3)
  weighted = previous * (window - elapsed) + current * window
  allowed = 1
end
local remaining = math.floor((limit * window - weighted) / window)
if remaining < 0 then remaining = 0 end
return {allowed, remaining}
"""


class SlidingWindowCounter:
    """Two-bucket weighted sliding-window approximation."""

    def __init__(self, redis_client, limit, window_ms, prefix="sliding-counter"):
        self.redis = redis_client
        self.limit = limit
        self.window_ms = window_ms
        self.prefix = prefix
        self.script_sha = redis_client.script_load(SLIDING_COUNTER_LUA)

    def allow(self, key, timestamp_ms=None):
        timestamp_ms = now_ms() if timestamp_ms is None else int(timestamp_ms)
        bucket = timestamp_ms // self.window_ms
        elapsed = timestamp_ms % self.window_ms
        previous_key = "%s:%s:%s" % (self.prefix, key, bucket - 1)
        current_key = "%s:%s:%s" % (self.prefix, key, bucket)
        allowed, remaining = self.redis.evalsha(
            self.script_sha,
            2,
            previous_key,
            current_key,
            elapsed,
            self.window_ms,
            self.limit,
        )
        reset_ms = (bucket + 1) * self.window_ms
        return bool(allowed), int(remaining), reset_ms / 1000.0


SLIDING_LOG_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], window * 2)
  count = count + 1
  allowed = 1
end
return {allowed, limit - count}
"""


class SlidingWindowLog:
    """Exact rolling-window log used as the accuracy ground truth."""

    def __init__(self, redis_client, limit, window_ms, prefix="sliding-log"):
        self.redis = redis_client
        self.limit = limit
        self.window_ms = window_ms
        self.prefix = prefix
        self.script_sha = redis_client.script_load(SLIDING_LOG_LUA)
        self.sequence = 0

    def allow(self, key, timestamp_ms=None):
        timestamp_ms = now_ms() if timestamp_ms is None else int(timestamp_ms)
        self.sequence += 1
        member = "%s:%s" % (self.sequence, uuid.uuid4().hex)
        redis_key = "%s:%s" % (self.prefix, key)
        allowed, remaining = self.redis.evalsha(
            self.script_sha,
            1,
            redis_key,
            timestamp_ms,
            self.window_ms,
            self.limit,
            member,
        )
        return bool(allowed), int(remaining), (timestamp_ms + self.window_ms) / 1000.0
