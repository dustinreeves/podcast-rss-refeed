import importlib
import io
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def make_feed(show: str, n: int, start_day: int = 1, guid_prefix: str | None = None) -> bytes:
    items = "".join(
        f"""
        <item>
          <title>{show} ep {i}</title>
          <guid>{guid_prefix or show}-{i}</guid>
          <pubDate>{start_day + i:02d} Sep 2026 10:00:00 GMT</pubDate>
          <enclosure url="https://cdn.example/{show}/{i}.mp3?token=SECRET" type="audio/mpeg" length="123"/>
          <itunes:duration>01:00:00</itunes:duration>
        </item>"""
        for i in range(n)
    )
    return f"""<?xml version="1.0"?>
    <rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <title>{show}</title>
        <itunes:image href="https://cdn.example/{show}.png"/>
        {items}
        <item><title>Text-only post, no audio</title><guid>{show}-post</guid></item>
      </channel>
    </rss>""".encode()


def make_png(color, size=(300, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


A = "https://a.example/rss"
B = "https://b.example/rss?auth=TOKEN"
C = "https://c.example/rss"
RESPONSES = {
    A: make_feed("Alpha", 5, start_day=1),
    B: make_feed("Bravo", 3, start_day=10, guid_prefix="Alpha"),
    C: make_feed("Charlie", 2, start_day=20),
    "https://cdn.example/Alpha.png": make_png((200, 30, 30)),
    "https://cdn.example/Bravo.png": make_png((30, 200, 30)),
    "https://cdn.example/Charlie.png": b"not an image",
}

PASSWORD = "correct horse"


def load_app(monkeypatch, tmp_path, admin_password=PASSWORD):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REFRESH_MINUTES", "9999")
    monkeypatch.setenv("FETCH_PROXY", "http://proxy.invalid:8888")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_USER", raising=False)
    if admin_password:
        monkeypatch.setenv("ADMIN_PASSWORD", admin_password)
    else:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    from app import auth, builder, cover, db, fetcher, main

    for mod in (db, auth, cover, fetcher, builder, main):
        importlib.reload(mod)
    return main


def mock_fetching(monkeypatch, seen_proxy):
    from app import fetcher

    async def fake_get(url, *, use_proxy=False, headers=None, max_bytes=0):
        seen_proxy.append(use_proxy)
        body = RESPONSES.get(url)
        return fetcher.Fetched(200, body) if body else fetcher.Fetched(404, b"")

    monkeypatch.setattr(fetcher, "http_get", fake_get)


def login(c, username="admin", password=PASSWORD):
    resp = c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return c


@pytest.fixture
def app_main(tmp_path, monkeypatch):
    main = load_app(monkeypatch, tmp_path)
    seen_proxy = []
    mock_fetching(monkeypatch, seen_proxy)
    main.seen_proxy = seen_proxy
    return main


@pytest.fixture
def client(app_main):
    from app import db

    with TestClient(app_main.app) as c:
        login(c)
        c.db = db
        c.uid = db.get_user_by_name("admin")["id"]
        c.seen_proxy = app_main.seen_proxy
        yield c


def invite_link(client) -> str:
    page = client.post("/admin/invites").text
    return re.search(r'value="(http[^"]*/signup\?invite=[^"]+)"', page).group(1)


def sign_up(app, link: str, username: str, password: str = "brother-pass") -> TestClient:
    c = TestClient(app)
    path = link[link.index("/signup") :]
    token = path.split("invite=")[1]
    resp = c.post(
        "/signup",
        data={"invite": token, "username": username, "password": password, "confirm": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return c


def default_collection(client, uid=None):
    return client.db.list_collections(uid or client.uid)[0]


def feed_xml(client, collection=None):
    token = (collection or default_collection(client))["token"]
    resp = client.get(f"/feed/{token}.xml")
    assert resp.status_code == 200
    return resp, ET.fromstring(resp.content).find("channel")


def titles(channel):
    return [i.findtext("title") for i in channel.findall("item")]


def shows_in(channel):
    return {t.split("]")[0].lstrip("[") for t in titles(channel)}


def feed_id(client, url, uid=None):
    return client.db.feed_id_by_url(uid or client.uid, url)


# ------------------------------------------------------------------ accounts


def test_pages_need_sign_in(app_main):
    with TestClient(app_main.app) as c:
        resp = c.get("/", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login"
        assert c.post("/refresh", follow_redirects=False).status_code == 303
        assert c.get("/admin", follow_redirects=False).status_code == 303
        assert c.get("/healthz").status_code == 200


def test_login_logout(app_main):
    with TestClient(app_main.app) as c:
        assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
        resp = c.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
        assert "HttpOnly" in resp.headers["set-cookie"] and "SameSite=lax" in resp.headers["set-cookie"]
        assert c.get("/").status_code == 200
        cookie = c.cookies.get("refeed_session")
        c.post("/logout")
        assert c.get("/", follow_redirects=False).status_code == 303
        # The old session token no longer works even if replayed.
        c.cookies.set("refeed_session", cookie)
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_throttle(app_main):
    with TestClient(app_main.app) as c:
        for _ in range(10):
            c.post("/login", data={"username": "admin", "password": "wrong"})
        resp = c.post("/login", data={"username": "admin", "password": PASSWORD})
        assert resp.status_code == 429


def test_admin_created_from_env(client):
    me = client.db.get_user(client.uid)
    assert me["is_admin"] == 1
    assert client.db.get_settings() == {}  # no plaintext password or other secrets stored
    assert me["password_hash"].startswith("scrypt$") and PASSWORD not in me["password_hash"]


def test_first_run_without_password_logs_setup_link(tmp_path, monkeypatch, caplog):
    main = load_app(monkeypatch, tmp_path, admin_password=None)
    with caplog.at_level(logging.WARNING), TestClient(main.app):
        pass
    link = re.search(r"/signup\?invite=(\S+)", caplog.text)
    assert link, caplog.text
    from app import db

    assert db.count_users() == 0
    sign_up(main.app, "/signup?invite=" + link.group(1), "boss")
    assert db.get_user_by_name("boss")["is_admin"] == 1  # first account is the admin


def test_invite_signup_flow(client, app_main):
    link = invite_link(client)
    token = link.split("invite=")[1]
    assert client.get(f"/signup?invite={token}").status_code == 200
    assert client.get("/signup?invite=bogus").status_code == 404

    brother = sign_up(app_main.app, link, "bro")
    page = brother.get("/").text
    assert "Signed in as <strong>bro</strong>" in page
    assert ">Admin<" not in page
    assert client.db.get_user_by_name("bro")["is_admin"] == 0
    assert len(client.db.list_collections(client.db.get_user_by_name("bro")["id"])) == 1

    # Invites work once.
    again = TestClient(app_main.app).post(
        "/signup", data={"invite": token, "username": "bro2", "password": "x" * 8, "confirm": "x" * 8}
    )
    assert again.status_code == 404 and client.db.get_user_by_name("bro2") is None


def test_signup_validation(client, app_main):
    token = invite_link(client).split("invite=")[1]
    c = TestClient(app_main.app)
    bad = [
        ({"username": "ad", "password": "longenough", "confirm": "longenough"}, "Usernames are"),
        ({"username": "bro", "password": "short", "confirm": "short"}, "at least 8"),
        ({"username": "bro", "password": "longenough", "confirm": "different"}, "match"),
        ({"username": "ADMIN", "password": "longenough", "confirm": "longenough"}, "taken"),
    ]
    for data, message in bad:
        resp = c.post("/signup", data={"invite": token, **data})
        assert resp.status_code == 400 and message in resp.text, message
    # A failed attempt doesn't burn the invite.
    sign_up(app_main.app, "/signup?invite=" + token, "bro")


def test_users_are_isolated(client, app_main):
    brother = sign_up(app_main.app, invite_link(client), "bro")
    bro_id = client.db.get_user_by_name("bro")["id"]

    client.post("/feeds", data={"url": A})
    brother.post("/feeds", data={"url": B})
    brother.post("/feeds", data={"url": A})  # same show, separate copy

    # Every-show feeds only include the owner's shows.
    assert shows_in(feed_xml(client)[1]) == {"Alpha"}
    assert shows_in(feed_xml(client, default_collection(client, bro_id))[1]) == {"Alpha", "Bravo"}

    # The admin can't see or touch the brother's shows and feeds, and vice versa.
    bro_feed = feed_id(client, B, bro_id)
    bro_col = default_collection(client, bro_id)
    assert "b.example" not in client.get("/").text
    for method, path in [
        ("post", f"/feeds/{bro_feed}/update"),
        ("post", f"/feeds/{bro_feed}/delete"),
        ("post", f"/feeds/{bro_feed}/refresh"),
        ("post", f"/collections/{bro_col['id']}/rotate"),
        ("post", f"/collections/{bro_col['id']}/delete"),
        ("post", f"/collections/{bro_col['id']}/update"),
        ("get", f"/collections/{bro_col['id']}/export.opml"),
        ("post", f"/feeds/{feed_id(client, A)}/collections/{bro_col['id']}"),
    ]:
        resp = client.post(path, data={"title": "x"}) if method == "post" else client.get(path)
        assert resp.status_code == 404, path
    assert client.db.get_feed(bro_feed) is not None
    assert default_collection(client, bro_id)["token"] == bro_col["token"]
    assert "Bravo" not in client.get("/feeds/export.opml").text
    assert brother.get("/admin").status_code == 403


def test_password_change_and_reset_link(client, app_main):
    brother = sign_up(app_main.app, invite_link(client), "bro")
    bro_id = client.db.get_user_by_name("bro")["id"]

    resp = brother.post("/account/password", data={"current": "wrong", "password": "newpass12", "confirm": "newpass12"})
    assert resp.status_code == 400
    brother.post("/account/password", data={"current": "brother-pass", "password": "newpass12", "confirm": "newpass12"})
    assert brother.get("/").status_code == 200  # still signed in on this device
    login(TestClient(app_main.app), "bro", "newpass12")

    # Admin issues a reset link; it signs him out everywhere and sets a new password.
    page = client.post(f"/admin/users/{bro_id}/reset").text
    reset = re.search(r'id="reset-url"[^>]*value="[^"]*invite=([^"]+)"', page) or re.search(
        r'value="[^"]*/signup\?invite=([^"]+)"', page
    )
    token = reset.group(1)
    c = TestClient(app_main.app)
    assert "Set a new password for bro" in c.get(f"/signup?invite={token}").text
    resp = c.post("/signup", data={"invite": token, "password": "resetpass1", "confirm": "resetpass1"}, follow_redirects=False)
    assert resp.status_code == 303
    assert brother.get("/", follow_redirects=False).status_code == 303  # old session gone
    login(TestClient(app_main.app), "bro", "resetpass1")
    assert c.post("/signup", data={"invite": token, "password": "again1234", "confirm": "again1234"}).status_code == 404


def test_admin_delete_user(client, app_main):
    brother = sign_up(app_main.app, invite_link(client), "bro")
    brother.post("/feeds", data={"url": B})
    bro_id = client.db.get_user_by_name("bro")["id"]
    token = default_collection(client, bro_id)["token"]
    assert client.post(f"/admin/users/{client.uid}/delete").status_code == 200  # refused for self
    assert client.db.get_user(client.uid) is not None
    client.post(f"/admin/users/{bro_id}/delete")
    assert client.db.get_user(bro_id) is None
    assert client.get(f"/feed/{token}.xml").status_code == 404
    assert brother.get("/", follow_redirects=False).status_code == 303


def test_invite_revoke(client, app_main):
    token = invite_link(client).split("invite=")[1]
    (invite,) = client.db.pending_invites()
    client.post(f"/admin/invites/{invite['id']}/revoke")
    assert client.get(f"/signup?invite={token}").status_code == 404


# ------------------------------------------------------------------ basics


def test_wrong_tokens_are_404(client):
    assert client.get("/feed/nope.xml").status_code == 404
    assert client.get("/cover/nope.jpg").status_code == 404


def test_feed_links_work_without_sign_in(client, app_main):
    client.post("/feeds", data={"url": A})
    token = default_collection(client)["token"]
    anon = TestClient(app_main.app)
    assert anon.get(f"/feed/{token}.xml").status_code == 200
    assert anon.get(f"/cover/{token}.jpg").status_code == 200


def test_add_feeds_and_merge(client):
    for url in (A, B):
        assert client.post("/feeds", data={"url": url}).status_code == 200

    _, channel = feed_xml(client)
    items = channel.findall("item")
    assert len(items) == 8  # text-only posts dropped
    assert titles(channel)[0] == "[Bravo] Bravo ep 2"  # newest first across shows
    assert titles(channel)[-1] == "[Alpha] Alpha ep 0"
    guids = [i.findtext("guid") for i in items]
    assert len(set(guids)) == len(guids)  # Bravo reuses Alpha's GUIDs
    assert items[0].find(f"{ITUNES}image").get("href") == "https://cdn.example/Bravo.png"
    assert items[0].find("enclosure").get("url").endswith("?token=SECRET")


def test_per_show_cap_pause_and_feed_settings(client):
    for url in (A, B):
        client.post("/feeds", data={"url": url})
    col = default_collection(client)

    client.post(f"/feeds/{feed_id(client, A)}/update", data={"max_episodes": "2", "enabled": "on"})
    client.post(f"/feeds/{feed_id(client, B)}/update", data={"title_override": "B!"})  # paused
    assert titles(feed_xml(client)[1]) == ["[Alpha] Alpha ep 4", "[Alpha] Alpha ep 3"]

    client.post(
        f"/collections/{col['id']}/update",
        data={"title": "Mine", "max_items": "1", "all_shows": "on"},
    )
    _, channel = feed_xml(client)
    assert titles(channel) == ["Alpha ep 4"]  # prefix off
    assert channel.findtext("title") == "Mine"


def test_zero_means_no_limit(client):
    for url in (A, B):  # 5 + 3 episodes
        client.post("/feeds", data={"url": url})
    col = default_collection(client)
    client.post("/settings", data={"default_max_episodes": "1"})
    assert len(titles(feed_xml(client)[1])) == 2  # one per show

    client.post(f"/feeds/{feed_id(client, A)}/update", data={"max_episodes": "0", "enabled": "on"})
    assert len(titles(feed_xml(client)[1])) == 5 + 1  # Alpha unlimited, Bravo default
    assert 'value="0"' in client.get("/").text  # a 0 override shows as 0, not blank

    client.post("/settings", data={"default_max_episodes": "0"})
    client.post(
        f"/collections/{col['id']}/update",
        data={"title": "All", "max_items": "0", "all_shows": "on", "prefix_titles": "on"},
    )
    assert len(titles(feed_xml(client)[1])) == 8  # everything


def test_etag_304(client):
    client.post("/feeds", data={"url": A})
    resp, _ = feed_xml(client)
    token = default_collection(client)["token"]
    again = client.get(f"/feed/{token}.xml", headers={"If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304


def test_fetch_error_hides_url(client):
    client.post("/feeds", data={"url": "https://missing.example/rss?token=SECRET"})
    feed = client.db.list_feeds(client.uid)[0]
    assert feed["last_error"] == "HTTP 404"
    assert "SECRET" not in client.get("/").text


def test_proxy_flag(client):
    client.post("/feeds", data={"url": A, "use_proxy": "on"})
    assert client.seen_proxy[-1] is True  # the add-time fetch went through the proxy
    assert client.db.list_feeds(client.uid)[0]["use_proxy"] == 1


def test_cross_origin_post_blocked(client):
    resp = client.post("/refresh", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_rotate_token(client):
    col = default_collection(client)
    client.post(f"/collections/{col['id']}/rotate")
    assert client.get(f"/feed/{col['token']}.xml").status_code == 404
    assert default_collection(client)["token"] != col["token"]


# ------------------------------------------------------------- collections


def test_collections_pick_their_shows(client):
    client.post("/collections", data={"title": "Comedy"})
    client.post("/collections", data={"title": "History"})
    everything, comedy, history = client.db.list_collections(client.uid)

    client.post("/feeds", data={"url": A, "collections": [comedy["id"]]})
    client.post("/feeds", data={"url": B, "collections": [comedy["id"], history["id"]]})
    client.post("/feeds", data={"url": C})

    assert shows_in(feed_xml(client, comedy)[1]) == {"Alpha", "Bravo"}
    assert shows_in(feed_xml(client, history)[1]) == {"Bravo"}
    assert shows_in(feed_xml(client, everything)[1]) == {"Alpha", "Bravo", "Charlie"}

    client.post(f"/feeds/{feed_id(client, B)}/collections/{comedy['id']}", data={})
    client.post(f"/feeds/{feed_id(client, C)}/collections/{history['id']}", data={"member": "1"})
    assert shows_in(feed_xml(client, comedy)[1]) == {"Alpha"}
    assert shows_in(feed_xml(client, history)[1]) == {"Bravo", "Charlie"}
    assert client.get("/").text.count('aria-pressed="true"') == 3

    client.post(f"/feeds/{feed_id(client, A)}/delete")
    assert titles(feed_xml(client, comedy)[1]) == []
    client.post(f"/collections/{comedy['id']}/delete")
    assert len(client.db.list_collections(client.uid)) == 2
    assert len(client.db.list_feeds(client.uid)) == 2


def test_existing_show_can_be_added_to_another_feed(client):
    client.post("/collections", data={"title": "Comedy"})
    comedy = client.db.list_collections(client.uid)[1]
    client.post("/feeds", data={"url": A})
    resp = client.post("/feeds", data={"url": A, "collections": [comedy["id"]]})
    assert "already in your library" in resp.text
    assert len(titles(feed_xml(client, comedy)[1])) == 5


def test_cannot_delete_last_feed(client):
    col = default_collection(client)
    client.post(f"/collections/{col['id']}/delete")
    assert len(client.db.list_collections(client.uid)) == 1


def test_apostrophe_in_feed_name_is_safe(client):
    client.post("/collections", data={"title": "Dad's <Shows>"})
    page = client.get("/").text
    assert "Dad&#39;s &lt;Shows&gt;" in page
    assert "Dad's" not in page


def test_opml_import_into_collection_and_export(client):
    client.post("/collections", data={"title": "Imported"})
    imported = client.db.list_collections(client.uid)[1]
    opml = f"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Podcasts">
        <outline type="rss" text="A" xmlUrl="{A}"/>
        <outline type="rss" text="B" xmlUrl="{B}"/>
      </outline></body></opml>""".encode()
    resp = client.post(
        "/feeds/import", files={"file": ("subs.opml", opml)}, data={"collections": [imported["id"]]}
    )
    assert resp.status_code == 200
    assert len(client.db.list_feeds(client.uid)) == 2
    assert len(client.db.collection_feeds(imported)) == 2
    export = client.get(f"/collections/{imported['id']}/export.opml")
    assert B.encode() in export.content


# ---------------------------------------------------------------- migrations


def _old_feeds_table(conn):
    conn.executescript(
        """
        CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT,
            title_override TEXT, image TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            use_proxy INTEGER NOT NULL DEFAULT 0, max_episodes INTEGER, etag TEXT,
            last_modified TEXT, last_fetched REAL, last_error TEXT,
            created_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO feeds (url, title) VALUES ('https://a.example/rss', 'Alpha');
        """
    )


def test_migrates_single_feed_install(tmp_path, monkeypatch):
    """A database from before collections keeps its feed link and settings."""
    conn = sqlite3.connect(tmp_path / "refeed.db")
    _old_feeds_table(conn)
    conn.executescript(
        """
        INSERT INTO settings VALUES ('title', 'Old Feed'), ('feed_token', 'old-token'),
            ('max_items', '50'), ('prefix_titles', '0'), ('default_max_episodes', '7'),
            ('description', ''), ('image', '');
        """
    )
    conn.commit()
    conn.close()

    main = load_app(monkeypatch, tmp_path)
    with TestClient(main.app):  # startup: migrate, then create the admin from env
        pass
    from app import db

    admin = db.get_user_by_name("admin")
    (col,) = db.list_collections(admin["id"])
    assert (col["title"], col["token"], col["max_items"], col["prefix_titles"], col["all_shows"]) == (
        "Old Feed", "old-token", 50, 0, 1,
    )
    assert col["show_count"] == 1
    assert admin["default_max_episodes"] == 7
    assert db.get_settings() == {}


def test_migrates_collections_install(tmp_path, monkeypatch):
    """A database from the single-user collections release (feeds.url UNIQUE,
    no owners) is adopted by the admin with episodes and memberships intact."""
    conn = sqlite3.connect(tmp_path / "refeed.db")
    _old_feeds_table(conn)
    conn.executescript(
        """
        ALTER TABLE feeds ADD COLUMN art_url TEXT;
        CREATE TABLE episodes (id INTEGER PRIMARY KEY,
            feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
            guid TEXT NOT NULL, title TEXT, link TEXT, description TEXT, published REAL,
            enclosure_url TEXT NOT NULL, enclosure_type TEXT, enclosure_length TEXT,
            duration TEXT, image TEXT, explicit TEXT, UNIQUE (feed_id, guid));
        CREATE TABLE collections (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL UNIQUE, all_shows INTEGER NOT NULL DEFAULT 0,
            max_items INTEGER NOT NULL DEFAULT 300, prefix_titles INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE collection_feeds (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
            PRIMARY KEY (collection_id, feed_id));
        INSERT INTO episodes (feed_id, guid, title, published, enclosure_url)
            VALUES (1, 'g1', 'Alpha ep', 1000, 'https://cdn.example/a.mp3');
        INSERT INTO collections (title, token, all_shows) VALUES ('Everything', 'tok-all', 1);
        INSERT INTO collections (title, token, all_shows) VALUES ('Comedy', 'tok-comedy', 0);
        INSERT INTO collection_feeds VALUES (2, 1);
        INSERT INTO settings VALUES ('default_max_episodes', '9');
        """
    )
    conn.commit()
    conn.close()

    main = load_app(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        from app import db

        admin = db.get_user_by_name("admin")
        cols = db.list_collections(admin["id"])
        assert [x["token"] for x in cols] == ["tok-all", "tok-comedy"]
        assert admin["default_max_episodes"] == 9
        assert titles(feed_xml(c, cols[1])[1]) == ["[Alpha] Alpha ep"]
        feeds_sql = sqlite3.connect(tmp_path / "refeed.db").execute(
            "SELECT sql FROM sqlite_master WHERE name = 'feeds'"
        ).fetchone()[0]
        assert "UNIQUE (user_id, url)" in feeds_sql
        # Deleting the show still cascades to its episodes (foreign keys intact).
        login(c)
        c.post(f"/feeds/{db.feed_id_by_url(admin['id'], A)}/delete")
        assert sqlite3.connect(tmp_path / "refeed.db").execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


# ------------------------------------------------------------------ covers


def cover_image(client, collection):
    resp = client.get(f"/cover/{collection['token']}.jpg")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    return Image.open(io.BytesIO(resp.content))


def test_generated_cover(client):
    for url in (A, B, C):
        client.post("/feeds", data={"url": url})
    col = default_collection(client)

    assert client.db.get_feed(feed_id(client, A))["art_url"] == "https://cdn.example/Alpha.png"
    assert client.db.get_feed(feed_id(client, C))["art_url"] is None

    img = cover_image(client, col).convert("RGB")
    assert img.size == (1400, 1400)
    r, g, _ = img.getpixel((100, 100))
    assert r > 150 and g < 80
    r, g, _ = img.getpixel((1300, 100))
    assert g > 150 and r < 80

    _, channel = feed_xml(client)
    href = channel.find(f"{ITUNES}image").get("href")
    assert f"/cover/{col['token']}.jpg?v=" in href
    client.post(f"/feeds/{feed_id(client, B)}/delete")
    _, channel = feed_xml(client)
    assert channel.find(f"{ITUNES}image").get("href") != href
    assert cover_image(client, col).size == (1400, 1400)

    client.post(
        f"/collections/{col['id']}/update",
        data={"title": "x", "image": "https://img.example/mine.jpg", "all_shows": "on"},
    )
    _, channel = feed_xml(client)
    assert channel.find(f"{ITUNES}image").get("href") == "https://img.example/mine.jpg"


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 6, 7, 40])
def test_render_layouts(tmp_path, n):
    from app import cover

    files = []
    for i in range(n):
        path = tmp_path / f"{i}.jpg"
        Image.new("RGB", (50, 50), (i * 5 % 255, 100, 150)).save(path)
        files.append(path)
    title = "A Very Long Collection Name That Will Not Fit On One Line At All, Really"
    img = cover.render(title, files)
    assert img.size == (1400, 1400) and img.mode == "RGB"


@pytest.mark.parametrize(
    "headers, allowed",
    [
        # Browser behind "Referrer-Policy: no-referrer": Origin is "null".
        ({"Origin": "null", "Sec-Fetch-Site": "same-origin"}, True),
        ({"Origin": "null"}, True),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Origin": "http://testserver"}, True),
        ({"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),  # another subdomain
        ({"Sec-Fetch-Site": "cross-site", "Origin": "null"}, False),
        ({"Origin": "https://evil.example"}, False),
    ],
)
def test_form_post_origin_checks(app_main, headers, allowed):
    with TestClient(app_main.app) as c:
        resp = c.post(
            "/login", data={"username": "admin", "password": PASSWORD}, headers=headers,
            follow_redirects=False,
        )
        assert (resp.status_code == 303) is allowed, resp.text


# --------------------------------------------------------------- curl fetching


@pytest.fixture
def local_server():
    """A tiny HTTP server: /feed (ETag + 304), /redirect, /big, /missing."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen["user_agent"] = self.headers.get("User-Agent")
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/feed?token=SECRET")
                self.end_headers()
            elif self.path.startswith("/feed"):
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                body = RESPONSES[A]
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("ETag", '"v1"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/big"):
                self.send_response(200)
                self.send_header("Content-Length", str(5_000_000))
                self.end_headers()
                self.wfile.write(b"x" * 5_000_000)
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def test_http_get_with_real_curl(local_server, monkeypatch):
    import asyncio
    import shutil

    from app import fetcher

    if not shutil.which(fetcher.CURL):
        pytest.skip("curl not installed")
    base, seen = local_server
    argv = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        argv.extend(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def run():
        ok = await fetcher.http_get(f"{base}/redirect?token=SECRET")
        assert ok.status == 200 and ok.content == RESPONSES[A]
        assert ok.headers["etag"] == '"v1"' and ok.headers["content-type"] == "application/rss+xml"
        assert seen["user_agent"] == fetcher.USER_AGENT
        cached = await fetcher.http_get(f"{base}/feed", headers={"If-None-Match": '"v1"'})
        assert cached.status == 304
        assert (await fetcher.http_get(f"{base}/missing")).status == 404
        with pytest.raises(fetcher.FetchError, match="too large"):
            await fetcher.http_get(f"{base}/big", max_bytes=1_000_000)
        with pytest.raises(fetcher.FetchError) as err:
            await fetcher.http_get("http://127.0.0.1:9/?token=SECRET")
        assert "SECRET" not in str(err.value)

    asyncio.run(run())
    assert argv and not any("SECRET" in str(a) for a in argv)  # URLs go via stdin


def test_single_add_form_url_file_or_both(client):
    client.post("/collections", data={"title": "Comedy"})
    comedy = client.db.list_collections(client.uid)[1]
    opml = f"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline type="rss" text="B" xmlUrl="{B}"/></body></opml>""".encode()

    assert "Paste a feed URL or choose" in client.post("/feeds", data={"url": ""}).text

    # URL and file in one submit, one set of feed checkboxes and proxy option for both.
    resp = client.post(
        "/feeds",
        data={"url": A, "collections": [comedy["id"]], "use_proxy": "on"},
        files={"file": ("subs.opml", opml)},
    )
    assert "Show added." in resp.text and "Imported 1 new shows" in resp.text
    assert {f["url"] for f in client.db.collection_feeds(comedy)} == {A, B}
    assert all(f["use_proxy"] for f in client.db.list_feeds(client.uid))

    # File only, with an empty file input alongside (what browsers send).
    resp = client.post("/feeds", data={"url": ""}, files={"file": ("subs.opml", opml)})
    assert "Imported 0 new shows (1 already present)" in resp.text


def test_add_url_with_empty_file_field(client):
    # Browsers send the file input even when nothing was chosen: empty name, no bytes.
    resp = client.post(
        "/feeds",
        data={"url": A},
        files={"file": ("", b"", "application/octet-stream")},
    )
    assert resp.status_code == 200 and "Show added." in resp.text
    assert "Couldn't read" not in resp.text


# -------------------------------------------------------------------- player


def episodes_json(page: str) -> list:
    import json

    raw = re.search(r'<script type="application/json" id="episodes">(.*?)</script>', page, re.S).group(1)
    return json.loads(raw)


def test_listen_page_streams_from_the_show(client):
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    page = client.get(f"/listen/{col['id']}").text
    eps = episodes_json(page)
    assert len(eps) == 5
    # Audio URLs are the shows' own enclosure URLs: nothing is proxied or hosted here.
    assert all(e["url"].startswith("https://cdn.example/Alpha/") for e in eps)
    assert eps[0]["title"] == "Alpha ep 4" and eps[0]["duration"] == 3600
    assert client.get("/listen", follow_redirects=False).headers["location"] == f"/listen/{col['id']}"


def test_listen_page_paging(client):
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    page = client.get(f"/listen/{col['id']}?limit=2").text
    assert len(episodes_json(page)) == 2 and "?limit=102" in page


def test_progress_is_saved_per_user(client, app_main):
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    ep = episodes_json(client.get(f"/listen/{col['id']}").text)[0]

    resp = client.post("/api/progress", json={"episode_id": ep["id"], "position": 754.5, "duration": 3600})
    assert resp.status_code == 204
    again = episodes_json(client.get(f"/listen/{col['id']}").text)[0]
    assert (again["position"], again["played"]) == (754.5, False)
    assert "◐" not in client.get("/").text  # status icons are drawn client-side

    client.post("/api/progress", json={"episode_id": ep["id"], "position": 0, "played": True})
    assert episodes_json(client.get(f"/listen/{col['id']}").text)[0]["played"] is True

    # sendBeacon posts a Blob: the body is JSON whatever the content type says.
    client.post("/api/progress", content=b'{"episode_id": %d, "position": 12}' % ep["id"],
                headers={"Content-Type": "text/plain"})
    assert episodes_json(client.get(f"/listen/{col['id']}").text)[0]["position"] == 12

    assert client.post("/api/progress", content=b"nonsense").status_code == 400
    assert client.post("/api/progress", json={"episode_id": 999999}).status_code == 404

    # Someone else can't read or write this user's episodes or progress.
    brother = sign_up(app_main.app, invite_link(client), "bro")
    assert brother.post("/api/progress", json={"episode_id": ep["id"], "position": 1}).status_code == 404
    assert brother.get(f"/listen/{col['id']}").status_code == 404
    assert episodes_json(client.get(f"/listen/{col['id']}").text)[0]["position"] == 12


def test_progress_rejects_odd_numbers(client):
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    ep = episodes_json(client.get(f"/listen/{col['id']}").text)[0]
    client.post("/api/progress", content=b'{"episode_id": %d, "position": -5, "duration": NaN}' % ep["id"])
    saved = episodes_json(client.get(f"/listen/{col['id']}").text)[0]
    assert saved["position"] == 0 and saved["duration"] == 3600  # duration from the feed


def test_listen_page_escapes_feed_content(client, monkeypatch):
    nasty = make_feed("Alpha", 1).replace(
        b"<title>Alpha ep 0</title>",
        b"<title>&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title>"
        b"<description>&lt;img src=x onerror=alert(2)&gt; Real notes &amp;amp; more</description>",
    )
    monkeypatch.setitem(RESPONSES, A, nasty)
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    page = client.get(f"/listen/{col['id']}").text
    assert "<script>alert(1)" not in page and "onerror=alert" not in page
    (ep,) = episodes_json(page)
    assert ep["title"] == "</script><script>alert(1)</script>"  # intact as data
    assert "Real notes & more" in ep["notes"] and "<img" not in ep["notes"]


def test_deleting_a_show_removes_its_progress(client):
    client.post("/feeds", data={"url": A})
    col = default_collection(client)
    ep = episodes_json(client.get(f"/listen/{col['id']}").text)[0]
    client.post("/api/progress", json={"episode_id": ep["id"], "position": 30})
    client.post(f"/feeds/{feed_id(client, A)}/delete")
    from app import db

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0] == 0


@pytest.mark.parametrize(
    "raw, seconds",
    [("3723", 3723), ("1:02:03", 3723), ("62:03", 3723), ("00:30", 30), ("", None), ("abc", None), (None, None), ("-5", None)],
)
def test_parse_duration(raw, seconds):
    from app import player

    assert player.parse_duration(raw) == seconds
