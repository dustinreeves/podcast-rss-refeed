"""OPML import/export, so subscriptions can move in and out of podcast apps."""

import xml.etree.ElementTree as ET


def parse_opml(content: bytes) -> list[str]:
    """Return every feed URL (xmlUrl) in an OPML document, in order, deduplicated."""
    root = ET.fromstring(content)
    urls = []
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def build_opml(feeds, title: str) -> bytes:
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(opml, "body")
    for feed in feeds:
        name = feed["title_override"] or feed["title"] or feed["url"]
        ET.SubElement(body, "outline", type="rss", text=name, title=name, xmlUrl=feed["url"])
    ET.indent(opml)
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True)
