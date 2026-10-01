"""Data for the web player.

The browser streams audio straight from each show's own server (the episode's
enclosure URL), exactly as a podcast app would; nothing is downloaded or
proxied here. Only listening progress is stored.
"""

import html
import re

from . import db

PAGE_SIZE = 100
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def parse_duration(value) -> float | None:
    """itunes:duration as seconds: '3723', '1:02:03' or '62:03'."""
    if value is None:
        return None
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds or None


def plain_text(value: str | None, limit: int = 1500) -> str:
    """Show notes as plain text: feed HTML is never rendered as markup."""
    text = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", value or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def episodes_for(collection, user_id: int, limit: int) -> tuple[list[dict], bool]:
    """A page of the collection's episodes with this user's progress, and whether
    there are more."""
    cap = collection["max_items"]
    n = min(limit, cap) if cap else limit
    rows = db.merged_episodes(collection, max_items=n + 1)
    more = len(rows) > n
    rows = rows[:n]
    listens = db.listens_for(user_id, [r["id"] for r in rows])
    items = []
    for r in rows:
        listen = listens.get(r["id"])
        items.append(
            {
                "id": r["id"],
                "title": r["title"] or "Untitled episode",
                "show": r["show_title"],
                "url": r["enclosure_url"],
                "type": r["enclosure_type"],
                "image": r["image"] or r["show_image"] or "",
                "published": r["published"],
                "duration": parse_duration(r["duration"]) or (listen["duration"] if listen else None),
                "position": listen["position"] if listen else 0,
                "played": bool(listen and listen["played"]),
                "updated": listen["updated_at"] if listen else 0,
                "notes": plain_text(r["description"]),
                "link": r["link"] or "",
            }
        )
    return items, more
