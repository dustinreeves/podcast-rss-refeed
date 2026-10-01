"""SQLite storage: source feeds, their cached episodes, and app settings."""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "refeed.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT,
    title_override TEXT,
    image         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    use_proxy     INTEGER NOT NULL DEFAULT 0,
    max_episodes  INTEGER,
    etag          TEXT,
    last_modified TEXT,
    last_fetched  REAL,
    last_error    TEXT,
    created_at    REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS episodes (
    id               INTEGER PRIMARY KEY,
    feed_id          INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid             TEXT NOT NULL,
    title            TEXT,
    link             TEXT,
    description      TEXT,
    published        REAL,
    enclosure_url    TEXT NOT NULL,
    enclosure_type   TEXT,
    enclosure_length TEXT,
    duration         TEXT,
    image            TEXT,
    explicit         TEXT,
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS episodes_published ON episodes (published DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "title": "My Podcasts",
    "description": "All my podcasts in one feed.",
    "image": "",
    "max_items": "300",
    "default_max_episodes": "25",
    "prefix_titles": "1",
}


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('feed_token', ?)",
            (new_token(),),
        )


def new_token() -> str:
    return secrets.token_urlsafe(24)


def get_settings() -> dict:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_settings(values: dict):
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )


def list_feeds() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id) AS episode_count "
            "FROM feeds f ORDER BY COALESCE(f.title_override, f.title, f.url) COLLATE NOCASE"
        ).fetchall()


def get_feed(feed_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()


def add_feed(url: str) -> int | None:
    """Insert a feed; returns its id, or None if the URL is already present."""
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO feeds (url) VALUES (?)", (url,))
        return cur.lastrowid if cur.rowcount else None


def update_feed(feed_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE feeds SET {cols} WHERE id = ?", (*fields.values(), feed_id))


def delete_feed(feed_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))


def upsert_episodes(feed_id: int, episodes: list[dict], keep: int = 500):
    """Store episodes for a feed, keeping only the newest `keep` of them."""
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO episodes (feed_id, guid, title, link, description, published,
                                  enclosure_url, enclosure_type, enclosure_length,
                                  duration, image, explicit)
            VALUES (:feed_id, :guid, :title, :link, :description, :published,
                    :enclosure_url, :enclosure_type, :enclosure_length,
                    :duration, :image, :explicit)
            ON CONFLICT(feed_id, guid) DO UPDATE SET
                title = excluded.title, link = excluded.link,
                description = excluded.description, published = excluded.published,
                enclosure_url = excluded.enclosure_url, enclosure_type = excluded.enclosure_type,
                enclosure_length = excluded.enclosure_length, duration = excluded.duration,
                image = excluded.image, explicit = excluded.explicit
            """,
            [{**ep, "feed_id": feed_id} for ep in episodes],
        )
        conn.execute(
            "DELETE FROM episodes WHERE feed_id = ? AND id NOT IN ("
            "  SELECT id FROM episodes WHERE feed_id = ? ORDER BY published DESC LIMIT ?)",
            (feed_id, feed_id, keep),
        )


def merged_episodes(default_max: int, max_items: int) -> list[sqlite3.Row]:
    """Newest episodes across enabled feeds, capped per feed and overall."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM (
                SELECT e.*,
                       COALESCE(f.title_override, f.title, f.url) AS show_title,
                       f.image AS show_image,
                       ROW_NUMBER() OVER (PARTITION BY e.feed_id ORDER BY e.published DESC) AS rn,
                       COALESCE(f.max_episodes, ?) AS cap
                FROM episodes e JOIN feeds f ON f.id = e.feed_id
                WHERE f.enabled = 1
            )
            WHERE rn <= cap
            ORDER BY published DESC
            LIMIT ?
            """,
            (default_max, max_items),
        ).fetchall()
