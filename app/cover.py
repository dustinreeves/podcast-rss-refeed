"""Automatic cover art for merged feeds.

A grid of the shows' artwork with the feed's name in large type across the
bottom. Podcast apps show cover art as small as ~60px, so the layout stays
coarse (at most a 3x3 grid) and the title is sized to remain readable there.
"""

import hashlib
import io
import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import db

log = logging.getLogger("refeed.cover")

SIZE = 1400  # Apple Podcasts' minimum; keeps the JPEG small
LAYOUT_VERSION = "3"  # bump when the drawing changes, so cached covers are rebuilt
BAND = (20, 19, 18)  # title band, also used for empty grid cells
PLUS_TILE = (48, 46, 43)
TEXT = (255, 255, 255)

FONT_CANDIDATES = [
    os.environ.get("COVER_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Docker image (fonts-dejavu-core)
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _art_dir() -> Path:
    return db.DATA_DIR / "art"


def _cover_dir() -> Path:
    return db.DATA_DIR / "covers"


def art_path(feed_id: int) -> Path:
    return _art_dir() / f"{feed_id}.jpg"


# ------------------------------------------------------------- show artwork


def save_show_art(feed_id: int, content: bytes) -> bool:
    """Store a show's artwork, square-cropped and resized. False if it isn't an image."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, (SIZE, SIZE), Image.LANCZOS)
    except Exception as exc:  # corrupt, unsupported or absurdly large images
        log.warning("feed %s: artwork unusable (%s)", feed_id, type(exc).__name__)
        return False
    _art_dir().mkdir(parents=True, exist_ok=True)
    img.save(art_path(feed_id), "JPEG", quality=88)
    return True


def delete_show_art(feed_id: int):
    art_path(feed_id).unlink(missing_ok=True)


# ------------------------------------------------------------- feed covers


def _shows_with_art(collection) -> list:
    return [f for f in db.collection_feeds(collection) if f["art_url"] and art_path(f["id"]).exists()]


def version(collection, shows=None) -> str:
    """Changes whenever the cover would look different, for cache-busting URLs."""
    shows = _shows_with_art(collection) if shows is None else shows
    parts = [LAYOUT_VERSION, collection["title"]] + [f"{f['id']}:{f['art_url']}" for f in shows]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def cover_file(collection) -> Path:
    """Path to the collection's cover, drawing it first if it's missing or stale."""
    shows = _shows_with_art(collection)
    path = _cover_dir() / f"{collection['id']}-{version(collection, shows)}.jpg"
    if not path.exists():
        _cover_dir().mkdir(parents=True, exist_ok=True)
        for old in _cover_dir().glob(f"{collection['id']}-*.jpg"):
            old.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        render(collection["title"], [art_path(f["id"]) for f in shows]).save(
            tmp, "JPEG", quality=88, optimize=True
        )
        tmp.replace(path)
    return path


def delete_covers(collection_id: int):
    for old in _cover_dir().glob(f"{collection_id}-*.jpg"):
        old.unlink(missing_ok=True)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str] | None:
    """Greedy word wrap; None if it doesn't fit in max_lines."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            continue
        if not line or draw.textlength(word, font=font) > width:
            return None
        lines.append(line)
        line = word
    lines.append(line)
    return lines if len(lines) <= max_lines else None


def _fit_title(draw, title: str, width: int, height: int):
    """Largest font (and line split) that fits the title in two lines within the box."""
    for size in range(240, 89, -10):
        font = _font(size)
        lines = _wrap(draw, title, font, width, max_lines=2)
        if lines and round(size * 1.12) * len(lines) <= height:
            return font, lines
    font = _font(90)
    text = title
    while text and draw.textlength(text + "\u2026", font=font) > width:
        text = text[:-1]
    return font, [text.rstrip() + "\u2026"]


def _layout(n: int) -> tuple[int, int, list[int | str]]:
    """(columns, rows, cells) for n shows. Cells hold an art index or '+N'.

    The title band always takes the space of one more row, so it never covers art.
    At most 3 columns x 2 rows: busier grids turn to mush at thumbnail size.
    """
    if n == 1:
        return 1, 1, [0]
    if n == 2:
        return 2, 1, [0, 1]
    if n == 3:  # second row shifted, so the repeat reads as a pattern, not stripes
        return 3, 2, [0, 1, 2, 1, 2, 0]
    if n <= 6:  # repeat the art to fill both rows rather than leave dark holes
        return 3, 2, [i % n for i in range(6)]
    return 3, 2, list(range(5)) + [f"+{n - 5}"]


def render(title: str, art_files: list[Path]) -> Image.Image:
    canvas = Image.new("RGB", (SIZE, SIZE), BAND)
    draw = ImageDraw.Draw(canvas)
    pad = SIZE // 18
    n = len(art_files)

    if n == 0:  # no artwork yet: just the title, centred
        band_top = 0
    elif n == 1:  # one show: its art fills the cover; the band covers only what the title needs
        with Image.open(art_files[0]) as art:
            canvas.paste(art.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS), (0, 0))
        font, lines = _fit_title(draw, title, SIZE - 2 * pad, SIZE // 3 - 2 * pad)
        band_top = SIZE - round(font.size * 1.12) * len(lines) - 2 * pad
        draw.rectangle((0, band_top, SIZE, SIZE), fill=BAND)
    else:
        cols, rows, cells = _layout(n)
        tile = SIZE // cols
        band_top = tile * rows
        for i, cell in enumerate(cells):
            x, y = (i % cols) * tile, (i // cols) * tile
            w = SIZE - x if i % cols == cols - 1 else tile  # absorb rounding at the right edge
            if isinstance(cell, int):
                with Image.open(art_files[cell]) as art:
                    canvas.paste(art.convert("RGB").resize((w, tile), Image.LANCZOS), (x, y))
            else:  # "+N more"
                draw.rectangle((x, y, x + w, y + tile), fill=PLUS_TILE)
                draw.text((x + w / 2, y + tile / 2), cell, font=_font(tile // 3), fill=TEXT, anchor="mm")

    font, lines = _fit_title(draw, title, SIZE - 2 * pad, SIZE - band_top - 2 * pad)
    line_h = round(font.size * 1.12)
    top = band_top + (SIZE - band_top - line_h * len(lines)) // 2  # centred in the band
    for i, line in enumerate(lines):
        draw.text((pad, top + i * line_h), line, font=font, fill=TEXT, anchor="la")
    return canvas
