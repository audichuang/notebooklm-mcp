"""Podcast publishing MCP tools. Thin I/O orchestration over the pure publish
modules. publish_series is the one-command "publish a whole season" entry point.

Commit order (crash-safe): write all media files -> write show.json (authoritative
state) -> render feed.xml + index.html from the committed state."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from . import runtime
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity, layout, state

_TZ = timezone(timedelta(hours=8))  # Asia/Taipei, RFC-2822 +0800


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"{name} is required (inject via Doppler) but is unset/empty")
    return val


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be non-empty")
    return value


def _now_rfc2822() -> str:
    from email.utils import format_datetime
    return format_datetime(datetime.now(_TZ))


async def _ensure_local_mp3(notebook_id: str, ep: dict) -> str:
    """Return a local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; if that fails, error out."""
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
    episode_overrides: dict | None = None,
) -> dict:
    """Publish a whole podcast series (one topic = one feed) as a static RSS feed.

    Reads the series_manifest.json, copies each episode mp3 to a content-hashed
    public filename, and renders an Apple-compliant feed.xml + index.html under
    a deterministic token directory. Idempotent: re-running keeps the same URL,
    GUIDs, and pubDates; only new/changed episodes move."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    salt = _require_env("PODCAST_TOKEN_SALT")

    identity.validate_show_id(show_id)
    _require(notebook_id, "notebook_id")
    _require(show_description, "show_description")
    _require(show_title, "show_title")
    _require(author, "author")
    _require(owner_name, "owner_name")
    _require(owner_email, "owner_email")
    # Validate artwork once, up front (fail-fast before any writes). Pick the
    # output extension from the real format so a JPEG is never served as .png.
    art_info = artwork_mod.validate_artwork(artwork_path)
    artwork_file = "artwork.jpg" if art_info["format"] == "JPEG" else "artwork.png"

    token = identity.make_token(show_id, salt)
    feed_dir = os.path.join(feeds_root, "feeds", token)

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_eps = manifest.get("episodes", [])
    if not manifest_eps:
        raise ValueError(f"manifest has no episodes: {manifest_path}")

    overrides = episode_overrides or {}
    prior = state.load_show(feed_dir) or {"episodes": {}}
    prior_eps = prior.get("episodes", {})

    # 1) Write all media files first (content-hashed, atomic).
    new_eps: dict[str, dict] = {}
    published = []
    for ep in manifest_eps:
        n = int(ep["episode"])
        prev = prior_eps.get(str(n), {})
        # Spec iron rule: a tombstoned episode number is PERMANENTLY retired and
        # never reused. If a retired number reappears in the manifest (e.g. a
        # source was re-added), keep it dead — do not resurrect or republish it.
        if prev.get("tombstone"):
            new_eps[str(n)] = prev
            continue

        local = await _ensure_local_mp3(notebook_id, ep)
        hash8 = layout.content_hash8(local)
        media_file = layout.media_filename(n, hash8)
        dst = os.path.join(feed_dir, media_file)
        if not os.path.exists(dst):
            layout.atomic_copy(local, dst)
        length = os.path.getsize(dst)

        ov = overrides.get(n) or overrides.get(str(n)) or {}
        new_eps[str(n)] = {
            "title": ep["title"],
            "description": ov.get("description", ep["title"]),
            "guid": prev.get("guid") or state.episode_guid(show_id, n),
            "pub_date": prev.get("pub_date") or _now_rfc2822(),
            "media_file": media_file,
            "length": length,
            "tombstone": False,
        }
        published.append({"n": n, "title": ep["title"],
                          "url": f"{base_url.rstrip('/')}/feeds/{token}/{media_file}",
                          "guid": new_eps[str(n)]["guid"]})

    # Episodes that existed before but are no longer in the manifest -> tombstone
    # (keep their state + file so already-cached clients don't 404; never reuse n).
    for key, prev in prior_eps.items():
        if key not in new_eps:
            tomb = dict(prev)
            tomb["tombstone"] = True
            new_eps[key] = tomb

    # 2) Write authoritative state.
    show = {
        "show_id": show_id,
        "token": token,
        "notebook_id": notebook_id,
        "title": show_title.strip(),
        "description": show_description,
        "language": "zh-Hant",
        "author": author,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "category": category,
        "explicit": bool(explicit),
        "artwork_file": artwork_file,
        "episodes": new_eps,
    }
    layout.atomic_copy(artwork_path, os.path.join(feed_dir, artwork_file))
    state.save_show(feed_dir, show)

    # 3) Render outputs from committed state.
    layout.atomic_write_text(os.path.join(feed_dir, "feed.xml"),
                             feed_mod.build_feed_xml(show, base_url))
    layout.atomic_write_text(os.path.join(feed_dir, "index.html"),
                             feed_mod.build_index_html(show, base_url))

    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len([e for e in new_eps.values() if not e.get("tombstone")]),
        "episodes": sorted(published, key=lambda e: e["n"]),
    }


@mcp.tool()
async def feed_list() -> list:
    """List all published shows by scanning feeds/*/show.json."""
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    root = os.path.join(feeds_root, "feeds")
    shows = []
    if os.path.isdir(root):
        for token in sorted(os.listdir(root)):
            show = state.load_show(os.path.join(root, token))
            if not show:
                continue
            shows.append({
                "show_id": show.get("show_id"),
                "show_title": show.get("title"),
                "token": show.get("token", token),
                "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
                "episode_count": len(
                    [e for e in show.get("episodes", {}).values() if not e.get("tombstone")]
                ),
            })
    return shows


@mcp.tool()
async def feed_info(show_id_or_token: str) -> dict:
    """Return full state for a show, looked up by show_id (preferred) or token."""
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    salt = _require_env("PODCAST_TOKEN_SALT")
    # Try as show_id first (deterministic token), then as a raw token directory.
    candidates = []
    try:
        candidates.append(identity.make_token(show_id_or_token, salt))
    except ValueError:
        pass
    candidates.append(show_id_or_token)
    for token in candidates:
        show = state.load_show(os.path.join(feeds_root, "feeds", token))
        if show:
            return show
    raise ValueError(f"no published show found for: {show_id_or_token}")
