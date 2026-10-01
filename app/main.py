"""Web UI + merged feed endpoints."""

import asyncio
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import builder, cover, db, fetcher, opml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("refeed")
# httpx logs every request URL at INFO, and private feed URLs carry access tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
REFRESH_MINUTES = float(os.environ.get("REFRESH_MINUTES", "30"))

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
_background: set[asyncio.Task] = set()


def run_in_background(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set - the web UI is open to anyone who can reach it")
    elif ADMIN_PASSWORD == "change-me":
        log.warning("ADMIN_PASSWORD is still the example value - change it in .env")
    run_in_background(fetcher.refresh_loop(REFRESH_MINUTES))
    yield
    for task in _background:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
security = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(security)):
    if not ADMIN_PASSWORD:
        return
    ok = creds is not None and (
        secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode())
        & secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="refeed"'})


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    # Browsers resend Basic auth automatically, so block cross-site form posts.
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return Response("cross-origin request blocked", status_code=403)
    return await call_next(request)


def feed_url(request: Request, token: str) -> str:
    base = BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/feed/{token}.xml"


def cover_url(request: Request, collection, absolute: bool = True) -> str:
    # The version changes with the cover, so podcast apps notice new artwork.
    base = (BASE_URL or str(request.base_url).rstrip("/")) if absolute else ""
    return f"{base}/cover/{collection['token']}.jpg?v={cover.version(collection)}"


def back(msg: str = "", anchor: str = "") -> RedirectResponse:
    url = f"/?msg={quote(msg)}" if msg else "/"
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


def opml_response(feeds, title: str) -> Response:
    filename = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-") or "refeed"
    return Response(
        opml.build_opml(feeds, title),
        media_type="text/x-opml",
        headers={"Content-Disposition": f'attachment; filename="{filename}.opml"'},
    )


# --------------------------------------------------------------- merged feeds


@app.get("/feed/{token}.xml")
def merged_feed(token: str, request: Request):
    collection = db.collection_by_token(token)
    if collection is None:
        raise HTTPException(404)
    body = builder.build_feed(collection, feed_url(request, token), cover_url(request, collection))
    # lastBuildDate changes every build, so hash everything but that for the ETag.
    etag = '"' + hashlib.sha256(
        b"".join(l for l in body.splitlines() if b"<lastBuildDate>" not in l)
    ).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        body,
        media_type="application/rss+xml; charset=utf-8",
        headers={"ETag": etag, "X-Robots-Tag": "noindex", "Cache-Control": "private, max-age=300"},
    )


@app.get("/cover/{token}.jpg")
def cover_image(token: str):
    collection = db.collection_by_token(token)
    if collection is None:
        raise HTTPException(404)
    return FileResponse(
        cover.cover_file(collection),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400", "X-Robots-Tag": "noindex"},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------- web UI


@app.get("/", dependencies=[Depends(require_admin)])
def index(request: Request, msg: str = "", preview: int | None = None):
    settings = db.get_settings()
    collections = db.list_collections()
    shown = next((c for c in collections if c["id"] == preview), collections[0] if collections else None)
    episodes = (
        db.merged_episodes(shown, int(settings["default_max_episodes"]), max_items=25) if shown else []
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": db.list_feeds(),
            "collections": collections,
            "picked_collections": [c for c in collections if not c["all_shows"]],
            "memberships": db.memberships(),
            "feed_urls": {c["id"]: feed_url(request, c["token"]) for c in collections},
            "cover_urls": {
                c["id"]: c["image"] or cover_url(request, c, absolute=False) for c in collections
            },
            "settings": settings,
            "preview_collection": shown,
            "preview": episodes,
            "msg": msg,
            "auth_enabled": bool(ADMIN_PASSWORD),
            "refreshing": fetcher._refresh_lock.locked(),
            "proxy_available": bool(fetcher.FETCH_PROXY),
        },
    )


# ---- collections (merged feeds)


@app.post("/collections", dependencies=[Depends(require_admin)])
def add_collection(title: str = Form(...), all_shows: str = Form("")):
    title = title.strip()
    if not title:
        return back("Give the feed a name.")
    db.add_collection(title, all_shows=bool(all_shows))
    if all_shows:
        return back(f'Created "{title}" with every show in it.')
    return back(f'Created "{title}". Now tick it on the shows that belong in it.')


@app.post("/collections/{collection_id}/update", dependencies=[Depends(require_admin)])
def update_collection(
    collection_id: int,
    title: str = Form(...),
    description: str = Form(""),
    image: str = Form(""),
    max_items: int = Form(300),
    prefix_titles: str = Form(""),
    all_shows: str = Form(""),
):
    db.update_collection(
        collection_id,
        title=title.strip() or "Untitled feed",
        description=description.strip(),
        image=image.strip(),
        max_items=max(0, max_items),  # 0 = no limit
        prefix_titles=1 if prefix_titles else 0,
        all_shows=1 if all_shows else 0,
    )
    return back("Feed saved.")


