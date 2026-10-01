"""SQLite storage: users, source feeds (shows), cached episodes and merged feeds.

Every show and merged feed (a "collection") belongs to one user. A collection
is one output feed; a user's shows are added to any number of their
collections, or a collection can include all of that user's shows.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "refeed.db"

SESSION_DAYS = 30
INVITE_DAYS = 7
DEFAULT_MAX_EPISODES = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    default_max_episodes INTEGER NOT NULL DEFAULT 25,
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id         INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    for_user   INTEGER REFERENCES users(id) ON DELETE CASCADE  -- set: a password reset link
);

CREATE TABLE IF NOT EXISTS feeds (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    title          TEXT,
    title_override TEXT,
    image          TEXT,
    art_url        TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    use_proxy      INTEGER NOT NULL DEFAULT 0,
    max_episodes   INTEGER,
    etag           TEXT,
    last_modified  TEXT,
    last_fetched   REAL,
    last_error     TEXT,
    created_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE (user_id, url)
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

CREATE TABLE IF NOT EXISTS collections (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
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

# Created after migrations, since older databases lack some of these columns.
INDEXES = """
CREATE INDEX IF NOT EXISTS episodes_published ON episodes (published DESC);
CREATE INDEX IF NOT EXISTS feeds_user ON feeds (user_id);
CREATE INDEX IF NOT EXISTS collections_user ON collections (user_id);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
"""

FEED_COLUMNS = (
    "id, user_id, url, title, title_override, image, art_url, enabled, use_proxy, "
    "max_episodes, etag, last_modified, last_fetched, last_error, created_at"
)

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


def _hash_token(token: str) -> str:
    # Session and invite tokens are stored hashed, so a leaked database can't be replayed.
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(24)


# ----------------------------------------------------------------- migrations


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit; explicit BEGIN below
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        _rebuild_feeds_for_users(conn)
        conn.executescript(INDEXES)
        _migrate_single_feed(conn)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    finally:
        conn.close()


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn):
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS skips them)."""
    added = {
        "feeds": {"use_proxy": "INTEGER NOT NULL DEFAULT 0", "art_url": "TEXT"},
        "collections": {"user_id": "INTEGER REFERENCES users(id) ON DELETE CASCADE"},
    }
    for table, columns in added.items():
        have = _columns(conn, table)
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _rebuild_feeds_for_users(conn):
    """Single-user databases have `url UNIQUE`; with users it's unique per user.
    SQLite can't alter a constraint, so rebuild the table (ids are kept, so
    episodes and collection memberships still point at the right rows)."""
    if "user_id" in _columns(conn, "feeds"):
        return
    # SQLite's documented order: create new, copy, drop old, rename new. (Renaming
    # the *old* table instead would drag other tables' foreign keys along with it.)
    old = [c for c in FEED_COLUMNS.split(", ") if c != "user_id"]
    feeds_ddl = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS feeds (") :]
    feeds_ddl = feeds_ddl[: feeds_ddl.index(");") + 2].replace("IF NOT EXISTS feeds (", "feeds_new (")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(feeds_ddl)
    conn.execute(f"INSERT INTO feeds_new ({', '.join(old)}) SELECT {', '.join(old)} FROM feeds")
    conn.execute("DROP TABLE feeds")
    conn.execute("ALTER TABLE feeds_new RENAME TO feeds")
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        conn.execute("ROLLBACK")
        raise RuntimeError(f"feeds migration would break foreign keys: {problems[:5]}")
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_single_feed(conn):
    """Before collections there was one merged feed configured in `settings`.
    Turn it into a collection, keeping its link. It's adopted by the first user."""
    old = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    if "feed_token" not in old or conn.execute("SELECT 1 FROM collections LIMIT 1").fetchone():
        return
    conn.execute(
        "INSERT INTO collections (title, description, image, token, all_shows, max_items, prefix_titles) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (
            old.get("title") or "All my podcasts",
            old.get("description") or "Every show, in one feed.",
            old.get("image") or "",
            old["feed_token"],
            int(old.get("max_items") or 300),
            int(old.get("prefix_titles") or 1),
        ),
    )
    conn.execute(
        "DELETE FROM settings WHERE key IN "
        "('title', 'description', 'image', 'feed_token', 'max_items', 'prefix_titles')"
    )


