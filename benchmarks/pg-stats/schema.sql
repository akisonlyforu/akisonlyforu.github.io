CREATE TABLE pg_stats_benchmark_identity (
    marker TEXT PRIMARY KEY CHECK (marker = 'pg_stats_bench_v1')
);

INSERT INTO pg_stats_benchmark_identity (marker) VALUES ('pg_stats_bench_v1');

CREATE TABLE admin_sessions (
    id             UUID PRIMARY KEY,
    admin_email    TEXT NOT NULL,
    ip_address     INET NOT NULL
);

CREATE TABLE audit_events (
    id             BIGSERIAL PRIMARY KEY,
    entity_type    TEXT NOT NULL,
    entity_id      BIGINT NOT NULL,
    session_id     UUID,
    event_type     TEXT NOT NULL,
    payload        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_events_session_id ON audit_events (session_id);

ALTER TABLE audit_events SET (autovacuum_enabled = false);
ALTER TABLE admin_sessions SET (autovacuum_enabled = false);
