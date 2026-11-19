import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "bot.db"

# One post -> many deliveries (one per target), all handled in a single Claude run.
SCHEMA = """
DROP TABLE IF EXISTS jobs;
CREATE TABLE IF NOT EXISTS targets(
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('group', 'page', 'profile')),
  url  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts(
  id           INTEGER PRIMARY KEY,
  content      TEXT NOT NULL,
  images       TEXT NOT NULL DEFAULT '[]',
  scheduled_at TEXT NOT NULL,
  only_me      INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'pending',
  error        TEXT,
  cost         REAL,
  updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS deliveries(
  id        INTEGER PRIMARY KEY,
  post_id   INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  position  INTEGER NOT NULL,
  status    TEXT NOT NULL DEFAULT 'pending',
  post_url  TEXT,
  error     TEXT
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection):
    """Upgrade DBs created by older versions. Runs with foreign keys OFF so the
    targets rebuild doesn't cascade-delete deliveries."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    if "only_me" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN only_me INTEGER NOT NULL DEFAULT 0")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'targets'").fetchone()[0]
    if "'profile'" not in sql:
        conn.executescript("""
            CREATE TABLE targets_new(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('group', 'page', 'profile')), url TEXT NOT NULL);
            INSERT INTO targets_new SELECT id, name, kind, url FROM targets;
            DROP TABLE targets;
            ALTER TABLE targets_new RENAME TO targets;
        """)


def init():
    conn = sqlite3.connect(DB_PATH, timeout=10)  # foreign keys off by default
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
