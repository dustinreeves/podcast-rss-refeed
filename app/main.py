"""Web UI + merged feed endpoints."""

import asyncio
import hashlib
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, builder, cover, db, fetcher, opml
from .auth import admin_user, current_user

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


def bootstrap_admin():
    """Make sure someone can sign in: create the admin from ADMIN_USER/ADMIN_PASSWORD,
    or log a one-time link for creating it in the browser."""
    if db.count_users():
        return
    if ADMIN_PASSWORD:
        db.create_user(ADMIN_USER, auth.hash_password(ADMIN_PASSWORD), is_admin=True)
        log.info("created admin account %r from ADMIN_USER/ADMIN_PASSWORD", ADMIN_USER)
        if ADMIN_PASSWORD == "change-me":
            log.warning("the admin password is the example value - change it in the web UI (Account)")
        return
    token = db.create_invite(None, days=1)
    log.warning(
        "No accounts yet. Create the admin account here (link works once, for 24 hours): %s/signup?invite=%s",
        BASE_URL or "http://<this-server>:8080",
        token,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    bootstrap_admin()
    run_in_background(fetcher.refresh_loop(REFRESH_MINUTES))
    yield
    for task in _background:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    # Defence in depth next to SameSite cookies: refuse cross-site form posts.
    if request.method == "POST" and not _same_origin(request):
        return Response("cross-origin request blocked", status_code=403)
    return await call_next(request)


def _same_origin(request: Request) -> bool:
    # Browsers send Sec-Fetch-Site on every request, whatever the referrer policy.
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")  # "none": typed URL or bookmark
    # Older clients: compare Origin/Referer. A "Referrer-Policy: no-referrer" header
    # (common in reverse-proxy configs) makes browsers send "Origin: null" on form
    # posts, which says nothing about where the post came from, so it isn't blocked.
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


def base_url(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def feed_url(request: Request, token: str) -> str:
    return f"{base_url(request)}/feed/{token}.xml"


def cover_url(request: Request, collection, absolute: bool = True) -> str:
    # The version changes with the cover, so podcast apps notice new artwork.
    base = base_url(request) if absolute else ""
    return f"{base}/cover/{collection['token']}.jpg?v={cover.version(collection)}"


def back(msg: str = "", anchor: str = "", to: str = "/") -> RedirectResponse:
    url = f"{to}?msg={quote(msg)}" if msg else to
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


def page(request: Request, name: str, user=None, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request, name, {"user": user, **context}, status_code=status_code
    )


def opml_response(feeds, title: str) -> Response:
    filename = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-") or "refeed"
    return Response(
        opml.build_opml(feeds, title),
        media_type="text/x-opml",
        headers={"Content-Disposition": f'attachment; filename="{filename}.opml"'},
    )


def owned_feed(user, feed_id: int):
    feed = db.get_feed(feed_id, user["id"])
    if feed is None:
        raise HTTPException(404)
    return feed


def owned_collection(user, collection_id: int):
    collection = db.get_collection(collection_id, user["id"])
    if collection is None:
        raise HTTPException(404)
    return collection


# --------------------------------------------------------------- merged feeds
# Public: podcast apps can't sign in, so the unguessable token is the key.


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


# ------------------------------------------------------------------- accounts


@app.get("/login")
def login_page(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token and db.user_for_session(token):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    key = auth.client_key(request)
    if auth.throttle.blocked(key):
        return page(
            request, "login.html", error="Too many failed sign-ins. Try again in 15 minutes.",
            username=username, status_code=429,
        )
    user = auth.authenticate(username, password)
    if user is None:
        auth.throttle.fail(key)
        return page(
            request, "login.html", error="Wrong username or password.", username=username,
            status_code=401,
        )
    auth.throttle.reset(key)
    response = RedirectResponse("/", status_code=303)
    auth.start_session(response, request, user["id"], BASE_URL)
    return response


@app.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.end_session(response, request)
    return response


@app.get("/signup")
def signup_page(request: Request, invite: str = ""):
    link = db.valid_invite(invite)
    if not link:
        return page(request, "signup.html", invalid=True, status_code=404)
    return page(request, "signup.html", invite=invite, reset_for=link["for_username"])


@app.post("/signup")
def signup(
    request: Request,
    invite: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
):
    link = db.valid_invite(invite)
    if not link:
        return page(request, "signup.html", invalid=True, status_code=404)
    if link["for_user"]:  # a password reset link
        error = auth.check_new_credentials(None, password, confirm)
        if error:
            return page(
                request, "signup.html", invite=invite, reset_for=link["for_username"],
                error=error, status_code=400,
            )
        user_id = db.reset_password_with_invite(invite, auth.hash_password(password))
        if user_id is None:
            return page(request, "signup.html", invalid=True, status_code=404)
        response = back("Password set. You're signed in.")
        auth.start_session(response, request, user_id, BASE_URL)
        return response

    username = username.strip()
    error = auth.check_new_credentials(username, password, confirm)
    if not error:
        try:
            user_id = db.signup_with_invite(invite, username, auth.hash_password(password))
        except sqlite3.IntegrityError:
            error = "That username is taken."
        else:
            if user_id is None:
                return page(request, "signup.html", invalid=True, status_code=404)
            log.info("new account %r", username)
            response = back("Welcome! Add some shows to get started.")
            auth.start_session(response, request, user_id, BASE_URL)
            return response
    return page(request, "signup.html", invite=invite, username=username, error=error, status_code=400)


@app.get("/account")
def account_page(request: Request, msg: str = "", user=Depends(current_user)):
    return page(request, "account.html", user, msg=msg)


@app.post("/account/password")
def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    user=Depends(current_user),
):
    error = None
    if not auth.verify_password(current, user["password_hash"]):
        error = "Your current password isn't right."
    else:
        error = auth.check_new_credentials(None, password, confirm)
    if error:
        return page(request, "account.html", user, error=error, status_code=400)
    db.update_user(user["id"], password_hash=auth.hash_password(password))
    db.delete_user_sessions(user["id"], keep_token=request.cookies.get(auth.COOKIE))
    return back("Password changed. You've been signed out everywhere else.", to="/account")


# ---------------------------------------------------------------------- admin


def admin_page(request: Request, user, **context):
    return page(
        request, "admin.html", user,
        users=db.list_users(), invites=db.pending_invites(), invite_days=db.INVITE_DAYS, **context,
    )


@app.get("/admin")
def admin(request: Request, msg: str = "", user=Depends(admin_user)):
    return admin_page(request, user, msg=msg)


@app.post("/admin/invites")
def create_invite(request: Request, user=Depends(admin_user)):
    token = db.create_invite(user["id"])
    # Shown once: only a hash of the token is stored.
    return admin_page(request, user, new_invite=f"{base_url(request)}/signup?invite={token}")


@app.post("/admin/users/{user_id}/reset")
def password_reset_link(request: Request, user_id: int, user=Depends(admin_user)):
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404)
    token = db.create_invite(user["id"], days=1, for_user=user_id)
    return admin_page(
        request, user,
        new_reset=f"{base_url(request)}/signup?invite={token}", reset_name=target["username"],
    )


@app.post("/admin/invites/{invite_id}/revoke")
def revoke_invite(invite_id: int, user=Depends(admin_user)):
    db.revoke_invite(invite_id)
    return back("Invite revoked.", to="/admin")


@app.post("/admin/users/{user_id}/admin")
def toggle_admin(user_id: int, make_admin: str = Form(""), user=Depends(admin_user)):
    if user_id == user["id"]:
        return back("You can't change your own admin rights.", to="/admin")
    db.update_user(user_id, is_admin=1 if make_admin else 0)
    return back("Saved.", to="/admin")


@app.post("/admin/users/{user_id}/delete")
def delete_user(user_id: int, user=Depends(admin_user)):
    if user_id == user["id"]:
        return back("You can't delete your own account.", to="/admin")
    feed_ids, collection_ids = db.delete_user(user_id)
    for feed_id in feed_ids:
        cover.delete_show_art(feed_id)
    for collection_id in collection_ids:
        cover.delete_covers(collection_id)
    return back("Account deleted, with all of its shows and feeds.", to="/admin")


# ---------------------------------------------------------------- main page


@app.get("/")
def index(request: Request, msg: str = "", preview: int | None = None, user=Depends(current_user)):
    collections = db.list_collections(user["id"])
    shown = next((c for c in collections if c["id"] == preview), collections[0] if collections else None)
    return page(
        request,
        "index.html",
        user,
        feeds=db.list_feeds(user["id"]),
        collections=collections,
        picked_collections=[c for c in collections if not c["all_shows"]],
        memberships=db.memberships(user["id"]),
        feed_urls={c["id"]: feed_url(request, c["token"]) for c in collections},
        cover_urls={c["id"]: c["image"] or cover_url(request, c, absolute=False) for c in collections},
        preview_collection=shown,
        preview=db.merged_episodes(shown, max_items=25) if shown else [],
        msg=msg,
        refreshing=fetcher._refresh_lock.locked(),
        proxy_available=bool(fetcher.FETCH_PROXY),
    )


# ---- collections (merged feeds)


@app.post("/collections")
def add_collection(title: str = Form(...), all_shows: str = Form(""), user=Depends(current_user)):
    title = title.strip()
    if not title:
        return back("Give the feed a name.")
    db.add_collection(user["id"], title, all_shows=bool(all_shows))
    if all_shows:
        return back(f'Created "{title}" with every show in it.')
    return back(f'Created "{title}". Now tick it on the shows that belong in it.')


@app.post("/collections/{collection_id}/update")
def update_collection(
    collection_id: int,
    title: str = Form(...),
    description: str = Form(""),
    image: str = Form(""),
    max_items: int = Form(300),
    prefix_titles: str = Form(""),
    all_shows: str = Form(""),
    user=Depends(current_user),
):
    owned_collection(user, collection_id)
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


@app.post("/collections/{collection_id}/rotate")
def rotate_collection_token(collection_id: int, user=Depends(current_user)):
    owned_collection(user, collection_id)
    db.update_collection(collection_id, token=db.new_token())
    return back("New link generated - resubscribe in your podcast app with the new link.")


@app.post("/collections/{collection_id}/delete")
def delete_collection(collection_id: int, user=Depends(current_user)):
    owned_collection(user, collection_id)
    if len(db.list_collections(user["id"])) <= 1:
        return back("You need at least one merged feed.")
    db.delete_collection(collection_id)
    cover.delete_covers(collection_id)
    return back("Feed deleted. Its shows are still in your library.")


@app.get("/collections/{collection_id}/export.opml")
def export_collection_opml(collection_id: int, user=Depends(current_user)):
    collection = owned_collection(user, collection_id)
    return opml_response(db.collection_feeds(collection), collection["title"])


# ---- shows (source feeds)


@app.post("/feeds")
async def add_feed(
    url: str = Form(...),
    use_proxy: str = Form(""),
    collections: list[int] = Form([]),
    user=Depends(current_user),
):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return back("Feed URL must start with http:// or https://")
    feed_id = db.add_feed(user["id"], url)
    if feed_id is None:
        db.add_feeds_to_collections([db.feed_id_by_url(user["id"], url)], collections)
        extra = " Added it to the feeds you picked." if collections else ""
        return back("That show is already in your library." + extra)
    db.add_feeds_to_collections([feed_id], collections)
    if use_proxy:
        db.update_feed(feed_id, use_proxy=1)
    error = await fetcher.refresh_one(feed_id)
    if error:
        return back(f"Added, but fetching failed: {error}")
    return back("Show added.")


@app.post("/feeds/import")
async def import_opml(
    file: UploadFile = File(...), collections: list[int] = Form([]), user=Depends(current_user)
):
    try:
        urls = opml.parse_opml(await file.read())
    except Exception:
        return back("Couldn't read that file - is it an OPML export?")
    added = sum(1 for url in urls if db.add_feed(user["id"], url) is not None)
    db.add_feeds_to_collections([db.feed_id_by_url(user["id"], u) for u in urls], collections)
    run_in_background(fetcher.refresh_all(user["id"]))
    return back(f"Imported {added} new shows ({len(urls) - added} already present). Fetching now...")


@app.get("/feeds/export.opml")
def export_opml(user=Depends(current_user)):
    return opml_response(db.list_feeds(user["id"]), "All shows")


@app.post("/feeds/{feed_id}/collections/{collection_id}")
def toggle_membership(
    feed_id: int, collection_id: int, member: str = Form(""), user=Depends(current_user)
):
    owned_feed(user, feed_id)
    owned_collection(user, collection_id)
    current = db.memberships(user["id"]).get(feed_id, set())
    db.set_feed_collections(
        feed_id, current | {collection_id} if member else current - {collection_id}
    )
    return back(anchor=f"show-{feed_id}")  # land back on the show, not the top of the page


@app.post("/feeds/{feed_id}/update")
def update_feed(
    feed_id: int,
    title_override: str = Form(""),
    max_episodes: str = Form(""),
    enabled: str = Form(""),
    use_proxy: str = Form(""),
    user=Depends(current_user),
):
    owned_feed(user, feed_id)
    db.update_feed(
        feed_id,
        title_override=title_override.strip() or None,
        max_episodes=int(max_episodes) if max_episodes.strip().isdigit() else None,
        enabled=1 if enabled else 0,
        use_proxy=1 if use_proxy else 0,
    )
    return back("Saved.")


@app.post("/feeds/{feed_id}/refresh")
async def refresh_feed(feed_id: int, user=Depends(current_user)):
    owned_feed(user, feed_id)
    error = await fetcher.refresh_one(feed_id)
    return back(f"Refresh failed: {error}" if error else "Refreshed.")


@app.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int, user=Depends(current_user)):
    owned_feed(user, feed_id)
    db.delete_feed(feed_id)
    cover.delete_show_art(feed_id)
    return back("Show removed.")


@app.post("/refresh")
def refresh_all(user=Depends(current_user)):
    run_in_background(fetcher.refresh_all(user["id"]))
    return back("Refreshing your shows in the background - reload in a moment.")


@app.post("/settings")
def save_settings(default_max_episodes: int = Form(25), user=Depends(current_user)):
    db.update_user(user["id"], default_max_episodes=max(0, default_max_episodes))  # 0 = no limit
    return back("Settings saved.")
