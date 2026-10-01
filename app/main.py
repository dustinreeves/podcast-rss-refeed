"""Web UI + merged feed endpoint."""

import asyncio
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import builder, db, fetcher, opml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("refeed")

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


def back(msg: str = "") -> RedirectResponse:
    return RedirectResponse(f"/?msg={quote(msg)}" if msg else "/", status_code=303)


# ---------------------------------------------------------------- merged feed


@app.get("/feed/{token}.xml")
def merged_feed(token: str, request: Request):
    settings = db.get_settings()
    if not secrets.compare_digest(token, settings["feed_token"]):
        raise HTTPException(404)
    body = builder.build_feed(feed_url(request, token))
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------- web UI


@app.get("/", dependencies=[Depends(require_admin)])
def index(request: Request, msg: str = ""):
    settings = db.get_settings()
    preview = db.merged_episodes(int(settings["default_max_episodes"]), 25)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": db.list_feeds(),
            "settings": settings,
            "feed_url": feed_url(request, settings["feed_token"]),
            "preview": preview,
            "msg": msg,
            "auth_enabled": bool(ADMIN_PASSWORD),
            "refreshing": fetcher._refresh_lock.locked(),
            "proxy_available": bool(fetcher.FETCH_PROXY),
        },
    )


@app.post("/feeds", dependencies=[Depends(require_admin)])
async def add_feed(url: str = Form(...), use_proxy: str = Form("")):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return back("Feed URL must start with http:// or https://")
    feed_id = db.add_feed(url)
    if feed_id is None:
        return back("That feed is already in the list.")
    if use_proxy:
        db.update_feed(feed_id, use_proxy=1)
    error = await fetcher.refresh_one(feed_id)
    if error:
        return back(f"Added, but fetching failed: {error}")
    return back("Feed added.")


@app.post("/feeds/import", dependencies=[Depends(require_admin)])
async def import_opml(file: UploadFile = File(...)):
    try:
        urls = opml.parse_opml(await file.read())
    except Exception:
        return back("Couldn't read that file - is it an OPML export?")
    added = sum(1 for url in urls if db.add_feed(url) is not None)
    run_in_background(fetcher.refresh_all())
    return back(f"Imported {added} new feeds ({len(urls) - added} already present). Fetching now...")


@app.get("/feeds/export.opml", dependencies=[Depends(require_admin)])
def export_opml():
    body = opml.build_opml(db.list_feeds(), db.get_settings()["title"])
    return Response(
        body,
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="refeed-subscriptions.opml"'},
    )


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
    return back("Feed removed.")


@app.post("/refresh", dependencies=[Depends(require_admin)])
def refresh_all():
    run_in_background(fetcher.refresh_all())
    return back("Refreshing all feeds in the background - reload in a moment.")


@app.post("/settings", dependencies=[Depends(require_admin)])
def save_settings(
    title: str = Form(...),
    description: str = Form(""),
    image: str = Form(""),
    max_items: int = Form(300),
    default_max_episodes: int = Form(25),
    prefix_titles: str = Form(""),
):
    db.set_settings(
        {
            "title": title.strip() or "My Podcasts",
            "description": description.strip(),
            "image": image.strip(),
            "max_items": str(max(1, max_items)),
            "default_max_episodes": str(max(1, default_max_episodes)),
            "prefix_titles": "1" if prefix_titles else "0",
        }
    )
    return back("Settings saved.")


@app.post("/token/rotate", dependencies=[Depends(require_admin)])
def rotate_token():
    db.set_settings({"feed_token": db.new_token()})
    return back("New feed URL generated - resubscribe in your podcast app with the new link.")
