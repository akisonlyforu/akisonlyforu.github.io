CREATE TABLE IF NOT EXISTS page_views (
  path TEXT NOT NULL,
  visitor_hash TEXT NOT NULL,
  day TEXT NOT NULL,
  PRIMARY KEY (path, visitor_hash, day)
);

CREATE TABLE IF NOT EXISTS votes (
  path TEXT NOT NULL,
  visitor_hash TEXT NOT NULL,
  vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (path, visitor_hash)
);

CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
CREATE INDEX IF NOT EXISTS idx_votes_path ON votes(path);
