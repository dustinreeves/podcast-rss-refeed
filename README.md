# podcast-rss-refeed

Merge all your podcasts into **one RSS feed**. Subscribe to a single link in any podcast app and every show you follow, public or private (Patreon, Supercast, ...), shows up in it, newest first.

Self-hosted: one small Docker container with a web UI for managing your shows.

## Why

Podcast apps lock your subscriptions inside the app. A single merged feed works in *any* app, on any device, and you only manage the list in one place. It's also handy for players that only take one feed, like smart speakers, car systems or simple RSS readers.

## Features

- **Web UI**: add feeds by URL, import the OPML export from your current podcast app, export OPML back out.
- **Works in any podcast app**: episodes keep their original audio links and GUIDs, each episode carries its own show's artwork, and titles can be prefixed with the show name (`[Show] Episode`).
- **Per-show control**: cap how many episodes each show contributes, rename shows, pause them.
- **Private feeds stay private**: the merged feed lives at an unguessable URL that you can rotate at any time, it's marked `itunes:block` and `noindex`, and source feed URLs (which often contain access tokens) are never logged.
- **Optional proxy per feed**: send specific feeds through an HTTP proxy, for example a VPN container. Useful for hosts like Patreon that block many datacenter IP addresses.
- **Polite fetching**: conditional requests (ETag / Last-Modified) on a schedule; everything is stored in SQLite.

## Quick start

You need Docker with the Compose plugin.

```bash
mkdir refeed && cd refeed
curl -fsSLO https://raw.githubusercontent.com/dustinreeves/podcast-rss-refeed/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/dustinreeves/podcast-rss-refeed/main/.env.example -o .env
# edit .env: set ADMIN_PASSWORD and BASE_URL
docker compose up -d
```

Open http://localhost:8080 and sign in as `admin` with your password. Add some shows or import your OPML, then subscribe to the link under **Your merged feed** in your podcast app.

To build from source instead, clone the repo and run `docker compose up -d --build`.

### Reaching it from your phone

Podcast apps need to reach the feed over the internet, so put the container behind a reverse proxy with HTTPS. With [Caddy](https://caddyserver.com/):

```
refeed.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

Then set `BASE_URL=https://refeed.example.com` in `.env` and run `docker compose up -d` again.

> **Behind Cloudflare or another bot filter?** Many podcast apps (Overcast, Pocket Casts, Apple Podcasts, Spotify) fetch feeds from their own servers, not from your phone. A bot challenge such as Cloudflare's "Just a moment…" page will break the feed for them. Let `/feed/*` through without a challenge, for example with a WAF skip rule for that path.

## Configuration

Environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_USER` | `admin` | Web UI username |
| `ADMIN_PASSWORD` | *(empty)* | Web UI password. If empty, the UI is open to anyone who can reach it. |
| `BASE_URL` | request host | Public address, e.g. `https://refeed.example.com`, used to build the feed link |
| `REFRESH_MINUTES` | `30` | How often every show is checked for new episodes |
| `FETCH_PROXY` | *(empty)* | HTTP proxy for feeds marked "Fetch through proxy", e.g. `http://gluetun:8888` |
| `DATA_DIR` | `/data` | Where the SQLite database lives (the `refeed-data` volume) |

Feed title, description, cover art, episodes per show and total episodes are set in the web UI.

### Fetching some feeds through a VPN

Some private feed hosts refuse requests from datacenter IP addresses. Run a VPN container with an HTTP proxy next to refeed, such as [gluetun](https://github.com/qdm12/gluetun), point `FETCH_PROXY` at it, and tick **Fetch through proxy** on those feeds. Example `docker-compose.override.yml`:

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun:/dev/net/tun]
    environment:
      - HTTPPROXY=on
      # plus your VPN provider settings, see the gluetun wiki
    restart: unless-stopped
  refeed:
    environment:
      - FETCH_PROXY=http://gluetun:8888
```

Only refeed's feed requests use the proxy. Your podcast app still downloads the audio itself.

## Updating and backups

```bash
docker compose pull && docker compose up -d
```

Everything (shows, cached episodes, settings and the feed link) is in the `refeed-data` volume. To back it up:

```bash
docker compose exec refeed python -c "import sqlite3; sqlite3.connect('/data/refeed.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp refeed:/data/backup.db ./refeed-backup.db
```

You can also keep an OPML export from the web UI as a lightweight backup of your show list.

## Security notes

- Anyone with the merged feed link can listen to everything in it, including paid shows. Treat it like a password. **New link** in the UI makes the old one stop working.
- The web UI uses HTTP Basic auth, so only expose it over HTTPS.
- Audio is not proxied or re-hosted. Podcast apps download episodes straight from each show's host, as they normally would, so the shows still see their downloads.
- This is for your own listening. Please don't use it to republish other people's podcasts, and never share a merged feed that contains paid content.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8080
```

The app is FastAPI with Jinja templates and SQLite, in [`app/`](app):

| File | What it does |
| --- | --- |
| `main.py` | Routes: web UI, merged feed endpoint, auth |
| `fetcher.py` | Fetches and parses source feeds on a schedule |
| `builder.py` | Builds the merged RSS XML |
| `db.py` | SQLite schema and queries |
| `opml.py` | OPML import and export |

## License

[MIT](LICENSE)
