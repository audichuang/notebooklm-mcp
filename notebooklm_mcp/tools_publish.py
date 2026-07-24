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
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from . import runtime
from .manifest_store import ManifestStore
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity
from .publish.layout import attachment_filename, cover_filename, media_filename
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


def _fallback_pub_date(n: int) -> str:
    return format_datetime(_FALLBACK_BASE + timedelta(days=n - 1))


def _make_client() -> httpx.AsyncClient:
    """One seam: tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


async def _ensure_local_mp3(ep: dict, fallback_notebook_id: str | None) -> str:
    """Local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; else fail-fast.

    重抓的筆記本**優先用該集自己的 `ep["notebook_id"]`**(podcast_episode 的
    manifest stub 會寫入)——滾動 feed 每集獨立筆記本,單一 show 層 notebook_id
    會抓錯本;沒有per-episode 欄位才退到呼叫端傳的 fallback。"""
    path = ep.get("mp3_path")
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    artifact_id = ep.get("artifact_id")
    if not artifact_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no artifact_id to re-download"
        )
    notebook_id = ep.get("notebook_id") or fallback_notebook_id
    if not notebook_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no notebook_id to re-download from "
            "(add per-episode notebook_id to the manifest entry, or pass notebook_id to publish_series)"
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


def _embed_cover(mp3_path: str, cover_path: str) -> bytes:
    """把支援的來源正規化成真正 MP3，內嵌 ID3 APIC，回傳決定性 bytes。

    NotebookLM 原始下載常是偽裝成 ``.mp3`` 的 fragmented MP4/AAC；這裡直接轉成
    256 kbps MP3，使實際 container/codec 與 enclosure 的 ``.mp3``/``audio/mpeg``
    契約一致。既有 true MP3 不重編音訊，只重寫封面。其他格式一律 fail-closed。
    這是可被測試 monkeypatch 的 seam(多數 publish 測試用假 audio bytes)。"""
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError

    with open(cover_path, "rb") as f:
        cover = f.read()
    cover_mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"

    try:
        probe = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=format_name:stream=codec_name",
             "-of", "json", mp3_path],
            text=True,
            timeout=30,
        ))
        formats = set(probe["format"]["format_name"].split(","))
        codec = probe["streams"][0]["codec_name"]
    except (KeyError, IndexError, json.JSONDecodeError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        raise ValueError(f"unsupported or unreadable audio: {mp3_path}") from exc

    if formats == {"mp3"} and codec == "mp3":
        source_kind = "mp3"
    elif "mp4" in formats and codec == "aac":
        source_kind = "mp4_aac"
    else:
        raise ValueError(
            f"unsupported audio container/codec for publication: "
            f"format={','.join(sorted(formats)) or 'unknown'} codec={codec or 'unknown'}"
        )

    fd, tmp = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        if source_kind == "mp3":
            shutil.copyfile(mp3_path, tmp)
        else:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", mp3_path,
                 "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                 "-c:a", "libmp3lame", "-b:a", "256k", "-ar", "44100", "-ac", "2",
                 tmp],
                check=True,
            )

        try:
            tags = ID3(tmp)
        except ID3NoHeaderError:
            tags = ID3()
        # 發布契約是一張 authoritative front cover；移除舊圖避免多 APIC frame 讓
        # client 任選錯張，也讓同音檔＋同封面的輸出 bytes 決定性。
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover))
        tags.save(tmp, v2_version=3, padding=lambda _info: 0)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _audio_duration_hms(path: str) -> str | None:
    """Return HH:MM:SS from ffprobe. NotebookLM audio is MP4/DASH with a .mp3
    suffix; mutagen currently reports length=0 for these files."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            text=True,
            timeout=15,
        ).strip()
        seconds = max(0, int(round(float(out))))
    except Exception:
        return None
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@mcp.tool()
async def publish_series(
    manifest_path: str,
    show_id: str | None = None,
    show_title: str | None = None,
    show_description: str | None = None,
    author: str | None = None,
    owner_name: str | None = None,
    owner_email: str | None = None,
    artwork_path: str | None = None,
    category: str | None = None,
    explicit: bool | None = None,
    notebook_id: str | None = None,
    return_episodes: list[int] | None = None,
) -> dict:
    """Publish a whole podcast series (one topic = one feed) as a static RSS feed.

    Reads series_manifest.json, content-hashes each episode mp3, renders an
    Apple-compliant feed.xml + index.html in memory, and PUTs the season to the
    feed host's uploader. Deterministic: same show_id -> same URL/token; manifest
    published_at -> stable pubDates; mp3 content -> stable enclosure URL. The
    uploader lands each file atomically and never deletes, so a regenerated
    episode gets a NEW immutable mp3 URL while old cached URLs keep working.

    **show 欄位存 manifest(v0.2.9 起)**:首次發布顯式傳齊 show 七欄,成功後自動
    存進 manifest["show"];之後滾動加集只傳 ``manifest_path``(+``return_episodes``)
    即沿用——不用重打、也不會打錯覆寫公開節目資訊。顯式參數永遠優先於 manifest
    既存值,且新值會回寫沿用。``category``/``explicit`` 兩邊都沒給時維持舊預設
    "Technology"/False。

    ``notebook_id`` 選填:只當某集 mp3 不在本機時的重抓 fallback,且**每集自己的
    manifest `notebook_id` 欄位優先**(每集獨立筆記本時別傳 show 層的,會抓錯本)。
    ``return_episodes`` 選填:整季照常發布,但回傳的 ``episodes`` 只含指定集號——
    滾動 feed 加一集時傳 ``[N]``,免得回傳隨集數線性膨脹(歷史集 URL 早已在案)。"""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    salt = _require_env("PODCAST_TOKEN_SALT")
    upload_url = _require_env("PODCAST_UPLOAD_URL").rstrip("/")
    upload_token = _require_env("PODCAST_UPLOAD_TOKEN")

    # show 設定解析:顯式參數 > manifest["show"] 既存值;七欄缺一即 fail-fast。
    # (manifest 先讀——show 設定在裡面;episodes preflight 沿用同一份。)
    store = ManifestStore(manifest_path)
    manifest = store.read()
    saved_show = manifest.get("show") or {}
    show_cfg = {
        "show_id": show_id or saved_show.get("show_id"),
        "show_title": show_title or saved_show.get("show_title"),
        "show_description": show_description or saved_show.get("show_description"),
        "author": author or saved_show.get("author"),
        "owner_name": owner_name or saved_show.get("owner_name"),
        "owner_email": owner_email or saved_show.get("owner_email"),
        "artwork_path": artwork_path or saved_show.get("artwork_path"),
        # 布林/有預設的兩欄:None 才 fallback,避免 explicit=False 被誤判成「沒傳」。
        "category": category if category is not None else saved_show.get("category", "Technology"),
        "explicit": explicit if explicit is not None else bool(saved_show.get("explicit", False)),
        # notebook_id(重抓 fallback,選填)也要一起解析:它會進上傳的 show.json,
        # 不解析的話「首發有傳、之後沒傳」會讓 show.json bytes 不穩(null vs 值)。
        "notebook_id": notebook_id or saved_show.get("notebook_id"),
    }
    missing = [k for k in ("show_id", "show_title", "show_description", "author",
                           "owner_name", "owner_email", "artwork_path") if not show_cfg[k]]
    if missing:
        raise ValueError(
            f"missing show fields: {', '.join(missing)} — 首次發布請顯式傳齊"
            "(成功後自動存進 manifest['show'],之後只傳 manifest_path 即沿用)"
        )
    show_id = show_cfg["show_id"]
    show_title = show_cfg["show_title"]
    show_description = show_cfg["show_description"]
    author = show_cfg["author"]
    owner_name = show_cfg["owner_name"]
    owner_email = show_cfg["owner_email"]
    artwork_path = show_cfg["artwork_path"]
    category = show_cfg["category"]
    explicit = show_cfg["explicit"]
    notebook_id = show_cfg["notebook_id"]

    identity.validate_show_id(show_id)
    # Validate artwork up front (fail-fast before any upload). Extension follows
    # the real format so a JPEG is never served as .png. Content-address the filename
    # (artwork-<hash>.jpg) so a CHANGED show cover gets a NEW URL → bypasses the CDN
    # cache: the old fixed "artwork.jpg" name made Cloudflare serve the stale cover up
    # to its TTL (~4h). Same content -> same hash -> byte-stable feed across republish.
    art_info = artwork_mod.validate_artwork(artwork_path)
    with open(artwork_path, "rb") as f:
        art_bytes = f.read()
    art_ext = "jpg" if art_info["format"] == "JPEG" else "png"
    artwork_file = f"artwork-{hashlib.sha256(art_bytes).hexdigest()[:8]}.{art_ext}"

    token = identity.make_token(show_id, salt)

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
        # 單集封面每集必做(程式對齊 skill 政策):缺 cover_path 直接 fail,不再靜默
        # fallback 節目封面——否則漏生封面的集數會「發布成功」卻掛錯圖(EP03 就這樣漏掉)。
        # 這裡就驗(存在 + Apple 規格),讓缺檔/不合規在**任何 PUT 之前**就 fail,不留 orphan media。
        if not ep.get("cover_path"):
            raise ValueError(f"episode {n}: cover_path is required (每集必做,不再 fallback 節目封面)")
        artwork_mod.validate_artwork(ep["cover_path"])
        # 單集 show notes 必做且不可等於標題:缺/等於標題都 fail,不再 fallback 成標題
        # (否則播放器上簡介跟標題一字不差、看起來像壞掉)。
        desc = (ep.get("description") or "").strip()
        if not desc:
            raise ValueError(f"episode {n}: description is required (真 show notes,不可空白)")
        if desc == ep["title"].strip():
            raise ValueError(f"episode {n}: description must not equal title (需真 show notes)")

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
            local = await _ensure_local_mp3(ep, notebook_id)
            # 正規化成 true MP3 並內嵌 ID3/APIC:Apple/Spotify 常優先吃音檔內嵌圖,
            # 不是 feed 的 <item> itunes:image。正規化/內嵌後 bytes 變 → content-hash/URL
            # 變(預期一次性 churn,uploader 不刪舊 URL)。cover_path 已 preflight。
            mp3_bytes = _embed_cover(local, ep["cover_path"])
            mp3_len = len(mp3_bytes)                                # enclosure length 用內嵌後大小
            hash8 = hashlib.sha256(mp3_bytes).hexdigest()[:8]      # hash 內嵌後 bytes
            mfile = media_filename(n, hash8)
            await _put(client, upload_url, token, upload_token, mfile, mp3_bytes)
            del mp3_bytes

            # 1a) media: 單集封面(每集必做,preflight 已驗存在 + Apple 規格:方形
            #     1400–3000px、RGB、無 alpha)。content-addressed;這裡重讀一次取格式定副檔名。
            cpath = ep["cover_path"]
            c_info = artwork_mod.validate_artwork(cpath)
            with open(cpath, "rb") as f:
                cover_bytes = f.read()
            c_ext = "jpg" if c_info["format"] == "JPEG" else "png"
            ep_artwork_file = cover_filename(n, hashlib.sha256(cover_bytes).hexdigest()[:8], c_ext)
            await _put(client, upload_url, token, upload_token, ep_artwork_file, cover_bytes)
            del cover_bytes

            # 1b) media: 選填附件(簡報 PDF / 研讀講義 HTML),content-addressed,
            #     公開 URL append 到單集 description。缺檔 fail-fast(不 re-download)。
            base_pub = base_url.rstrip("/")
            desc_base = ep["description"].strip()   # preflight 已保證非空且不等於標題
            attachments: list[tuple[str, str, str]] = []   # (emoji, label, url)
            pdf_url = None      # 回傳給呼叫端,免其事後逆向 content-hash 檔名
            html_url = None

            spath = ep.get("slides_pdf_path")
            if spath:
                if not (os.path.exists(spath) and os.path.getsize(spath) > 0):
                    raise ValueError(f"episode {n}: slides_pdf_path missing file: {spath}")
                with open(spath, "rb") as f:
                    pdf_bytes = f.read()
                pfile = attachment_filename(n, hashlib.sha256(pdf_bytes).hexdigest()[:8], "pdf")
                pdf_url = f"{base_pub}/feeds/{token}/{pfile}"
                await _put(client, upload_url, token, upload_token, pfile, pdf_bytes)
                attachments.append(("📄", "本集簡報 (PDF)", pdf_url))
                del pdf_bytes

            rpath = ep.get("report_md_path")
            if rpath:
                if not (os.path.exists(rpath) and os.path.getsize(rpath) > 0):
                    raise ValueError(f"episode {n}: report_md_path missing file: {rpath}")
                with open(rpath, encoding="utf-8") as f:
                    html_bytes = notes_html.render_report_html(f.read(), ep["title"]).encode("utf-8")
                hfile = attachment_filename(n, hashlib.sha256(html_bytes).hexdigest()[:8], "html")
                html_url = f"{base_pub}/feeds/{token}/{hfile}"
                await _put(client, upload_url, token, upload_token, hfile, html_bytes)
                del html_bytes                                     # 同 mp3/pdf:一次一 blob,傳完即釋放
                attachments.append(("📖", "研讀講義", html_url))

            # 純文字 <description>(fallback,含裸 URL)+ 富文字 <content:encoded>
            # (Apple/Overcast/Pocket Casts 優先渲染:條列 + 具名連結,不裸露長 URL)。
            desc = desc_base
            if attachments:
                desc += "\n\n" + "\n".join(f"{e} {label}:{u}" for e, label, u in attachments)
            desc_html = notes_html.render_episode_notes_html(desc_base, attachments)

            new_eps[str(n)] = {
                "title": ep["title"],
                "description": desc,
                "description_html": desc_html,
                "guid": identity.episode_guid(show_id, n),
                "pub_date": ep.get("published_at") or _fallback_pub_date(n),
                "media_file": mfile,
                "length": mp3_len,   # 內嵌封面後的大小(mp3_bytes 已 del)
            }
            duration = _audio_duration_hms(local)
            if duration:
                new_eps[str(n)]["duration"] = duration
            new_eps[str(n)]["artwork_file"] = ep_artwork_file   # 每集必做,一定有單集封面
            published.append({
                "n": n, "title": ep["title"], "guid": new_eps[str(n)]["guid"],
                "url": f"{base_pub}/feeds/{token}/{mfile}",
                "cover_url": f"{base_pub}/feeds/{token}/{ep_artwork_file}",
                "pdf_url": pdf_url,     # None 若該集無簡報
                "html_url": html_url,   # None 若該集無講義
                "duration": duration,   # 對帳用(HH:MM:SS;ffprobe 失敗為 None),免再抓整份 feed
            })

        await _put(client, upload_url, token, upload_token,         # 1) media: artwork
                   artwork_file, art_bytes)                         # bytes 已在 preflight 讀好

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

    # 發布成功才在最新 snapshot 上回寫 show；store lock 不跨上方任何 HTTP await。
    persisted_show = {
        **show_cfg,
        "artwork_path": os.path.abspath(show_cfg["artwork_path"]),
    }
    store.update(lambda latest: latest.update({"show": persisted_show}))

    episodes_out = sorted(published, key=lambda e: e["n"])
    if return_episodes is not None:
        # 只縮回傳、不縮發布:feed 仍是整季;episode_count 維持全季數,別誤讀成「只發了這些」。
        want = set(return_episodes)
        episodes_out = [e for e in episodes_out if e["n"] in want]
    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len(new_eps),
        "episodes": episodes_out,
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
