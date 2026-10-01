import importlib
import io
import sqlite3
import xml.etree.ElementTree as ET

import httpx
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


def load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("REFRESH_MINUTES", "9999")
    monkeypatch.setenv("FETCH_PROXY", "http://proxy.invalid:8888")
    monkeypatch.delenv("BASE_URL", raising=False)
    from app import builder, cover, db, fetcher, main

    for mod in (db, cover, fetcher, builder, main):
        importlib.reload(mod)
    return main


@pytest.fixture
def client(tmp_path, monkeypatch):
    main = load_app(monkeypatch, tmp_path)
    from app import db, fetcher

    seen_proxy = []

    def handler(request):
        body = RESPONSES.get(str(request.url))
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    def fake_client(use_proxy=False):
        seen_proxy.append(use_proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(fetcher, "_client", fake_client)
    with TestClient(main.app) as c:
        c.auth = ("admin", "pw")
        c.seen_proxy = seen_proxy
        c.db = db
        yield c


def default_collection(client):
    return client.db.list_collections()[0]


def feed_xml(client, collection=None):
    token = (collection or default_collection(client))["token"]
    resp = client.get(f"/feed/{token}.xml", auth=None)
    assert resp.status_code == 200
    return resp, ET.fromstring(resp.content).find("channel")


def titles(channel):
    return [i.findtext("title") for i in channel.findall("item")]


def shows_in(channel):
    return {t.split("]")[0].lstrip("[") for t in titles(channel)}


def feed_id(client, url):
    return client.db.feed_id_by_url(url)


# ------------------------------------------------------------------ basics


def test_ui_requires_password(client):
    assert client.get("/", auth=None).status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/").status_code == 200


def test_wrong_tokens_are_404(client):
    assert client.get("/feed/nope.xml", auth=None).status_code == 404
    assert client.get("/cover/nope.jpg", auth=None).status_code == 404


def test_fresh_install_has_an_all_shows_feed(client):
    (only,) = client.db.list_collections()
    assert only["all_shows"] == 1


def test_add_feeds_and_merge(client):
    for url in (A, B):
        assert client.post("/feeds", data={"url": url}).status_code == 200

    _, channel = feed_xml(client)
    items = channel.findall("item")
    assert len(items) == 8  # text-only posts dropped
    assert titles(channel)[0] == "[Bravo] Bravo ep 2"  # newest first across shows
    assert titles(channel)[-1] == "[Alpha] Alpha ep 0"
    # Bravo reuses Alpha's GUIDs; output GUIDs must still be unique.
    guids = [i.findtext("guid") for i in items]
    assert len(set(guids)) == len(guids)
    # Show artwork is copied onto each episode, enclosures are untouched.
    assert items[0].find(f"{ITUNES}image").get("href") == "https://cdn.example/Bravo.png"
    assert items[0].find("enclosure").get("url").endswith("?token=SECRET")


def test_per_show_cap_pause_and_feed_settings(client):
    for url in (A, B):
        client.post("/feeds", data={"url": url})
    col = default_collection(client)

    client.post(f"/feeds/{feed_id(client, A)}/update", data={"max_episodes": "2", "enabled": "on"})
    client.post(f"/feeds/{feed_id(client, B)}/update", data={"title_override": "B!"})  # paused
    _, channel = feed_xml(client)
    assert titles(channel) == ["[Alpha] Alpha ep 4", "[Alpha] Alpha ep 3"]

    client.post(
        f"/collections/{col['id']}/update",
        data={"title": "Mine", "max_items": "1", "all_shows": "on"},
    )
    _, channel = feed_xml(client)
    assert titles(channel) == ["Alpha ep 4"]  # prefix off
    assert channel.findtext("title") == "Mine"


def test_etag_304(client):
    client.post("/feeds", data={"url": A})
    resp, _ = feed_xml(client)
    token = default_collection(client)["token"]
    again = client.get(f"/feed/{token}.xml", headers={"If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304


def test_fetch_error_hides_url(client):
    client.post("/feeds", data={"url": "https://missing.example/rss?token=SECRET"})
    feed = client.db.list_feeds()[0]
    assert feed["last_error"] == "HTTP 404"
    assert "SECRET" not in client.get("/").text


def test_proxy_flag(client):
    client.post("/feeds", data={"url": A, "use_proxy": "on"})
    assert client.seen_proxy[-1] is True  # the add-time fetch went through the proxy
    assert client.db.list_feeds()[0]["use_proxy"] == 1


def test_cross_origin_post_blocked(client):
    resp = client.post("/refresh", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_rotate_token(client):
    col = default_collection(client)
    client.post(f"/collections/{col['id']}/rotate")
    assert client.get(f"/feed/{col['token']}.xml", auth=None).status_code == 404
    assert default_collection(client)["token"] != col["token"]


# ------------------------------------------------------------- collections


def test_collections_pick_their_shows(client):
    client.post("/collections", data={"title": "Comedy"})
    client.post("/collections", data={"title": "History"})
    everything, comedy, history = client.db.list_collections()

    client.post("/feeds", data={"url": A, "collections": [comedy["id"]]})
    client.post("/feeds", data={"url": B, "collections": [comedy["id"], history["id"]]})
    client.post("/feeds", data={"url": C})

    assert shows_in(feed_xml(client, comedy)[1]) == {"Alpha", "Bravo"}
    assert shows_in(feed_xml(client, history)[1]) == {"Bravo"}
    assert shows_in(feed_xml(client, everything)[1]) == {"Alpha", "Bravo", "Charlie"}

    # Toggle chips: take Bravo out of Comedy, put Charlie into History.
    client.post(f"/feeds/{feed_id(client, B)}/collections/{comedy['id']}", data={})
    client.post(f"/feeds/{feed_id(client, C)}/collections/{history['id']}", data={"member": "1"})
    assert shows_in(feed_xml(client, comedy)[1]) == {"Alpha"}
    assert shows_in(feed_xml(client, history)[1]) == {"Bravo", "Charlie"}

    # The UI shows a pressed chip per membership.
    page = client.get("/").text
    assert page.count('aria-pressed="true"') == 3

    # Removing a show takes it out of every feed; deleting a feed keeps the shows.
    client.post(f"/feeds/{feed_id(client, A)}/delete")
    assert titles(feed_xml(client, comedy)[1]) == []
    client.post(f"/collections/{comedy['id']}/delete")
    assert len(client.db.list_collections()) == 2
    assert len(client.db.list_feeds()) == 2


def test_existing_show_can_be_added_to_another_feed(client):
    client.post("/collections", data={"title": "Comedy"})
    comedy = client.db.list_collections()[1]
    client.post("/feeds", data={"url": A})
    resp = client.post("/feeds", data={"url": A, "collections": [comedy["id"]]})
    assert "already in your library" in resp.text
    assert len(titles(feed_xml(client, comedy)[1])) == 5


def test_cannot_delete_last_feed(client):
    col = default_collection(client)
    client.post(f"/collections/{col['id']}/delete")
    assert len(client.db.list_collections()) == 1


def test_apostrophe_in_feed_name_is_safe(client):
    client.post("/collections", data={"title": "Dad's <Shows>"})
    page = client.get("/").text
    assert "Dad&#39;s &lt;Shows&gt;" in page
    assert "Dad's" not in page  # never lands raw in an attribute or script


def test_opml_import_into_collection_and_export(client):
    client.post("/collections", data={"title": "Imported"})
    imported = client.db.list_collections()[1]
    opml = f"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Podcasts">
        <outline type="rss" text="A" xmlUrl="{A}"/>
        <outline type="rss" text="B" xmlUrl="{B}"/>
      </outline></body></opml>""".encode()
    resp = client.post(
        "/feeds/import", files={"file": ("subs.opml", opml)}, data={"collections": [imported["id"]]}
    )
    assert resp.status_code == 200
    assert len(client.db.list_feeds()) == 2
    assert len(client.db.collection_feeds(imported)) == 2
    export = client.get(f"/collections/{imported['id']}/export.opml")
    assert B.encode() in export.content
    assert client.get("/feeds/export.opml").status_code == 200


def test_migrates_single_feed_install(tmp_path, monkeypatch):
    """A database from before collections keeps its feed link and settings."""
    conn = sqlite3.connect(tmp_path / "refeed.db")
    conn.executescript(
        """
        CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT,
            title_override TEXT, image TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            use_proxy INTEGER NOT NULL DEFAULT 0, max_episodes INTEGER, etag TEXT,
            last_modified TEXT, last_fetched REAL, last_error TEXT,
            created_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO feeds (url, title) VALUES ('https://a.example/rss', 'Alpha');
        INSERT INTO settings VALUES ('title', 'Old Feed'), ('feed_token', 'old-token'),
            ('max_items', '50'), ('prefix_titles', '0'), ('default_max_episodes', '7'),
            ('description', ''), ('image', '');
        """
    )
    conn.commit()
    conn.close()

    load_app(monkeypatch, tmp_path)
    from app import db

    db.init()
    (col,) = db.list_collections()
    assert (col["title"], col["token"], col["max_items"], col["prefix_titles"], col["all_shows"]) == (
        "Old Feed", "old-token", 50, 0, 1,
    )
    assert col["show_count"] == 1
    assert db.get_settings() == {"default_max_episodes": "7"}
    assert "art_url" in db.list_feeds()[0].keys()


# ------------------------------------------------------------------ covers


def cover_image(client, collection):
    resp = client.get(f"/cover/{collection['token']}.jpg", auth=None)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    return Image.open(io.BytesIO(resp.content))


def test_generated_cover(client):
    for url in (A, B, C):
        client.post("/feeds", data={"url": url})
    col = default_collection(client)

    # Alpha and Bravo have artwork; Charlie's "image" isn't one and is skipped.
    assert client.db.get_feed(feed_id(client, A))["art_url"] == "https://cdn.example/Alpha.png"
    assert client.db.get_feed(feed_id(client, C))["art_url"] is None

    img = cover_image(client, col).convert("RGB")
    assert img.size == (1400, 1400)
    # Two shows -> 2x2 checkerboard: Alpha top-left, Bravo top-right.
    r, g, _ = img.getpixel((100, 100))
    assert r > 150 and g < 80
    r, g, _ = img.getpixel((1300, 100))
    assert g > 150 and r < 80

    # The feed points at a versioned cover URL that changes with the shows.
    _, channel = feed_xml(client)
    href = channel.find(f"{ITUNES}image").get("href")
    assert f"/cover/{col['token']}.jpg?v=" in href
    client.post(f"/feeds/{feed_id(client, B)}/delete")
    _, channel = feed_xml(client)
    assert channel.find(f"{ITUNES}image").get("href") != href
    assert cover_image(client, col).size == (1400, 1400)

    # A cover URL set by hand wins.
    client.post(
        f"/collections/{col['id']}/update",
        data={"title": "x", "image": "https://img.example/mine.jpg", "all_shows": "on"},
    )
    _, channel = feed_xml(client)
    assert channel.find(f"{ITUNES}image").get("href") == "https://img.example/mine.jpg"


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 9, 10, 40])
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
