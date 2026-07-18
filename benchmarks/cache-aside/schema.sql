CREATE TABLE users (
    id          BIGINT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    version     BIGINT NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (id, name, email, payload)
SELECT
    id,
    'user-' || id,
    'user-' || id || '@example.test',
    repeat(md5(id::text), 8)
FROM generate_series(1, 100000) AS id;

ANALYZE users;

