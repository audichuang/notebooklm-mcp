"""Podcast publishing MCP tool. publish_series renders the whole season in memory
and PUTs each file to the feed host's LAN-only uploader (token-gated). The MCP
host writes nothing durable and needs no NAS mount — it reaches the NAS over the
internal network (PODCAST_UPLOAD_URL = http://<nas-lan-ip>:<port>).

Commit order (the uploader is a dumb landing zone, so the CLIENT enforces order):
media (mp3 + artwork) -> show.json -> feed.xml/index.html. Combined with "the
uploader never deletes", a reader on the public read port never sees a feed.xml
that points at a missing enclosure."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from . import runtime
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity
from .publish.layout import attachment_filename, media_filename
from .publish import notes_html

_TZ = timezone(timedelta(hours=8))          # Asia/Taipei, RFC-2822 +0800
_TIMEOUT = 600.0                            # a season of mp3 PUTs can take a while
# Deterministic fallback pubDate for legacy manifests without published_at:
# a fixed base PLUS (n-1) days -> distinct per episode, higher episode = later
# date (SAME direction as the published_at path: EP01 oldest, EPn newest), and
# byte-stable across republish (never a wall clock, so two publishes render an
# identical feed.xml). base 2020-01-01 + 98 days is still 2020 -> always past.
_FALLBACK_BASE = datetime(2020, 1, 1, 9, 0, 0, tzinfo=_TZ)


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"{name} is required (inject via Doppler) but is unset/empty")
    return val


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be non-empty")
    return value


def _fallback_pub_date(n: int) -> str:
    return format_datetime(_FALLBACK_BASE + timedelta(days=n - 1))


def _make_client() -> httpx.AsyncClient:
    """One seam: tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


async def _ensure_local_mp3(notebook_id: str, ep: dict) -> str:
    """Local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; else fail-fast."""
    path = ep.get("mp3_path")
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    artifact_id = ep.get("artifact_id")
    if not artifact_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no artifact_id to re-download"
        )
    staging = path or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"ep{ep['episode']:02d}.mp3"
    )
    os.makedirs(os.path.dirname(staging), exist_ok=True)
    client = runtime.get_client()
    await client.artifacts.download_audio(notebook_id, staging, artifact_id)
    if not (os.path.exists(staging) and os.path.getsize(staging) > 0):
        raise ValueError(
            f"episode {ep.get('episode')}: re-download via artifact {artifact_id} produced no file"
        )
    return staging


