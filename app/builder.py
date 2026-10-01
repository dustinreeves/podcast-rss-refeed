"""Build the merged RSS feed from stored episodes."""

import xml.etree.ElementTree as ET
from email.utils import formatdate

from . import db

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("content", CONTENT)
ET.register_namespace("atom", ATOM)


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def build_feed(self_url: str) -> bytes:
    settings = db.get_settings()
    episodes = db.merged_episodes(
        default_max=int(settings["default_max_episodes"]),
        max_items=int(settings["max_items"]),
    )
    prefix = settings["prefix_titles"] == "1"

    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    _sub(channel, "title", settings["title"])
    _sub(channel, "description", settings["description"])
    _sub(channel, "link", self_url)
    _sub(channel, f"{{{ATOM}}}link", href=self_url, rel="self", type="application/rss+xml")
    _sub(channel, "lastBuildDate", formatdate(usegmt=True))
    _sub(channel, f"{{{ITUNES}}}author", "podcast-rss-refeed")
    _sub(channel, f"{{{ITUNES}}}block", "Yes")  # keep directories from listing it
    if settings["image"]:
        _sub(channel, f"{{{ITUNES}}}image", href=settings["image"])
        image = _sub(channel, "image")
        _sub(image, "url", settings["image"])
        _sub(image, "title", settings["title"])
        _sub(image, "link", self_url)

    seen_guids = set()
    for ep in episodes:
        guid = ep["guid"]
        if guid in seen_guids:  # two shows reusing a GUID; keep both distinct
            guid = f"{ep['feed_id']}:{guid}"
        seen_guids.add(guid)

        title = f"[{ep['show_title']}] {ep['title']}" if prefix else ep["title"]
        item = _sub(channel, "item")
        _sub(item, "title", title)
        _sub(item, f"{{{ITUNES}}}title", title)
        _sub(item, f"{{{ITUNES}}}author", ep["show_title"])
        _sub(item, "guid", guid, isPermaLink="false")
        if ep["link"]:
            _sub(item, "link", ep["link"])
        if ep["published"]:
            _sub(item, "pubDate", formatdate(ep["published"], usegmt=True))
        _sub(item, "description", ep["description"] or "")
        _sub(
            item,
            "enclosure",
            url=ep["enclosure_url"],
            type=ep["enclosure_type"],
            length=ep["enclosure_length"],
        )
        if ep["duration"]:
            _sub(item, f"{{{ITUNES}}}duration", ep["duration"])
        if ep["image"] or ep["show_image"]:
            _sub(item, f"{{{ITUNES}}}image", href=ep["image"] or ep["show_image"])
        if ep["explicit"]:
            _sub(item, f"{{{ITUNES}}}explicit", ep["explicit"])

    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
