"""Fetch source podcast feeds and store their episodes.

Private feeds (Patreon, Supercast, ...) carry access tokens in their URLs, so
nothing here logs or surfaces a feed URL - errors are reported by feed id/title.
"""

import asyncio
import calendar
import logging
import time

import os

import feedparser
import httpx

from . import db

log = logging.getLogger("refeed.fetcher")

USER_AGENT = "podcast-rss-refeed/1.0 (+https://github.com/dustinreeves/podcast-rss-refeed)"
CONCURRENCY = 5
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Optional HTTP proxy (e.g. a VPN container) for feeds that block datacenter IPs.
FETCH_PROXY = os.environ.get("FETCH_PROXY") or None


def _episode_from_entry(entry) -> dict | None:
    enclosure = next(
        (l for l in entry.get("links", []) if l.get("rel") == "enclosure" and l.get("href")),
        None,
    )
    if enclosure is None:
        return None  # not a podcast episode (no audio/video attached)

    published = entry.get("published_parsed") or entry.get("updated_parsed")
    image = entry.get("image", {}).get("href") if isinstance(entry.get("image"), dict) else None
    return {
        "guid": entry.get("id") or enclosure["href"],
        "title": entry.get("title", "Untitled episode"),
        "link": entry.get("link"),
        "description": entry.get("summary", ""),
        "published": calendar.timegm(published) if published else None,
        "enclosure_url": enclosure["href"],
        "enclosure_type": enclosure.get("type") or "audio/mpeg",
        "enclosure_length": str(enclosure.get("length") or "0"),
        "duration": entry.get("itunes_duration"),
        "image": image,
        "explicit": entry.get("itunes_explicit"),
    }


def parse_feed(content: bytes) -> tuple[dict, list[dict]]:
    """Parse feed XML into (channel info, episodes)."""
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a valid feed ({type(parsed.bozo_exception).__name__})")
    channel = parsed.feed
    image = channel.get("image", {}).get("href") if isinstance(channel.get("image"), dict) else None
    info = {"title": channel.get("title"), "image": image}
    episodes = [ep for ep in map(_episode_from_entry, parsed.entries) if ep]
    return info, episodes


async def refresh_feed(client: httpx.AsyncClient, feed) -> str | None:
    """Fetch one feed and store its episodes. Returns an error message or None."""
    headers = {}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]

    try:
        resp = await client.get(feed["url"], headers=headers)
        if resp.status_code == 304:
            db.update_feed(feed["id"], last_fetched=time.time(), last_error=None)
            return None
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}")
        info, episodes = await asyncio.to_thread(parse_feed, resp.content)
    except (httpx.HTTPError, ValueError) as exc:
        # str(httpx error) can include the URL (and its token), so only keep the type.
        msg = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        log.warning("feed %s (%s): %s", feed["id"], feed["title"] or "untitled", msg)
        db.update_feed(feed["id"], last_fetched=time.time(), last_error=msg)
        return msg

    db.upsert_episodes(feed["id"], episodes)
    db.update_feed(
        feed["id"],
        title=info["title"] or feed["title"],
        image=info["image"] or feed["image"],
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
        last_fetched=time.time(),
        last_error=None,
    )
    log.info("feed %s (%s): %d episodes", feed["id"], info["title"], len(episodes))
    return None


def _client(use_proxy: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        proxy=FETCH_PROXY if use_proxy else None,
    )


async def refresh_one(feed_id: int) -> str | None:
    feed = db.get_feed(feed_id)
    if feed is None:
        return "feed not found"
    async with _client(bool(feed["use_proxy"])) as client:
        return await refresh_feed(client, feed)


_refresh_lock = asyncio.Lock()


async def refresh_all():
    if _refresh_lock.locked():
        return  # a refresh is already running
    async with _refresh_lock:
        feeds = [f for f in db.list_feeds() if f["enabled"]]
        sem = asyncio.Semaphore(CONCURRENCY)

        async def run(clients, feed):
            async with sem:
                await refresh_feed(clients[bool(feed["use_proxy"])], feed)

        async with _client() as direct, _client(use_proxy=True) as proxied:
            await asyncio.gather(*(run({False: direct, True: proxied}, f) for f in feeds))
        log.info("refreshed %d feeds", len(feeds))


async def refresh_loop(interval_minutes: float):
    while True:
        try:
            await refresh_all()
        except Exception:
            log.exception("refresh failed")
        await asyncio.sleep(interval_minutes * 60)
