import importlib
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

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
        <itunes:image href="https://cdn.example/{show}.jpg"/>
        {items}
        <item><title>Text-only post, no audio</title><guid>{show}-post</guid></item>
      </channel>
    </rss>""".encode()


FEEDS = {
    "https://a.example/rss": make_feed("Alpha", 5, start_day=1),
    "https://b.example/rss?auth=TOKEN": make_feed("Bravo", 3, start_day=10, guid_prefix="Alpha"),
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("REFRESH_MINUTES", "9999")
    monkeypatch.setenv("FETCH_PROXY", "http://proxy.invalid:8888")
    from app import builder, db, fetcher, main

    for mod in (db, fetcher, builder, main):
        importlib.reload(mod)

    seen_proxy = []

    def handler(request):
        body = FEEDS.get(str(request.url))
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


def feed_items(client):
    token = client.db.get_settings()["feed_token"]
    resp = client.get(f"/feed/{token}.xml", auth=None)
    assert resp.status_code == 200
    return resp, ET.fromstring(resp.content).find("channel").findall("item")


def test_ui_requires_password(client):
    assert client.get("/", auth=None).status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/").status_code == 200


def test_wrong_feed_token_is_404(client):
    assert client.get("/feed/nope.xml", auth=None).status_code == 404


def test_add_feeds_and_merge(client):
    for url in FEEDS:
        assert client.post("/feeds", data={"url": url}).status_code == 200

    _, items = feed_items(client)
    assert len(items) == 8  # text-only posts dropped
    titles = [i.findtext("title") for i in items]
    assert titles[0] == "[Bravo] Bravo ep 2"  # newest first across shows
    assert titles[-1] == "[Alpha] Alpha ep 0"
    # Bravo reuses Alpha's GUIDs; output GUIDs must still be unique.
    guids = [i.findtext("guid") for i in items]
    assert len(set(guids)) == len(guids)
    # Show artwork is copied onto each episode, enclosures are untouched.
    assert items[0].find(f"{ITUNES}image").get("href") == "https://cdn.example/Bravo.jpg"
    assert items[0].find("enclosure").get("url").endswith("?token=SECRET")


def test_per_show_cap_disable_and_settings(client):
    for url in FEEDS:
        client.post("/feeds", data={"url": url})
    alpha, bravo = sorted(client.db.list_feeds(), key=lambda f: f["title"])

    client.post(f"/feeds/{alpha['id']}/update", data={"max_episodes": "2", "enabled": "on"})
    client.post(f"/feeds/{bravo['id']}/update", data={"title_override": "B!"})  # disabled
    _, items = feed_items(client)
    assert [i.findtext("title") for i in items] == ["[Alpha] Alpha ep 4", "[Alpha] Alpha ep 3"]

    client.post(
        "/settings",
        data={"title": "Mine", "max_items": "1", "default_max_episodes": "25"},
    )
    resp, items = feed_items(client)
    assert [i.findtext("title") for i in items] == ["Alpha ep 4"]  # prefix off
    assert ET.fromstring(resp.content).findtext("channel/title") == "Mine"


def test_etag_304(client):
    client.post("/feeds", data={"url": "https://a.example/rss"})
    resp, _ = feed_items(client)
    token = client.db.get_settings()["feed_token"]
    again = client.get(f"/feed/{token}.xml", headers={"If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304


def test_fetch_error_hides_url(client):
    client.post("/feeds", data={"url": "https://missing.example/rss?token=SECRET"})
    feed = client.db.list_feeds()[0]
    assert feed["last_error"] == "HTTP 404"
    assert "SECRET" not in client.get("/").text


def test_proxy_flag(client):
    client.post("/feeds", data={"url": "https://a.example/rss", "use_proxy": "on"})
    assert client.seen_proxy[-1] is True  # the add-time fetch went through the proxy
    assert client.db.list_feeds()[0]["use_proxy"] == 1


def test_opml_import_export(client):
    opml = b"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Podcasts">
        <outline type="rss" text="A" xmlUrl="https://a.example/rss"/>
        <outline type="rss" text="B" xmlUrl="https://b.example/rss?auth=TOKEN"/>
      </outline></body></opml>"""
    resp = client.post("/feeds/import", files={"file": ("subs.opml", opml)})
    assert resp.status_code == 200
    assert len(client.db.list_feeds()) == 2
    export = client.get("/feeds/export.opml")
    assert b"https://b.example/rss?auth=TOKEN" in export.content


def test_cross_origin_post_blocked(client):
    resp = client.post("/refresh", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_rotate_token(client):
    old = client.db.get_settings()["feed_token"]
    client.post("/token/rotate")
    assert client.get(f"/feed/{old}.xml", auth=None).status_code == 404