async def _auth_precheck(client, base: str, upload_token: str) -> None:
    """Probe GET /healthz WITH the bearer before sending any big mp3, so a wrong
    token (or a PODCAST_UPLOAD_URL mis-pointed at the read-only Caddy) fails fast
    instead of surfacing as a mid-PUT connection reset. Require BOTH 200 AND the
    uploader's marker header — Caddy's /healthz also returns 200 but lacks it."""
    r = await client.get(f"{base}/healthz",
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 200 or r.headers.get("X-Podcast-Uploader") != "1":
        raise ValueError(
            f"uploader auth precheck failed (status={r.status_code}, "
            f"marker={r.headers.get('X-Podcast-Uploader')!r}): check PODCAST_UPLOAD_URL "
            "points at the uploader (not the read Caddy) and PODCAST_UPLOAD_TOKEN "
            "matches the NAS .env UPLOAD_TOKEN"
        )


async def _put(client, base: str, token: str, upload_token: str, name: str, data: bytes) -> None:
    url = f"{base}/feeds/{token}/{name}"
    r = await client.put(url, content=data,
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 201:
        raise ValueError(f"upload failed: PUT {name} -> {r.status_code} {r.text[:300]}")


@mcp.tool()
async def publish_series(
    show_id: str,
    notebook_id: str,
    manifest_path: str,
    show_title: str,
    show_description: str,
    author: str,
    owner_name: str,
    owner_email: str,
    artwork_path: str,
    category: str = "Technology",
    explicit: bool = False,
) -> dict:
    """Publish a whole podcast series (one topic = one feed) as a static RSS feed.

    Reads series_manifest.json, content-hashes each episode mp3, renders an
    Apple-compliant feed.xml + index.html in memory, and PUTs the season to the
    feed host's uploader. Deterministic: same show_id -> same URL/token; manifest
    published_at -> stable pubDates; mp3 content -> stable enclosure URL. The
    uploader lands each file atomically and never deletes, so a regenerated
    episode gets a NEW immutable mp3 URL while old cached URLs keep working."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    salt = _require_env("PODCAST_TOKEN_SALT")
    upload_url = _require_env("PODCAST_UPLOAD_URL").rstrip("/")
    upload_token = _require_env("PODCAST_UPLOAD_TOKEN")

    identity.validate_show_id(show_id)
    _require(notebook_id, "notebook_id")
    _require(show_title, "show_title")
    _require(show_description, "show_description")
    _require(author, "author")
    _require(owner_name, "owner_name")
    _require(owner_email, "owner_email")
    # Validate artwork up front (fail-fast before any upload). Extension follows
    # the real format so a JPEG is never served as .png.
    art_info = artwork_mod.validate_artwork(artwork_path)
    artwork_file = "artwork.jpg" if art_info["format"] == "JPEG" else "artwork.png"

    token = identity.make_token(show_id, salt)

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_eps = manifest.get("episodes", [])
    if not manifest_eps:
        raise ValueError(f"manifest has no episodes: {manifest_path}")
    # Preflight the WHOLE manifest before any upload, so bad data fails fast
    # instead of after some media already landed. EP\d{2} on the wire caps a feed
    # at 99 episodes; enforce that + integer + uniqueness + non-empty title here.
    seen_n: set[int] = set()
    for ep in manifest_eps:
        n = ep.get("episode")
        if not isinstance(n, int) or not (1 <= n <= 99):
            raise ValueError(f"episode number must be an int in 1..99, got: {n!r}")
        if n in seen_n:
            raise ValueError(f"duplicate episode number in manifest: {n}")
        seen_n.add(n)
        if not isinstance(ep.get("title"), str) or not ep["title"].strip():
            raise ValueError(f"episode {n}: title is required and must be non-empty")

    # One mp3 in RAM at a time: read -> hash -> PUT -> drop. NEVER accumulate the
    # whole season (8-12 episodes x tens of MB = 300-600MB resident on a possibly
    # small VM). Commit order still holds: every mp3 + artwork is PUT inside this
    # loop, and show.json/feed.xml are rendered and PUT only AFTER it.
    new_eps: dict[str, dict] = {}
    published = []
    async with _make_client() as client:
        await _auth_precheck(client, upload_url, upload_token)

        for ep in manifest_eps:                                    # 1) media: mp3
            n = int(ep["episode"])
            local = await _ensure_local_mp3(notebook_id, ep)
            with open(local, "rb") as f:
                mp3_bytes = f.read()
            hash8 = hashlib.sha256(mp3_bytes).hexdigest()[:8]      # hash bytes we already read
            mfile = media_filename(n, hash8)
            await _put(client, upload_url, token, upload_token, mfile, mp3_bytes)
            del mp3_bytes

            # 1b) media: 選填附件(簡報 PDF / 研讀講義 HTML),content-addressed,
            #     公開 URL append 到單集 description。缺檔 fail-fast(不 re-download)。
            base_pub = base_url.rstrip("/")
            desc = (ep.get("description") or "").strip() or ep["title"]
            links: list[str] = []

            spath = ep.get("slides_pdf_path")
            if spath:
                if not (os.path.exists(spath) and os.path.getsize(spath) > 0):
                    raise ValueError(f"episode {n}: slides_pdf_path missing file: {spath}")
                with open(spath, "rb") as f:
                    pdf_bytes = f.read()
                pfile = attachment_filename(n, hashlib.sha256(pdf_bytes).hexdigest()[:8], "pdf")
                await _put(client, upload_url, token, upload_token, pfile, pdf_bytes)
                links.append(f"📄 本集簡報:{base_pub}/feeds/{token}/{pfile}")
                del pdf_bytes

            rpath = ep.get("report_md_path")
            if rpath:
                if not (os.path.exists(rpath) and os.path.getsize(rpath) > 0):
                    raise ValueError(f"episode {n}: report_md_path missing file: {rpath}")
                with open(rpath, encoding="utf-8") as f:
                    html_bytes = notes_html.render_report_html(f.read(), ep["title"]).encode("utf-8")
                hfile = attachment_filename(n, hashlib.sha256(html_bytes).hexdigest()[:8], "html")
                await _put(client, upload_url, token, upload_token, hfile, html_bytes)
                links.append(f"📖 研讀講義:{base_pub}/feeds/{token}/{hfile}")

            if links:
                desc = desc + "\n\n" + "\n".join(links)

            new_eps[str(n)] = {
                "title": ep["title"],
                "description": desc,
                "guid": identity.episode_guid(show_id, n),
                "pub_date": ep.get("published_at") or _fallback_pub_date(n),
                "media_file": mfile,
                "length": os.path.getsize(local),   # mp3_bytes 已 del,用檔案大小(同值)
            }
            published.append({
                "n": n, "title": ep["title"], "guid": new_eps[str(n)]["guid"],
                "url": f"{base_pub}/feeds/{token}/{mfile}",
            })

        with open(artwork_path, "rb") as f:                        # 1) media: artwork
            await _put(client, upload_url, token, upload_token, artwork_file, f.read())

        show = {
            "show_id": show_id, "token": token, "notebook_id": notebook_id,
            "title": show_title.strip(), "description": show_description,
            "language": "zh-Hant", "author": author,
            "owner_name": owner_name, "owner_email": owner_email,
            "category": category, "explicit": bool(explicit),
            "artwork_file": artwork_file, "episodes": new_eps,
        }
        show_json = json.dumps(show, ensure_ascii=False, indent=2).encode("utf-8")
        await _put(client, upload_url, token, upload_token, "show.json", show_json)    # 2) state
        await _put(client, upload_url, token, upload_token,                            # 3) derived
                   "feed.xml", feed_mod.build_feed_xml(show, base_url).encode("utf-8"))
        await _put(client, upload_url, token, upload_token,
                   "index.html", feed_mod.build_index_html(show, base_url).encode("utf-8"))

    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len(new_eps),
        "episodes": sorted(published, key=lambda e: e["n"]),
    }


@mcp.tool()
async def feed_info(show_id: str) -> dict:
    """Deterministic feed identity + public URLs for a show_id. Pure computation
    (token = HMAC(salt, show_id)); the MCP keeps no state, so per-episode detail
    is NOT returned — fetch feed.xml over the read port for that."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    salt = _require_env("PODCAST_TOKEN_SALT")
    token = identity.make_token(show_id, salt)     # also validates show_id
    base = f"{base_url.rstrip('/')}/feeds/{token}"
    return {
        "show_id": show_id,
        "token": token,
        "feed_url": f"{base}/feed.xml",
        "show_page_url": f"{base}/index.html",
    }
