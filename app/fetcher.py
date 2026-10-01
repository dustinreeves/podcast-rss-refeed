"""Fetch source podcast feeds and store their episodes.

Downloads go through the curl command-line tool rather than a Python HTTP
library: some hosts (Patreon, behind Cloudflare) challenge the TLS handshake
Python makes but accept curl's, the same way they accept podcast apps.

Private feeds (Patreon, Supercast, ...) carry access tokens in their URLs, so
nothing here logs or surfaces a feed URL - errors are reported by feed id/title.
URLs are handed to curl on stdin, never on its command line, so they don't show
up in process listings either.
"""

import asyncio
import calendar
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

import feedparser

from . import cover, db

log = logging.getLogger("refeed.fetcher")

USER_AGENT = "podcast-rss-refeed/1.0 (+https://github.com/dustinreeves/podcast-rss-refeed)"
CONCURRENCY = 5
MAX_FEED_BYTES = 50 * 1024 * 1024
MAX_ART_BYTES = 20 * 1024 * 1024
# Optional HTTP proxy (e.g. a VPN container) for feeds that block datacenter IPs.
FETCH_PROXY = os.environ.get("FETCH_PROXY") or None
CURL = os.environ.get("CURL_BINARY") or shutil.which("curl") or "curl"

# curl exit codes worth explaining (https://curl.se/docs/manpage.html#EXIT)
CURL_ERRORS = {
    5: "proxy address not found",
    6: "host not found",
    7: "couldn't connect",
    28: "timed out",
    35: "TLS handshake failed",
    47: "too many redirects",
    52: "empty reply",
    56: "connection dropped",
    60: "TLS certificate not trusted",
    63: "file too large",
    97: "proxy refused",
}


class FetchError(Exception):
    """A download failed. The message never contains the URL."""


@dataclass
class Fetched:
    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names, last value


def _quote(value: str) -> str:
    # curl config file string: backslash escapes, no newlines.
    value = value.replace("\r", "").replace("\n", "")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def http_get(
    url: str,
    *,
    use_proxy: bool = False,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_FEED_BYTES,
) -> Fetched:
    """GET a URL with curl, following redirects."""
    config = [
        f"url = {_quote(url)}",
        f"user-agent = {_quote(USER_AGENT)}",
        f"max-filesize = {max_bytes}",
    ]
    if use_proxy and FETCH_PROXY:
        config.append(f"proxy = {_quote(FETCH_PROXY)}")
    for name, value in (headers or {}).items():
        config.append(f"header = {_quote(f'{name}: {value}')}")

    proc = await asyncio.create_subprocess_exec(
        CURL,
        "--silent", "--location", "--max-redirs", "5", "--compressed",
        "--connect-timeout", "15", "--max-time", "120",
        "--output", "-",
        # Status and the final response's headers go to stderr, after the body on stdout.
        "--write-out", "%{stderr}%{http_code}\n%{header_json}",
        "--config", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    body, meta = await proc.communicate("\n".join(config).encode())
    if proc.returncode:
        raise FetchError(CURL_ERRORS.get(proc.returncode, f"download failed (curl {proc.returncode})"))
    status_line, _, header_json = meta.decode(errors="replace").partition("\n")
    try:
        raw = json.loads(header_json) if header_json.strip() else {}
        resp_headers = {k.lower(): (v[-1] if isinstance(v, list) and v else str(v)) for k, v in raw.items()}
        return Fetched(int(status_line), body, resp_headers)
    except ValueError as exc:
        raise FetchError("unexpected response from curl") from exc


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


async def update_art(feed_id: int, image_url: str | None, cached_url: str | None, use_proxy: bool):
    """Download a show's artwork for cover collages when its URL has changed."""
    if not image_url or (image_url == cached_url and cover.art_path(feed_id).exists()):
        return
    try:
        resp = await http_get(image_url, use_proxy=use_proxy, max_bytes=MAX_ART_BYTES)
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status}")
    except FetchError as exc:
        log.warning("feed %s: artwork download failed: %s", feed_id, exc)
        return
    if await asyncio.to_thread(cover.save_show_art, feed_id, resp.content):
        db.update_feed(feed_id, art_url=image_url)


async def refresh_feed(feed) -> str | None:
    """Fetch one feed and store its episodes. Returns an error message or None."""
    use_proxy = bool(feed["use_proxy"])
    headers = {}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]

    try:
        resp = await http_get(feed["url"], use_proxy=use_proxy, headers=headers)
        if resp.status == 304:
            db.update_feed(feed["id"], last_fetched=time.time(), last_error=None)
            await update_art(feed["id"], feed["image"], feed["art_url"], use_proxy)
            return None
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status}")
        info, episodes = await asyncio.to_thread(parse_feed, resp.content)
    except (FetchError, ValueError) as exc:
        msg = str(exc)
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
    await update_art(feed["id"], info["image"] or feed["image"], feed["art_url"], use_proxy)
    log.info("feed %s (%s): %d episodes", feed["id"], info["title"], len(episodes))
    return None


async def refresh_one(feed_id: int) -> str | None:
    feed = db.get_feed(feed_id)
    if feed is None:
        return "feed not found"
    return await refresh_feed(feed)


_refresh_lock = asyncio.Lock()


async def refresh_all(user_id: int | None = None):
    """Refresh every enabled show, or just one user's."""
    if _refresh_lock.locked():
        return  # a refresh is already running
    async with _refresh_lock:
        feeds = [f for f in db.all_feeds(user_id) if f["enabled"]]
        sem = asyncio.Semaphore(CONCURRENCY)

        async def run(feed):
            async with sem:
                await refresh_feed(feed)

        await asyncio.gather(*(run(f) for f in feeds))
        log.info("refreshed %d feeds", len(feeds))


async def refresh_loop(interval_minutes: float):
    while True:
        try:
            await refresh_all()
        except Exception:
            log.exception("refresh failed")
        await asyncio.sleep(interval_minutes * 60)
