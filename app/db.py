"""SQLite storage: source feeds, their cached episodes, collections and app settings.

A collection is one merged output feed. Shows (source feeds) are added to any
number of collections, or a collection can include every show automatically.
"""

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
    art_url       TEXT,
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

CREATE TABLE IF NOT EXISTS collections (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    image         TEXT NOT NULL DEFAULT '',
    token         TEXT NOT NULL UNIQUE,
    all_shows     INTEGER NOT NULL DEFAULT 0,
    max_items     INTEGER NOT NULL DEFAULT 300,
    prefix_titles INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS collection_feeds (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    feed_id       INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, feed_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "default_max_episodes": "25",
}

COLLECTION_FIELDS = ("title", "description", "image", "all_shows", "max_items", "prefix_titles")


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
        _add_missing_columns(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        _migrate_single_feed(conn)


def _add_missing_columns(conn):
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS skips them)."""
    added = {"feeds": {"use_proxy": "INTEGER NOT NULL DEFAULT 0", "art_url": "TEXT"}}
    for table, columns in added.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_single_feed(conn):
    """Before collections there was one merged feed configured in `settings`.
    Turn it into the first collection, keeping its link, and create a default
    collection on a fresh install."""
    if conn.execute("SELECT 1 FROM collections LIMIT 1").fetchone():
        return
    old = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    conn.execute(
        "INSERT INTO collections (title, description, image, token, all_shows, max_items, prefix_titles) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (
            old.get("title") or "All my podcasts",
            old.get("description") or "Every show, in one feed.",
            old.get("image") or "",
            old.get("feed_token") or new_token(),
            int(old.get("max_items") or 300),
            int(old.get("prefix_titles") or 1),
        ),
    )
    conn.execute(
        "DELETE FROM settings WHERE key IN "
        "('title', 'description', 'image', 'feed_token', 'max_items', 'prefix_titles')"
    )


def new_token() -> str:
    return secrets.token_urlsafe(24)


# ------------------------------------------------------------------- settings


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


# ---------------------------------------------------------------- collections


def list_collections() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT c.*,
                   CASE WHEN c.all_shows THEN (SELECT COUNT(*) FROM feeds WHERE enabled = 1)
                        ELSE (SELECT COUNT(*) FROM collection_feeds cf JOIN feeds f ON f.id = cf.feed_id
                              WHERE cf.collection_id = c.id AND f.enabled = 1)
                   END AS show_count
            FROM collections c ORDER BY c.id
            """
        ).fetchall()


def get_collection(collection_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()


def collection_by_token(token: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM collections WHERE token = ?", (token,)).fetchone()


def add_collection(title: str, all_shows: bool = False) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO collections (title, token, all_shows) VALUES (?, ?, ?)",
            (title, new_token(), int(all_shows)),
        ).lastrowid


def update_collection(collection_id: int, **fields):
    fields = {k: v for k, v in fields.items() if k in COLLECTION_FIELDS + ("token",)}
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE collections SET {cols} WHERE id = ?", (*fields.values(), collection_id))


def delete_collection(collection_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))


def memberships() -> dict[int, set[int]]:
    """feed id -> ids of the collections it was explicitly added to."""
    result: dict[int, set[int]] = {}
    with connect() as conn:
        for r in conn.execute("SELECT collection_id, feed_id FROM collection_feeds"):
            result.setdefault(r["feed_id"], set()).add(r["collection_id"])
    return result


def set_feed_collections(feed_id: int, collection_ids):
    with connect() as conn:
        conn.execute("DELETE FROM collection_feeds WHERE feed_id = ?", (feed_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO collection_feeds (collection_id, feed_id) "
            "SELECT id, ? FROM collections WHERE id = ?",
            [(feed_id, cid) for cid in collection_ids],
        )


def add_feeds_to_collections(feed_ids, collection_ids):
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO collection_feeds (collection_id, feed_id) "
            "SELECT id, ? FROM collections WHERE id = ?",
            [(fid, cid) for fid in feed_ids for cid in collection_ids],
        )


# ---------------------------------------------------------------------- feeds


def list_feeds() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id) AS episode_count "
            "FROM feeds f ORDER BY COALESCE(f.title_override, f.title, f.url) COLLATE NOCASE"
        ).fetchall()


def get_feed(feed_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()


def feed_id_by_url(url: str) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()
        return row["id"] if row else None


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


def collection_feeds(collection) -> list[sqlite3.Row]:
    """The shows in a collection (all enabled shows for an all-shows collection)."""
    with connect() as conn:
        return conn.execute(
            "SELECT f.* FROM feeds f WHERE f.enabled = 1 AND (? OR f.id IN "
            "(SELECT feed_id FROM collection_feeds WHERE collection_id = ?)) "
            "ORDER BY COALESCE(f.title_override, f.title, f.url) COLLATE NOCASE",
            (collection["all_shows"], collection["id"]),
        ).fetchall()


def merged_episodes(collection, default_max: int, max_items: int | None = None) -> list[sqlite3.Row]:
    """Newest episodes across a collection's enabled shows, capped per show and overall."""
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
                WHERE f.enabled = 1 AND (? OR f.id IN
                      (SELECT feed_id FROM collection_feeds WHERE collection_id = ?))
            )
            WHERE rn <= cap
            ORDER BY published DESC
            LIMIT ?
            """,
            (
                default_max,
                collection["all_shows"],
                collection["id"],
                max_items if max_items is not None else collection["max_items"],
            ),
        ).fetchall()