# ---------------------------------------------------------------------- users


def count_users() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _insert_user(conn, username: str, password_hash: str, is_admin: bool) -> int:
    """Create a user. The first user adopts data from before accounts existed;
    everyone starts with an all-shows feed."""
    first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    user_id = conn.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
        (username, password_hash, int(is_admin or first)),  # the first account runs the place
    ).lastrowid
    if first:
        conn.execute("UPDATE feeds SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.execute("UPDATE collections SET user_id = ? WHERE user_id IS NULL", (user_id,))
        legacy = conn.execute("SELECT value FROM settings WHERE key = 'default_max_episodes'").fetchone()
        if legacy:
            conn.execute(
                "UPDATE users SET default_max_episodes = ? WHERE id = ?", (int(legacy[0]), user_id)
            )
            conn.execute("DELETE FROM settings WHERE key = 'default_max_episodes'")
    if not conn.execute("SELECT 1 FROM collections WHERE user_id = ?", (user_id,)).fetchone():
        conn.execute(
            "INSERT INTO collections (user_id, title, description, token, all_shows) VALUES (?, ?, ?, ?, 1)",
            (user_id, "All my podcasts", "Every show, in one feed.", new_token()),
        )
    return user_id


def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    """Raises sqlite3.IntegrityError if the username is taken."""
    with connect() as conn:
        return _insert_user(conn, username, password_hash, is_admin)


def get_user(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(username: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT u.*,
                   (SELECT COUNT(*) FROM feeds f WHERE f.user_id = u.id) AS show_count,
                   (SELECT COUNT(*) FROM collections c WHERE c.user_id = u.id) AS collection_count
            FROM users u ORDER BY u.created_at
            """
        ).fetchall()


def update_user(user_id: int, **fields):
    fields = {k: v for k, v in fields.items() if k in ("password_hash", "is_admin", "default_max_episodes")}
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE id = ?", (*fields.values(), user_id))


def delete_user(user_id: int) -> tuple[list[int], list[int]]:
    """Delete a user and everything they own. Returns (feed ids, collection ids)
    so their cached artwork and covers can be removed too."""
    with connect() as conn:
        feed_ids = [r[0] for r in conn.execute("SELECT id FROM feeds WHERE user_id = ?", (user_id,))]
        collection_ids = [
            r[0] for r in conn.execute("SELECT id FROM collections WHERE user_id = ?", (user_id,))
        ]
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return feed_ids, collection_ids


# ------------------------------------------------------------------- sessions


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now, now + SESSION_DAYS * 86400),
        )
    return token


def user_for_session(token: str):
    with connect() as conn:
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_hash_token(token), time.time()),
        ).fetchone()


def delete_session(token: str):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def delete_user_sessions(user_id: int, keep_token: str | None = None):
    with connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _hash_token(keep_token) if keep_token else ""),
        )


# -------------------------------------------------------------------- invites


def create_invite(created_by: int | None, days: float = INVITE_DAYS, for_user: int | None = None) -> str:
    """A one-time link: an invite to sign up, or (with for_user) a password reset."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with connect() as conn:
        if for_user is not None:  # only the newest reset link per user works
            conn.execute("DELETE FROM invites WHERE for_user = ? AND used_at IS NULL", (for_user,))
        conn.execute(
            "INSERT INTO invites (token_hash, created_by, created_at, expires_at, for_user) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), created_by, now, now + days * 86400, for_user),
        )
    return token


def valid_invite(token: str):
    with connect() as conn:
        return conn.execute(
            "SELECT i.*, u.username AS for_username FROM invites i "
            "LEFT JOIN users u ON u.id = i.for_user "
            "WHERE i.token_hash = ? AND i.used_at IS NULL AND i.expires_at > ?",
            (_hash_token(token), time.time()),
        ).fetchone()


def pending_invites() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT i.*, c.username AS created_by_name, r.username AS for_username FROM invites i "
            "LEFT JOIN users c ON c.id = i.created_by "
            "LEFT JOIN users r ON r.id = i.for_user "
            "WHERE i.used_at IS NULL AND i.expires_at > ? ORDER BY i.created_at DESC",
            (time.time(),),
        ).fetchall()


def revoke_invite(invite_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM invites WHERE id = ? AND used_at IS NULL", (invite_id,))


def signup_with_invite(token: str, username: str, password_hash: str) -> int | None:
    """Use an invite and create the account in one transaction. None if the invite
    isn't valid (any more); sqlite3.IntegrityError if the username is taken."""
    now = time.time()
    with connect() as conn:
        claimed = conn.execute(
            "UPDATE invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL "
            "AND expires_at > ? AND for_user IS NULL",
            (now, _hash_token(token), now),
        ).rowcount
        if not claimed:
            return None
        user_id = _insert_user(conn, username, password_hash, is_admin=False)
        conn.execute("UPDATE invites SET used_by = ? WHERE token_hash = ?", (user_id, _hash_token(token)))
        return user_id


def reset_password_with_invite(token: str, password_hash: str) -> int | None:
    """Use a password-reset link. Returns the user id, or None if the link isn't valid."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT for_user FROM invites WHERE token_hash = ? AND used_at IS NULL "
            "AND expires_at > ? AND for_user IS NOT NULL",
            (_hash_token(token), now),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE invites SET used_at = ?, used_by = for_user WHERE token_hash = ?",
            (now, _hash_token(token)),
        )
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row[0]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row[0],))  # sign out everywhere
        return row[0]


# ------------------------------------------------------------------- settings


def get_settings() -> dict:
    """Instance-wide settings (per-user ones live on the users table)."""
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


def list_collections(user_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT c.*,
                   CASE WHEN c.all_shows
                        THEN (SELECT COUNT(*) FROM feeds WHERE enabled = 1 AND user_id = c.user_id)
                        ELSE (SELECT COUNT(*) FROM collection_feeds cf JOIN feeds f ON f.id = cf.feed_id
                              WHERE cf.collection_id = c.id AND f.enabled = 1)
                   END AS show_count
            FROM collections c WHERE c.user_id = ? ORDER BY c.id
            """,
            (user_id,),
        ).fetchall()


def get_collection(collection_id: int, user_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM collections WHERE id = ? AND user_id = ?", (collection_id, user_id)
        ).fetchone()


def collection_by_token(token: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM collections WHERE token = ? AND user_id IS NOT NULL", (token,)
        ).fetchone()


def add_collection(user_id: int, title: str, all_shows: bool = False) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO collections (user_id, title, token, all_shows) VALUES (?, ?, ?, ?)",
            (user_id, title, new_token(), int(all_shows)),
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


def memberships(user_id: int) -> dict[int, set[int]]:
    """feed id -> ids of the collections it was explicitly added to."""
    result: dict[int, set[int]] = {}
    with connect() as conn:
        for r in conn.execute(
            "SELECT cf.collection_id, cf.feed_id FROM collection_feeds cf "
            "JOIN feeds f ON f.id = cf.feed_id WHERE f.user_id = ?",
            (user_id,),
        ):
            result.setdefault(r["feed_id"], set()).add(r["collection_id"])
    return result


# Only links a show to a collection with the same owner.
_LINK_SAME_OWNER = (
    "INSERT OR IGNORE INTO collection_feeds (collection_id, feed_id) "
    "SELECT c.id, f.id FROM collections c JOIN feeds f ON f.user_id = c.user_id "
    "WHERE c.id = ? AND f.id = ?"
)


def set_feed_collections(feed_id: int, collection_ids):
    with connect() as conn:
        conn.execute("DELETE FROM collection_feeds WHERE feed_id = ?", (feed_id,))
        conn.executemany(_LINK_SAME_OWNER, [(cid, feed_id) for cid in collection_ids])


def add_feeds_to_collections(feed_ids, collection_ids):
    with connect() as conn:
        conn.executemany(
            _LINK_SAME_OWNER, [(cid, fid) for fid in feed_ids if fid for cid in collection_ids]
        )


# ---------------------------------------------------------------------- feeds


def list_feeds(user_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id) AS episode_count "
            "FROM feeds f WHERE f.user_id = ? "
            "ORDER BY COALESCE(f.title_override, f.title, f.url) COLLATE NOCASE",
            (user_id,),
        ).fetchall()


def all_feeds(user_id: int | None = None) -> list[sqlite3.Row]:
    """Feeds to refresh: everyone's, or one user's."""
    with connect() as conn:
        if user_id is None:
            return conn.execute("SELECT * FROM feeds WHERE user_id IS NOT NULL").fetchall()
        return conn.execute("SELECT * FROM feeds WHERE user_id = ?", (user_id,)).fetchall()


def get_feed(feed_id: int, user_id: int | None = None):
    """A feed by id; with user_id, only if that user owns it."""
    with connect() as conn:
        if user_id is None:
            return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        return conn.execute(
            "SELECT * FROM feeds WHERE id = ? AND user_id = ?", (feed_id, user_id)
        ).fetchone()


def feed_id_by_url(user_id: int, url: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM feeds WHERE user_id = ? AND url = ?", (user_id, url)
        ).fetchone()
        return row["id"] if row else None


def add_feed(user_id: int, url: str) -> int | None:
    """Insert a feed; returns its id, or None if this user already has the URL."""
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO feeds (user_id, url) VALUES (?, ?)", (user_id, url))
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


# Episodes kept per show. Generous so a "no limit" feed really has everything the
# show has published (including episodes that later drop out of its RSS), while
# still bounding the database if a feed goes haywire.
KEEP_PER_SHOW = 5000


def upsert_episodes(feed_id: int, episodes: list[dict], keep: int = KEEP_PER_SHOW):
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


# A collection's shows: its owner's enabled shows, all of them or the picked ones.
_IN_COLLECTION = (
    "f.enabled = 1 AND f.user_id = :owner AND (:all_shows OR f.id IN "
    "(SELECT feed_id FROM collection_feeds WHERE collection_id = :collection))"
)


def _collection_params(collection) -> dict:
    return {
        "owner": collection["user_id"],
        "all_shows": collection["all_shows"],
        "collection": collection["id"],
    }


def collection_feeds(collection) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            f"SELECT f.* FROM feeds f WHERE {_IN_COLLECTION} "
            "ORDER BY COALESCE(f.title_override, f.title, f.url) COLLATE NOCASE",
            _collection_params(collection),
        ).fetchall()


def merged_episodes(collection, max_items: int | None = None) -> list[sqlite3.Row]:
    """Newest episodes across a collection's shows, capped per show and overall.

    Per-show caps fall back to the owner's default. A cap of 0 means no limit,
    both per show and for the whole feed."""
    if max_items is None:
        max_items = collection["max_items"]
    with connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM (
                SELECT e.*,
                       COALESCE(f.title_override, f.title, f.url) AS show_title,
                       f.image AS show_image,
                       ROW_NUMBER() OVER (PARTITION BY e.feed_id ORDER BY e.published DESC) AS rn,
                       COALESCE(f.max_episodes, u.default_max_episodes) AS cap
                FROM episodes e
                JOIN feeds f ON f.id = e.feed_id
                JOIN users u ON u.id = f.user_id
                WHERE {_IN_COLLECTION}
            )
            WHERE cap = 0 OR rn <= cap
            ORDER BY published DESC
            LIMIT :limit
            """,
            {**_collection_params(collection), "limit": max_items or -1},  # LIMIT -1: no limit
        ).fetchall()