@app.post("/collections/{collection_id}/rotate", dependencies=[Depends(require_admin)])
def rotate_collection_token(collection_id: int):
    db.update_collection(collection_id, token=db.new_token())
    return back("New link generated - resubscribe in your podcast app with the new link.")


@app.post("/collections/{collection_id}/delete", dependencies=[Depends(require_admin)])
def delete_collection(collection_id: int):
    if len(db.list_collections()) <= 1:
        return back("You need at least one merged feed.")
    db.delete_collection(collection_id)
    cover.delete_covers(collection_id)
    return back("Feed deleted. Its shows are still in your library.")


@app.get("/collections/{collection_id}/export.opml", dependencies=[Depends(require_admin)])
def export_collection_opml(collection_id: int):
    collection = db.get_collection(collection_id)
    if collection is None:
        raise HTTPException(404)
    return opml_response(db.collection_feeds(collection), collection["title"])


# ---- shows (source feeds)


@app.post("/feeds", dependencies=[Depends(require_admin)])
async def add_feed(
    url: str = Form(...), use_proxy: str = Form(""), collections: list[int] = Form([])
):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return back("Feed URL must start with http:// or https://")
    feed_id = db.add_feed(url)
    if feed_id is None:
        db.add_feeds_to_collections([db.feed_id_by_url(url)], collections)
        extra = " Added it to the feeds you picked." if collections else ""
        return back("That show is already in your library." + extra)
    db.add_feeds_to_collections([feed_id], collections)
    if use_proxy:
        db.update_feed(feed_id, use_proxy=1)
    error = await fetcher.refresh_one(feed_id)
    if error:
        return back(f"Added, but fetching failed: {error}")
    return back("Show added.")


@app.post("/feeds/import", dependencies=[Depends(require_admin)])
async def import_opml(file: UploadFile = File(...), collections: list[int] = Form([])):
    try:
        urls = opml.parse_opml(await file.read())
    except Exception:
        return back("Couldn't read that file - is it an OPML export?")
    added = sum(1 for url in urls if db.add_feed(url) is not None)
    db.add_feeds_to_collections([db.feed_id_by_url(u) for u in urls], collections)
    run_in_background(fetcher.refresh_all())
    return back(f"Imported {added} new shows ({len(urls) - added} already present). Fetching now...")


@app.get("/feeds/export.opml", dependencies=[Depends(require_admin)])
def export_opml():
    return opml_response(db.list_feeds(), "All shows")


@app.post("/feeds/{feed_id}/collections/{collection_id}", dependencies=[Depends(require_admin)])
def toggle_membership(feed_id: int, collection_id: int, member: str = Form("")):
    current = db.memberships().get(feed_id, set())
    db.set_feed_collections(
        feed_id, current | {collection_id} if member else current - {collection_id}
    )
    return back(anchor=f"show-{feed_id}")  # land back on the show, not the top of the page


@app.post("/feeds/{feed_id}/update", dependencies=[Depends(require_admin)])
def update_feed(
    feed_id: int,
    title_override: str = Form(""),
    max_episodes: str = Form(""),
    enabled: str = Form(""),
    use_proxy: str = Form(""),
):
    db.update_feed(
        feed_id,
        title_override=title_override.strip() or None,
        max_episodes=int(max_episodes) if max_episodes.strip().isdigit() else None,
        enabled=1 if enabled else 0,
        use_proxy=1 if use_proxy else 0,
    )
    return back("Saved.")


@app.post("/feeds/{feed_id}/refresh", dependencies=[Depends(require_admin)])
async def refresh_feed(feed_id: int):
    error = await fetcher.refresh_one(feed_id)
    return back(f"Refresh failed: {error}" if error else "Refreshed.")


@app.post("/feeds/{feed_id}/delete", dependencies=[Depends(require_admin)])
def delete_feed(feed_id: int):
    db.delete_feed(feed_id)
    cover.delete_show_art(feed_id)
    return back("Show removed.")


@app.post("/refresh", dependencies=[Depends(require_admin)])
def refresh_all():
    run_in_background(fetcher.refresh_all())
    return back("Refreshing all shows in the background - reload in a moment.")


@app.post("/settings", dependencies=[Depends(require_admin)])
def save_settings(default_max_episodes: int = Form(25)):
    db.set_settings({"default_max_episodes": str(max(0, default_max_episodes))})  # 0 = no limit
    return back("Settings saved.")
